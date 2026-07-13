#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualiza data/soja/series/oil_crops_outlook.json

Lê o relatório mensal "Oil Crops Outlook" (USDA/ERS), envia o texto para um
agente Claude que classifica o sentimento para o preço da soja em grão (CBOT)
em BULLISH / BEARISH / NEUTRAL via ponderação fato a fato.

Espelha a arquitetura de scripts/cafe/update_ico_ia_series.py, adaptando a
camada de descoberta de PDF para a ERS (página de série -> páginas de detalhe).
"""

import os
import re
import io
import json
import hashlib
import warnings
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
import pdfplumber
import anthropic
from requests.exceptions import SSLError, HTTPError

SERIES_PATH = Path("data/soja/series/oil_crops_outlook.json")

# Sitemap do ERS (índice -> sitemap.xml -> páginas paginadas). Usado para
# descobrir novas edições do Oil Crops Outlook. A página de busca antiga
# (?series=OCS) passou a ser renderizada via JS e não expõe mais links
# no HTML estático (confirmado em 2026-07 — ela retorna "There was an
# error. Please refresh the page." em requisição direta).
SITEMAP_INDEX_URL = "https://www.ers.usda.gov/index/sitemap.xml"
SITEMAP_RECENT_DAYS = 45  # janela de "candidato recente" pra não varrer o sitemap inteiro

# Fallbacks (caso o scraping falhe). Atualize quando souber de novas edições.
FALLBACK_PDFS = [
    "https://ers.usda.gov/sites/default/files/_laserfiche/outlooks/114151/OCS-26e.pdf",
    "https://ers.usda.gov/sites/default/files/_laserfiche/outlooks/114038/OCS-26d.pdf",
    "https://ers.usda.gov/sites/default/files/_laserfiche/outlooks/113933/OCS-26c.pdf",
    "https://ers.usda.gov/sites/default/files/_laserfiche/outlooks/113803/OCS-26b.pdf",
    "https://ers.usda.gov/sites/default/files/_laserfiche/outlooks/113678/OCS-26a.pdf",
]

# Anthropic Claude
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
PROMPT_VERSION = "v1"

MAX_CHARS_TOTAL = 120_000
HEAD_CHARS = 70_000
TAIL_CHARS = 50_000

# Letra do nome do arquivo (OCS-26e) -> mês
LETTER_TO_MONTH = {c: i for i, c in enumerate("abcdefghijkl", start=1)}
MONTH_TO_LETTER = {v: k for k, v in LETTER_TO_MONTH.items()}

SYSTEM_ROLE = (
    "Você é um analista macroeconômico especializado em commodities agrícolas, "
    "com foco no mercado global de soja. Seu objetivo NÃO é resumir o texto, "
    "mas extrair sinais econômicos relevantes para o preço da soja em grão "
    "negociada na bolsa de Chicago (CBOT)."
)

USER_PROMPT_TEMPLATE = """
Leia cuidadosamente o relatório "Oil Crops Outlook" (USDA/ERS) abaixo e
execute as etapas a seguir.

FONTE: {fonte}

RELATÓRIO:
\"\"\"
{document_text}
\"\"\"

ETAPA 1 — EXTRAÇÃO DE FATOS
Identifique APENAS fatos novos ou relevantes relacionados a SOJA EM GRÃO,
FARELO DE SOJA e ÓLEO DE SOJA:
- Produção de soja (EUA, Brasil, Argentina, global)
- Área plantada / intenção de plantio
- Esmagamento (crush)
- Exportações e importações de soja e derivados
- Estoques finais
- Demanda de farelo e de óleo de soja
- Uso de óleo de soja para biocombustível
Ignore canola, girassol, palma, azeite, amendoim, algodão e demais
oleaginosas. Se houver um "Special Article" (artigo especial), trate-o
como contexto e NÃO extraia fatos dele.

Antes de extrair, identifique o REGIME do relatório:
- MENSAL (junho a abril): revisa o ano-safra em curso. Avalie as
  mudanças mês a mês ("raised/reduced this month").
- NOVA SAFRA (maio, parte de junho): introduz a projeção do próximo
  ano-safra. Avalie as mudanças ano-contra-ano da nova projeção.

ETAPA 2 — CLASSIFICAÇÃO ECONÔMICA DE CADA FATO
Para cada fato, classifique segundo as regras abaixo:

REGRAS FIXAS:
- Aumento de produção (qualquer país) -> BEARISH
- Queda de produção -> BULLISH
- Revisão de safra para cima -> BEARISH
- Revisão de safra para baixo -> BULLISH
- Aumento de área plantada -> BEARISH
- Redução de área plantada -> BULLISH
- Aumento de estoques finais -> BEARISH
- Queda de estoques finais -> BULLISH
- Aumento de esmagamento (crush) -> BULLISH
- Queda de esmagamento -> BEARISH
- Exportações dos EUA acelerando / revisadas para cima -> BULLISH
- Exportações dos EUA desacelerando / revisadas para baixo:
  - Se por concorrência de outro exportador (ex.: Brasil) / perda de
    competitividade / preço alto -> BEARISH
  - Se por restrição de oferta / falta de produto físico -> BULLISH
  - Se não houver explicação clara -> NEUTRAL
- Aumento de demanda de farelo ou de óleo -> BULLISH
- Queda de demanda de farelo ou de óleo -> BEARISH
- Aumento de uso de óleo para biocombustível -> BULLISH
- Queda de uso de óleo para biocombustível -> BEARISH
- Aumento das importações da China -> BULLISH
- Queda das importações da China -> BEARISH
- Se um fato não tiver impacto claro -> NEUTRAL

ETAPA 3 — CLASSIFICAÇÃO FINAL (REGRA OBRIGATÓRIA)
Com base EXCLUSIVAMENTE nas evidências da Etapa 2:
- Conte quantas evidências são BEARISH e quantas são BULLISH.
- Ignore as classificadas como NEUTRAL.

Regras (regra de diferença, sem assimetria):
1) Se (BEARISH - BULLISH) >= 2 -> label = BEARISH e signal = -1.0
2) Se (BULLISH - BEARISH) >= 2 -> label = BULLISH e signal = 1.0
3) Em qualquer outro caso -> label = NEUTRAL e signal = 0.0

Esta regra deve ser seguida obrigatoriamente, sem exceções ou
julgamentos subjetivos. NÃO use a previsão de preço do USDA como
evidência nem como desempate.

ETAPA 4 — SCORE NUMÉRICO
- BULLISH -> +1.0
- NEUTRAL -> 0.0
- BEARISH -> -1.0
Não use valores intermediários.

ETAPA 5 — EXPLICAÇÃO CURTA (OBRIGATÓRIA)
Explique o diagnóstico em até 5 bullets:
- Fonte: USDA/ERS — Oil Crops Outlook
- Regime identificado (MENSAL ou NOVA SAFRA)
- Contagem final (X BEARISH x Y BULLISH)
- Fatos principais que pesaram
- Impacto esperado no preço

ETAPA 6 — SAÍDA ESTRUTURADA
Retorne APENAS o JSON abaixo, sem texto adicional, sem markdown, sem ```json:

{{
  "fonte": "{fonte}",
  "regime": "<MENSAL | NOVA_SAFRA>",
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


def ocs_name_to_date(url_or_name: str) -> str:
    """
    OCS-26e.pdf -> '2026-05-01'  (e = 5o mes do ano de letras a..l)
    """
    m = re.search(r"OCS-(\d{2})([a-l])\.pdf", url_or_name, flags=re.IGNORECASE)
    if not m:
        raise ValueError(f"Não consegui extrair YY+letra de: {url_or_name}")
    yy = int(m.group(1))
    letter = m.group(2).lower()
    month = LETTER_TO_MONTH[letter]
    year = 2000 + yy
    return f"{year:04d}-{month:02d}-01"


def find_ocs_pdf_links(html: str, base_url: str) -> list[str]:
    """Extrai links diretos para OCS-NN[a-l].pdf de um HTML."""
    abs_matches = re.findall(
        r'(https?://[^\s"\'<>]*OCS-\d{2}[a-l]\.pdf[^\s"\'<>]*)',
        html, flags=re.IGNORECASE,
    )
    rel_matches = re.findall(
        r'["\']([^"\']*OCS-\d{2}[a-l]\.pdf[^"\']*)["\']',
        html, flags=re.IGNORECASE,
    )
    urls = [u.strip() for u in abs_matches]
    urls += [urljoin(base_url, u.strip()) for u in rel_matches]
    return sorted(set(u for u in urls if u))


def strip_pdf_query(url: str) -> str:
    """Remove o sufixo ?v=NNNNN para deduplicar e normalizar."""
    return url.split("?")[0]


def _fetch_xml(url: str, headers: dict) -> str:
    r = requests.get(url, timeout=30, headers=headers)
    r.raise_for_status()
    return r.text


def get_recent_publication_candidates(max_age_days: int = SITEMAP_RECENT_DAYS) -> list[str]:
    """
    Varre o sitemap do ERS (3 níveis: índice -> sitemap.xml -> páginas
    paginadas) e retorna as URLs /publications/{id} cujo <lastmod> é
    recente. Isso substitui a antiga dependência da página de busca
    (?series=OCS), que passou a ser renderizada via JS.
    """
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        idx_xml = _fetch_xml(SITEMAP_INDEX_URL, headers)
        top_locs = re.findall(r"<loc>(.*?)</loc>", idx_xml)
        if not top_locs:
            print("DEBUG: índice do sitemap sem <loc>.")
            return []

        mid_xml = _fetch_xml(top_locs[0].strip(), headers)
        page_locs = re.findall(r"<loc>(.*?)</loc>", mid_xml)
        if not page_locs:
            print("DEBUG: sitemap intermediário sem páginas.")
            return []
    except Exception as e:
        print(f"DEBUG: falha ao navegar o índice do sitemap: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    candidates: list[str] = []

    for page_url in page_locs:
        try:
            page_xml = _fetch_xml(page_url.strip(), headers)
        except Exception as e:
            print(f"DEBUG: falha ao buscar {page_url}: {e}")
            continue

        blocks = re.findall(
            r"<url>\s*<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>.*?</url>",
            page_xml, flags=re.DOTALL,
        )
        for loc, lastmod in blocks:
            loc = loc.strip()
            if not re.search(r"/publications/\d+$", loc):
                continue
            try:
                dt = datetime.fromisoformat(lastmod.strip())
            except ValueError:
                continue
            if dt >= cutoff:
                candidates.append(loc)

    candidates = sorted(set(candidates))
    print(f"DEBUG: {len(candidates)} candidatos com lastmod <= {max_age_days} dias")
    return candidates


def discover_pdfs_via_scraping() -> list[str]:
    """
    Descobre PDFs do Oil Crops Outlook via sitemap do ERS:
    1. Pega candidatos recentes de /publications/{id} pelo sitemap.
    2. Abre cada candidato e confirma pelo <title> se é do Oil Crops Outlook.
    3. Extrai o link do PDF de cada página confirmada (find_ocs_pdf_links
       já trata link relativo via urljoin, sem precisar de mudança).
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    pdf_urls: list[str] = []

    candidates = get_recent_publication_candidates()
    print(f"DEBUG: {len(candidates)} páginas candidatas (via sitemap)")

    for page_url in candidates:
        try:
            r = requests.get(page_url, timeout=30, headers=headers)
            if not r.ok:
                continue
            html = r.text
        except Exception as e:
            print(f"DEBUG: falha ao abrir {page_url}: {e}")
            continue

        title_match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
        title = title_match.group(1) if title_match else ""
        if "oil crops outlook" not in title.lower():
            continue

        print(f"DEBUG: página do Oil Crops Outlook encontrada: {page_url} | title={title.strip()}")
        page_pdfs = find_ocs_pdf_links(html, page_url)
        if page_pdfs:
            pdf_urls.extend(page_pdfs)

    return sorted(set(pdf_urls))


def try_download(url: str) -> bytes:
    """Baixa um PDF, com fallback de SSL verify=False (igual ao script do café)."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, timeout=90, headers=headers, allow_redirects=True)
        r.raise_for_status()
        content = r.content
        if not content.startswith(b"%PDF-"):
            raise RuntimeError(f"Conteúdo de {url} não é PDF (bytes: {content[:20]!r})")
        print(f"DEBUG: download OK em {url}")
        return content
    except SSLError:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        r = requests.get(url, timeout=90, headers=headers,
                          allow_redirects=True, verify=False)
        r.raise_for_status()
        content = r.content
        if not content.startswith(b"%PDF-"):
            raise RuntimeError(f"Fallback SSL: conteúdo de {url} não é PDF")
        print(f"DEBUG: download OK em {url} (sem SSL verify)")
        return content


def load_series():
    if SERIES_PATH.exists():
        payload = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
        series = payload.get("series", [])
        if not isinstance(series, list):
            series = []
        return payload, series

    payload = {
        "id": "oil_crops_outlook",
        "name": "Oil Crops Outlook (IA) — Relatório Mensal",
        "unit": "score",
        "frequency": "Mensal",
        "series": [],
        "source": "USDA, Economic Research Service — Oil Crops Outlook.",
    }
    return payload, []


def already_has_sha(series: list[dict], sha: str) -> bool:
    return any(str(p.get("pdf_sha256", "")).lower() == sha.lower() for p in series)


def already_has_date(series: list[dict], date: str) -> bool:
    return any(str(p.get("date", "")).strip() == str(date).strip() for p in series)


def call_claude(document_text: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY não encontrado no ambiente (GitHub Secrets).")

    client = anthropic.Anthropic()
    user_prompt = USER_PROMPT_TEMPLATE.format(
        fonte="USDA/ERS — Oil Crops Outlook",
        document_text=document_text,
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_ROLE,
        messages=[{"role": "user", "content": user_prompt}],
    )

    out = ""
    for block in resp.content:
        if hasattr(block, "text"):
            out += block.text
    out = out.strip()

    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", out, flags=re.DOTALL)
        if not m:
            raise RuntimeError(f"Resposta não veio em JSON válido.\n\nBruta:\n{out[:2000]}")
        return json.loads(m.group(0))


# -------------------------
# Main
# -------------------------
def main():
    payload, series = load_series()

    backfill_n = int(os.environ.get("OCS_BACKFILL_N", "1").strip() or "1")
    backfill_n = max(1, min(backfill_n, 48))

    print("=" * 60)
    print("ETAPA 1: Scraping da série ERS")
    print("=" * 60)
    scraped = discover_pdfs_via_scraping()
    print(f"DEBUG: scraping retornou {len(scraped)} URLs")

    print("=" * 60)
    print("ETAPA 2: Fallbacks fixos")
    print("=" * 60)
    pdf_urls = list(scraped) + list(FALLBACK_PDFS)
    print(f"DEBUG: {len(FALLBACK_PDFS)} fallbacks adicionados")

    # Mantém só URLs que casam OCS-NN[a-l].pdf
    ocs_urls = [u for u in pdf_urls
                if re.search(r"OCS-\d{2}[a-l]\.pdf", u, flags=re.IGNORECASE)]

    # Dedup por data (YYYY-MM-01), preferindo URL com sufixo ?v= (link oficial da página)
    by_date: dict[str, str] = {}
    for u in ocs_urls:
        try:
            d = ocs_name_to_date(u)
        except ValueError:
            continue
        if d not in by_date:
            by_date[d] = u
        elif "?v=" in u and "?v=" not in by_date[d]:
            by_date[d] = u

    pdf_urls = sorted(by_date.values(), key=ocs_name_to_date)

    targets = pdf_urls[-min(backfill_n, len(pdf_urls)):]
    print(f"\nINFO: backfill_n={backfill_n} | candidatos únicos={len(pdf_urls)} "
          f"| processando={len(targets)}")
    for t in targets:
        print(f"  -> {t}")

    added = 0
    for pdf_url in targets:
        point_date = ocs_name_to_date(pdf_url)

        if already_has_date(series, point_date):
            print(f"SKIP: date já existe no JSON: {point_date}")
            continue

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
        regime = str(result.get("regime", "")).upper().strip()
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
            "regime": regime,
            "evidencias": evid[:5],
            "pdf_url": strip_pdf_query(pdf_url),
            "pdf_sha256": pdf_sha,
            "model": MODEL,
            "prompt_version": PROMPT_VERSION,
        }

        series.append(point)
        added += 1
        print(f"OK: add {point_date} | {label} | {signal} | regime={regime}")

    if added == 0:
        print("INFO: nenhum ponto novo para adicionar.")
        return

    tmp = {str(p.get("date")): p for p in series}
    series2 = [tmp[k] for k in sorted(tmp.keys())]

    payload["series"] = series2
    payload["updated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    SERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERIES_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"OK: oil_crops_outlook.json atualizado. added={added} "
          f"| last={series2[-1]['date']}")


if __name__ == "__main__":
    main()
