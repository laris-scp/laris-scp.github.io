#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import io
import json
import hashlib
import warnings
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin

import requests
import pdfplumber
import anthropic
from requests.exceptions import SSLError, HTTPError

SERIES_PATH = Path("data/cafe/series/ico_ia.json")

ICO_LIST_URL = "https://ico.org/specialized-reports/"

# Fallbacks (caso o HTML mude / scraping falhe / geração por regra falhe)
FALLBACK_PDFS = [
    "https://www.ico.org/documents/cy2025-26/cmr-0326-e.pdf",
    "https://www.ico.org/documents/cy2025-26/cmr-0226-e.pdf",
    "https://www.ico.org/documents/cy2025-26/cmr-0126-e.pdf",
    "https://www.ico.org/documents/cy2025-26/cmr-1225-e.pdf",
    "https://www.ico.org/documents/cy2025-26/cmr-1125-e.pdf",
]

# Anthropic Claude
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

MAX_CHARS_TOTAL = 120_000
HEAD_CHARS = 70_000
TAIL_CHARS = 50_000

SYSTEM_ROLE = (
    "Você é um analista macroeconômico especializado em commodities agrícolas, "
    "com foco no mercado global de café (Arabica e Robusta). "
    "Seu objetivo NÃO é resumir o texto, mas extrair sinais econômicos relevantes para o preço do café."
)

USER_PROMPT_TEMPLATE = """
Leia cuidadosamente o relatório abaixo e execute as etapas a seguir.

FONTE: {fonte}

RELATÓRIO:
\"\"\"
{document_text}
\"\"\"

ETAPA 1 — EXTRAÇÃO DE FATOS
Identifique APENAS fatos novos ou relevantes relacionados a:
- Produção de café
- Exportações de café
- Estoques de café
- Revisões de safra
- Ritmo de embarques / fluxos comerciais

Ignore dados irrelevantes (outros produtos, valores financeiros sem contexto físico, etc).

ETAPA 2 — CLASSIFICAÇÃO ECONÔMICA DE CADA FATO
Para cada fato identificado, classifique segundo as regras abaixo:

REGRAS FIXAS:
- Aumento de produção → BEARISH
- Se a produção total subir, mas o Arábica cair (ou houver mudança relevante de mix Arábica/Robusta), classifique como NEUTRAL e explique o conflito (mix).
- Revisão positiva de safra → BEARISH
- Queda de produção → BULLISH
- Queda de estoques → BULLISH
- Aumento de estoques → BEARISH
- Exportações acelerando → BULLISH
- Exportações desacelerando:
  - Se a queda ocorreu por restrição de oferta / falta de produto / estoques internos baixos / logística sem disponibilidade física → BULLISH
  - Se a queda ocorreu por demanda fraca / cancelamentos / perda de competitividade / preços altos reduzindo consumo / recessão → BEARISH
  - Se não houver explicação clara do motivo → NEUTRAL
- Se um fato não tiver impacto claro → NEUTRAL

ETAPA 3 — CLASSIFICAÇÃO FINAL (REGRA OBRIGATÓRIA)

Com base EXCLUSIVAMENTE nas evidências listadas acima, aplique a seguinte regra objetiva
para definir o cenário final do mercado de café:

- Conte quantas evidências são BEARISH e quantas são BULLISH.
- Ignore evidências classificadas como NEUTRAL.

Regras:
1) Se BEARISH ≥ 2 e BULLISH ≤ 1 → label = BEARISH e signal = -1.0
2) Se BULLISH ≥ 2 e BEARISH ≤ 1 → label = BULLISH e signal = 1.0
3) Em qualquer outro caso → label = NEUTRAL e signal = 0.0

Esta regra deve ser seguida obrigatoriamente, sem exceções ou julgamentos subjetivos.


ETAPA 4 — SCORE NUMÉRICO
- BULLISH → +1.0
- NEUTRAL → 0.0
- BEARISH → -1.0
Não use valores intermediários.

ETAPA 5 — EXPLICAÇÃO CURTA (OBRIGATÓRIA)
Explique o diagnóstico final em até 5 bullets, no formato:
- Fonte: <nome da instituição>
- Fato principal
- Regra aplicada
- Impacto esperado no preço

ETAPA 6 — SAÍDA ESTRUTURADA
Retorne APENAS o JSON abaixo, sem texto adicional, sem markdown, sem ```json:

{{
  "fonte": "{fonte}",
  "signal": <score numérico>,
  "label": "<BULLISH | BEARISH | NEUTRAL>",
  "evidencias": [
    "<bullet 1>",
    "<bullet 2>",
    "<bullet 3>"
  ]
}}
""".strip()


# -------------------------
# Helpers
# -------------------------
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def extract_pdf_text_bytes(pdf_bytes: bytes) -> str:
    buff = io.BytesIO(pdf_bytes)
    texts = []
    with pdfplumber.open(buff) as pdf:
        for i, page in enumerate(pdf.pages):
            txt = page.extract_text() or ""
            txt = re.sub(r"[ \t]+", " ", txt)
            texts.append(f"\n\n--- PAGE {i+1} ---\n{txt}")
    return "\n".join(texts).strip()


def shrink_text(text: str) -> str:
    if len(text) <= MAX_CHARS_TOTAL:
        return text
    head = text[:HEAD_CHARS]
    tail = text[-TAIL_CHARS:]
    return head + "\n\n[...TRUNCADO PARA CABER NO CONTEXTO...]\n\n" + tail


def find_pdf_links_from_html(html: str, base_url: str) -> list[str]:
    """
    Extrai ocorrências de cmr-MMYY-e.pdf no HTML (absoluta ou relativa).
    """
    abs_matches = re.findall(
        r'(https?://[^\s"\'<>]*cmr-\d{4}-e\.pdf[^\s"\'<>]*)',
        html,
        flags=re.IGNORECASE,
    )
    rel_matches = re.findall(
        r'["\']([^"\']*cmr-\d{4}-e\.pdf[^"\']*)["\']',
        html,
        flags=re.IGNORECASE,
    )

    urls = []
    for u in abs_matches:
        urls.append(u.strip())
    for u in rel_matches:
        urls.append(urljoin(base_url, u.strip()))

    urls = [u for u in urls if u]
    return sorted(set(urls))


def find_report_page_links_from_html(html: str, base_url: str) -> list[str]:
    """
    Extrai links das páginas individuais de relatório (ex: '.../monthly-coffee-market-report-march-2026/').
    Esses links levam pra páginas que contêm o PDF embutido.
    """
    matches = re.findall(
        r'href=["\']([^"\']*monthly-coffee-market-report[^"\']*)["\']',
        html,
        flags=re.IGNORECASE,
    )
    urls = [urljoin(base_url, m.strip()) for m in matches]
    # remove ancoras e duplicatas
    urls = [u.split("#")[0] for u in urls if u]
    return sorted(set(urls))


def pdf_mmyy_to_date(url: str) -> str:
    m = re.search(r"cmr-(\d{2})(\d{2})-e\.pdf", url, flags=re.IGNORECASE)
    if not m:
        raise ValueError(f"Não consegui extrair MMYY do PDF: {url}")
    mm = int(m.group(1))
    yy = int(m.group(2))
    year = 2000 + yy
    return f"{year:04d}-{mm:02d}-01"


def cy_folder_for_year_month(year: int, month: int) -> str:
    """
    Pela evidência:
      - Oct-Dec/2025 => cy2025-26
      - Sep/2025     => cy2024-25
    O 'coffee year' vira em Outubro.
    """
    if month >= 10:
        y1 = year
        y2 = year + 1
    else:
        y1 = year - 1
        y2 = year
    return f"cy{y1}-{str(y2)[-2:]}"


def build_ico_cmr_url_variants(year: int, month: int) -> list[str]:
    """
    Gera as 3 variações de URL pra um dado PDF, em ordem de preferência.
    A ICO mudou de http pra https em algum momento, então tentamos várias.
    """
    cy = cy_folder_for_year_month(year, month)
    mmyy = f"{month:02d}{str(year)[-2:]}"
    path = f"/documents/{cy}/cmr-{mmyy}-e.pdf"
    return [
        f"https://www.ico.org{path}",
        f"http://www.ico.org{path}",
        f"https://ico.org{path}",
    ]


def generate_candidate_cmr_urls(n_months: int, anchor: datetime | None = None) -> list[str]:
    """
    Gera URLs dos últimos n_months a partir de anchor (default: hoje UTC),
    do mais antigo para o mais recente. Apenas a 1ª variação (https://www.ico.org)
    porque o normalize/try-download cuida das variações depois.
    """
    if anchor is None:
        anchor = datetime.utcnow()

    y = anchor.year
    m = anchor.month

    # primeiro mês (mais antigo)
    y0, m0 = y, m
    for _ in range(n_months - 1):
        m0 -= 1
        if m0 == 0:
            m0 = 12
            y0 -= 1

    out = []
    yy, mm = y0, m0
    for _ in range(n_months):
        out.append(build_ico_cmr_url_variants(yy, mm)[0])  # versão preferencial
        mm += 1
        if mm == 13:
            mm = 1
            yy += 1

    return out


def load_series():
    if SERIES_PATH.exists():
        payload = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
        series = payload.get("series", [])
        if not isinstance(series, list):
            series = []
        return payload, series

    payload = {
        "id": "ico_ia",
        "name": "ICO (IA) — Relatório Mensal",
        "unit": "score",
        "frequency": "Mensal",
        "series": [],
        "source": "International Coffee Organization (ICO) — Monthly Coffee Market Report (CMR).",
    }
    return payload, []


def already_has_sha(series: list[dict], sha: str) -> bool:
    for p in series:
        if str(p.get("pdf_sha256", "")).lower() == sha.lower():
            return True
    return False


def already_has_date(series: list[dict], date: str) -> bool:
    for p in series:
        if str(p.get("date", "")).strip() == str(date).strip():
            return True
    return False


def call_claude(document_text: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY não encontrado no ambiente (GitHub Secrets).")

    client = anthropic.Anthropic()
    user_prompt = USER_PROMPT_TEMPLATE.format(fonte="ICO", document_text=document_text)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_ROLE,
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Extrai texto de todos os blocos (geralmente é só 1)
    out = ""
    for block in resp.content:
        if hasattr(block, "text"):
            out += block.text
    out = out.strip()

    # Tenta parsear direto
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # Procura JSON no meio do texto (caso venha com algum preâmbulo)
        m = re.search(r"\{.*\}", out, flags=re.DOTALL)
        if not m:
            raise RuntimeError(f"Resposta não veio em JSON válido.\n\nResposta bruta:\n{out[:2000]}")
        return json.loads(m.group(0))


def url_variants_for(url: str) -> list[str]:
    """
    Dado um URL de PDF (qualquer scheme/host), gera as 3 variações pra tentar.
    """
    m = re.search(r"cmr-(\d{2})(\d{2})-e\.pdf", url, flags=re.IGNORECASE)
    if not m:
        # se não casa o padrão, devolve só o original
        return [url]

    mm = int(m.group(1))
    yy = int(m.group(2))
    year = 2000 + yy
    return build_ico_cmr_url_variants(year, mm)


def try_download(url: str) -> bytes:
    """
    Tenta baixar um PDF testando as 3 variações de URL (https/www, http/www, https sem www).
    Retorna o conteúdo do primeiro que funcionar; raise no último erro se todos falharem.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    last_err: Exception | None = None

    for candidate in url_variants_for(url):
        try:
            r = requests.get(candidate, timeout=90, headers=headers, allow_redirects=True)
            r.raise_for_status()
            content = r.content
            # valida se é PDF mesmo
            if not content.startswith(b"%PDF-"):
                last_err = RuntimeError(f"Conteúdo baixado de {candidate} não é PDF (primeiros bytes: {content[:20]!r})")
                continue
            print(f"DEBUG: download OK em {candidate}")
            return content
        except SSLError as e:
            # tenta sem verify
            try:
                warnings.filterwarnings("ignore", message="Unverified HTTPS request")
                r = requests.get(candidate, timeout=90, headers=headers, allow_redirects=True, verify=False)
                r.raise_for_status()
                content = r.content
                if not content.startswith(b"%PDF-"):
                    last_err = RuntimeError(f"Fallback SSL: conteúdo de {candidate} não é PDF")
                    continue
                print(f"DEBUG: download OK em {candidate} (sem SSL verify)")
                return content
            except Exception as e2:
                last_err = e2
                continue
        except Exception as e:
            last_err = e
            continue

    raise last_err if last_err else RuntimeError(f"Falha em todas as variações de {url}")


def discover_pdfs_via_scraping() -> list[str]:
    """
    Estratégia melhorada de scraping:
    1. Pega o HTML da página de listagem (specialized-reports)
    2. Tenta achar links diretos pra cmr-MMYY-e.pdf
    3. Se não achar, segue os links das páginas individuais de relatório
       e tenta extrair o PDF de cada uma.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    pdf_urls: list[str] = []

    try:
        r = requests.get(ICO_LIST_URL, timeout=30, headers=headers)
        r.raise_for_status()
        list_html = r.text
    except Exception as e:
        print(f"DEBUG: falha ao buscar página de listagem: {e}")
        return []

    # Tentativa 1: links diretos pro PDF
    direct = find_pdf_links_from_html(list_html, ICO_LIST_URL)
    if direct:
        print(f"DEBUG: scraping direto encontrou {len(direct)} PDFs")
        pdf_urls.extend(direct)

    # Tentativa 2: páginas individuais de relatório
    report_pages = find_report_page_links_from_html(list_html, ICO_LIST_URL)
    print(f"DEBUG: encontradas {len(report_pages)} páginas de relatório")

    for page_url in report_pages[:24]:  # limita para não explodir
        try:
            rp = requests.get(page_url, timeout=30, headers=headers)
            if not rp.ok:
                continue
            page_pdfs = find_pdf_links_from_html(rp.text, page_url)
            if page_pdfs:
                pdf_urls.extend(page_pdfs)
        except Exception as e:
            print(f"DEBUG: falha ao abrir {page_url}: {e}")
            continue

    pdf_urls = sorted(set(pdf_urls))
    return pdf_urls


# -------------------------
# Main
# -------------------------
def main():
    payload, series = load_series()

    # Backfill: por padrão roda 1 (último). Em workflow_dispatch você passa ICO_BACKFILL_N.
    backfill_n = int(os.environ.get("ICO_BACKFILL_N", "1").strip() or "1")
    backfill_n = max(1, min(backfill_n, 48))  # teto de segurança

    # 1) Descobrir PDFs disponíveis — 3 camadas:
    #    (a) scraping melhorado (segue páginas individuais)
    #    (b) geração por regra (últimos N + alguns extras pra cobrir gap)
    #    (c) fallbacks fixos
    pdf_urls: list[str] = []

    print("=" * 60)
    print("ETAPA 1: Scraping melhorado")
    print("=" * 60)
    scraped = discover_pdfs_via_scraping()
    print(f"DEBUG: scraping retornou {len(scraped)} URLs")
    pdf_urls.extend(scraped)

    print("=" * 60)
    print("ETAPA 2: Geração por regra")
    print("=" * 60)
    # gera mais meses do que backfill_n pra dar margem (ICO publica com ~10d de atraso)
    gen_n = max(backfill_n + 3, 6)
    generated = generate_candidate_cmr_urls(gen_n)
    print(f"DEBUG: geração por regra produziu {len(generated)} candidatos")
    pdf_urls.extend(generated)

    print("=" * 60)
    print("ETAPA 3: Fallbacks fixos")
    print("=" * 60)
    pdf_urls.extend(FALLBACK_PDFS)
    print(f"DEBUG: {len(FALLBACK_PDFS)} fallbacks adicionados")

    # Filtra só PDFs do CMR e dedupa por (mês, ano), preferindo a versão https://www.ico.org
    cmr_urls = [u for u in pdf_urls if re.search(r"cmr-\d{4}-e\.pdf", u, flags=re.IGNORECASE)]

    # Dedup por date (MMYY) — mantém só uma URL por mês
    by_date: dict[str, str] = {}
    for u in cmr_urls:
        try:
            d = pdf_mmyy_to_date(u)
        except ValueError:
            continue
        # prioriza https://www.ico.org
        if d not in by_date:
            by_date[d] = u
        elif "https://www.ico.org" in u and "https://www.ico.org" not in by_date[d]:
            by_date[d] = u

    pdf_urls = sorted(by_date.values(), key=pdf_mmyy_to_date)

    # processa só os N mais recentes (backfill)
    targets = pdf_urls[-min(backfill_n, len(pdf_urls)):]
    print(f"\nINFO: backfill_n={backfill_n} | candidatos únicos={len(pdf_urls)} | processando={len(targets)}")
    for t in targets:
        print(f"  -> {t}")

    added = 0
    for pdf_url in targets:
        point_date = pdf_mmyy_to_date(pdf_url)

        if already_has_date(series, point_date):
            print(f"SKIP: date já existe no JSON: {point_date}")
            continue

        # download resiliente: tenta as 3 variações de URL
        try:
            pdf_bytes = try_download(pdf_url)
        except HTTPError as e:
            print(f"SKIP: HTTP ao baixar {pdf_url} | erro={e}")
            continue
        except Exception as e:
            print(f"SKIP: falha ao baixar {pdf_url} | erro={e}")
            continue

        pdf_sha = sha256_bytes(pdf_bytes)

        if already_has_sha(series, pdf_sha):
            print(f"SKIP: hash já existe no JSON: {point_date}")
            continue

        raw_text = extract_pdf_text_bytes(pdf_bytes)
        doc_text = shrink_text(raw_text)
        result = call_claude(doc_text)

        signal = float(result.get("signal"))
        label = str(result.get("label", "")).upper().strip()
        evid = result.get("evidencias", [])

        if label not in {"BULLISH", "BEARISH", "NEUTRAL"}:
            raise RuntimeError(f"Label inválido retornado: {label}")
        if signal not in (-1.0, 0.0, 1.0):
            raise RuntimeError(f"Signal inválido retornado: {signal}")

        point = {
            "date": point_date,
            "close": signal,
            "signal": signal,
            "label": label,
            "evidencias": evid[:5],
            "pdf_url": pdf_url,
            "pdf_sha256": pdf_sha,
            "model": MODEL,
            "prompt_version": "v2",
        }

        series.append(point)
        added += 1
        print(f"OK: add {point_date} | {label} | {signal}")

    if added == 0:
        print("INFO: nenhum ponto novo para adicionar.")
        return

    # dedup por date e ordena
    tmp = {}
    for p in series:
        tmp[str(p.get("date"))] = p
    series2 = [tmp[k] for k in sorted(tmp.keys())]

    payload["series"] = series2
    payload["updated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    SERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERIES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"OK: ico_ia.json atualizado. added={added} | last={series2[-1]['date']}")


if __name__ == "__main__":
    main()
