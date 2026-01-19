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
from openai import OpenAI
from requests.exceptions import SSLError, HTTPError

SERIES_PATH = Path("data/cafe/series/ico_ia.json")

ICO_LIST_URL = "https://ico.org/specialized-reports/"

# Fallbacks (caso o HTML mude / scraping falhe)
FALLBACK_PDFS = [
    "http://www.ico.org/documents/cy2025-26/cmr-1225-e.pdf",
    "http://www.ico.org/documents/cy2025-26/cmr-1125-e.pdf",
]

# OpenAI
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")

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

ETAPA 3 — CONSOLIDAÇÃO DO SINAL DO RELATÓRIO
Avalie o conjunto de fatos e responda:
- O relatório como um todo é: BULLISH, BEARISH ou NEUTRAL.

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
Retorne APENAS o JSON abaixo, sem texto adicional:

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
    Tenta extrair qualquer ocorrência de cmr-####-e.pdf no HTML (absoluta ou relativa).
    Observação: no GitHub Actions, pode vir vazio (conteúdo carregado via JS / bloqueio).
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
    Pela evidência que você trouxe:
      - Oct-Dec/2025 => cy2025-26
      - Sep/2025     => cy2024-25
    Logo, o 'coffee year' vira em Outubro.
    """
    if month >= 10:
        y1 = year
        y2 = year + 1
    else:
        y1 = year - 1
        y2 = year
    return f"cy{y1}-{str(y2)[-2:]}"


def build_ico_cmr_url(year: int, month: int) -> str:
    cy = cy_folder_for_year_month(year, month)
    mmyy = f"{month:02d}{str(year)[-2:]}"
    return f"http://www.ico.org/documents/{cy}/cmr-{mmyy}-e.pdf"


def generate_candidate_cmr_urls(n_months: int, anchor: datetime | None = None) -> list[str]:
    """
    Gera URLs dos últimos n_months a partir de anchor (default: hoje UTC),
    do mais antigo para o mais recente.
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
        out.append(build_ico_cmr_url(yy, mm))
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


def call_openai(document_text: str) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY não encontrado no ambiente (GitHub Secrets).")

    client = OpenAI()
    user_prompt = USER_PROMPT_TEMPLATE.format(fonte="ICO", document_text=document_text)

    resp = client.responses.create(
        model=MODEL,
        instructions=SYSTEM_ROLE,
        input=user_prompt,
        reasoning={"effort": "low"},
    )
    out = (resp.output_text or "").strip()

    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", out, flags=re.DOTALL)
        if not m:
            raise RuntimeError(f"Resposta não veio em JSON válido.\n\nResposta bruta:\n{out[:2000]}")
        return json.loads(m.group(0))


def normalize_ico_pdf_url(u: str) -> str:
    u = u.replace("https://www.ico.org", "http://www.ico.org")
    u = u.replace("https://ico.org", "http://www.ico.org")
    return u


def try_download(url: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, timeout=90, headers=headers, allow_redirects=True)
        r.raise_for_status()
        return r.content
    except SSLError:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        r = requests.get(url, timeout=90, headers=headers, allow_redirects=True, verify=False)
        r.raise_for_status()
        content = r.content
        if not content.startswith(b"%PDF-"):
            raise RuntimeError("Fallback SSL foi usado, mas o conteúdo baixado não parece ser um PDF válido.")
        return content


# -------------------------
# Main
# -------------------------
def main():
    payload, series = load_series()

    # Backfill: por padrão roda 1 (último). Em workflow_dispatch você passa ICO_BACKFILL_N.
    backfill_n = int(os.environ.get("ICO_BACKFILL_N", "1").strip() or "1")
    backfill_n = max(1, min(backfill_n, 48))  # teto de segurança

    # 1) Descobrir PDFs disponíveis
    pdf_urls = []
    try:
        r = requests.get(ICO_LIST_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        pdf_urls = find_pdf_links_from_html(r.text, ICO_LIST_URL)
    except Exception:
        pdf_urls = []

    print(f"DEBUG: PDFs encontrados via scraping: {len(pdf_urls)}")

    # Se scraping vier vazio (caso do Actions), gera candidatos por regra
    if not pdf_urls:
        pdf_urls = generate_candidate_cmr_urls(backfill_n)
        print(f"DEBUG: usando geração por regra. candidatos={len(pdf_urls)}")

    # adiciona fallback fixo
    for u in FALLBACK_PDFS:
        if u not in pdf_urls:
            pdf_urls.append(u)

    # mantém apenas PDFs do CMR
    pdf_urls = [u for u in pdf_urls if re.search(r"cmr-\d{4}-e\.pdf", u, flags=re.IGNORECASE)]
    pdf_urls = sorted(set(pdf_urls), key=pdf_mmyy_to_date)

    # processa só os N mais recentes (backfill)
    targets = pdf_urls[-min(backfill_n, len(pdf_urls)) :]
    print(f"INFO: backfill_n={backfill_n} | encontrados={len(pdf_urls)} | processando={len(targets)}")

    added = 0
    for pdf_url in targets:
        point_date = pdf_mmyy_to_date(pdf_url)

        if already_has_date(series, point_date):
            print(f"SKIP: date já existe no JSON: {point_date}")
            continue

        pdf_url_norm = normalize_ico_pdf_url(pdf_url)

        # download resiliente: se o PDF não existir, pula
        try:
            pdf_bytes = try_download(pdf_url_norm)
        except HTTPError as e:
            print(f"SKIP: HTTP ao baixar {pdf_url_norm} | erro={e}")
            continue
        except Exception as e:
            print(f"SKIP: falha ao baixar {pdf_url_norm} | erro={e}")
            continue

        pdf_sha = sha256_bytes(pdf_bytes)

        if already_has_sha(series, pdf_sha):
            print(f"SKIP: hash já existe no JSON: {point_date}")
            continue

        raw_text = extract_pdf_text_bytes(pdf_bytes)
        doc_text = shrink_text(raw_text)
        result = call_openai(doc_text)

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
            "pdf_url": pdf_url_norm,
            "pdf_sha256": pdf_sha,
            "model": MODEL,
            "prompt_version": "v1",
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
