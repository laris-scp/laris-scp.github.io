#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin

import requests
import pdfplumber
from openai import OpenAI

SERIES_PATH = Path("data/cafe/series/ico_ia.json")

ICO_LIST_URL = "https://ico.org/specialized-reports/"

# Fallbacks (se o HTML do ICO mudar)
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

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def extract_pdf_text_bytes(pdf_bytes: bytes) -> str:
    # abre PDF a partir de bytes
    import io
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
    # pega todos os href .pdf
    hrefs = re.findall(r'href=["\']([^"\']+\.pdf)["\']', html, flags=re.IGNORECASE)
    urls = [urljoin(base_url, h) for h in hrefs]
    # filtra PDFs do CMR em inglês: cmr-<MMYY>-e.pdf
    out = []
    for u in urls:
        if re.search(r"/cmr-\d{4}-e\.pdf$", u, flags=re.IGNORECASE):
            out.append(u)
    return sorted(set(out))

def pdf_mmyy_to_date(url: str) -> str:
    # extrai mmYY do filename
    m = re.search(r"cmr-(\d{2})(\d{2})-e\.pdf$", url, flags=re.IGNORECASE)
    if not m:
        raise ValueError(f"Não consegui extrair MMYY do PDF: {url}")
    mm = int(m.group(1))
    yy = int(m.group(2))
    year = 2000 + yy
    # padroniza para 1o dia do mês
    return f"{year:04d}-{mm:02d}-01"

def load_series():
    if SERIES_PATH.exists():
        payload = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
        series = payload.get("series", [])
        if not isinstance(series, list):
            series = []
        return payload, series
    else:
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

    # JSON estrito ou extração do primeiro objeto
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", out, flags=re.DOTALL)
        if not m:
            raise RuntimeError(f"Resposta não veio em JSON válido.\n\nResposta bruta:\n{out[:2000]}")
        return json.loads(m.group(0))

def main():
    payload, series = load_series()

    # 1) Descobrir PDFs disponíveis
    pdf_urls = []
    try:
        r = requests.get(ICO_LIST_URL, timeout=30)
        r.raise_for_status()
        pdf_urls = find_pdf_links_from_html(r.text, ICO_LIST_URL)
    except Exception:
        pdf_urls = []

    # fallback se scraping falhar
    for u in FALLBACK_PDFS:
        if u not in pdf_urls:
            pdf_urls.append(u)

    # sanity
    pdf_urls = [u for u in pdf_urls if re.search(r"cmr-\d{4}-e\.pdf$", u, flags=re.IGNORECASE)]
    if not pdf_urls:
        raise RuntimeError("Não encontrei nenhum PDF do CMR para processar (nem por scraping, nem por fallback).")

    # 2) Ordena por data (YYYY-MM) derivada do filename e escolhe o mais recente
    def key_date(u: str):
        d = pdf_mmyy_to_date(u)  # YYYY-MM-01
        return d
    pdf_urls = sorted(set(pdf_urls), key=key_date)
    latest_pdf = pdf_urls[-1]
    latest_date = pdf_mmyy_to_date(latest_pdf)

    # 3) Baixa PDF e calcula hash
    pdf_resp = requests.get(latest_pdf, timeout=60)
    pdf_resp.raise_for_status()
    pdf_bytes = pdf_resp.content
    pdf_sha = sha256_bytes(pdf_bytes)

    if already_has_sha(series, pdf_sha):
        print("SKIP: PDF já processado (hash igual).")
        return

    # 4) Extrai texto e chama OpenAI
    raw_text = extract_pdf_text_bytes(pdf_bytes)
    doc_text = shrink_text(raw_text)

    result = call_openai(doc_text)

    # validações mínimas
    signal = float(result.get("signal"))
    label = str(result.get("label", "")).upper().strip()
    evid = result.get("evidencias", [])
    if label not in {"BULLISH", "BEARISH", "NEUTRAL"}:
        raise RuntimeError(f"Label inválido retornado: {label}")
    if signal not in (-1.0, 0.0, 1.0):
        raise RuntimeError(f"Signal inválido retornado: {signal}")

    point = {
        "date": latest_date,
        "close": signal,                 # para o gráfico do site (linha única)
        "signal": signal,
        "label": label,
        "evidencias": evid[:5],
        "pdf_url": latest_pdf,
        "pdf_sha256": pdf_sha,
        "model": MODEL,
        "prompt_version": "v1",
    }

    series.append(point)

    # ordena e dedup por date (mantém o último)
    tmp = {}
    for p in series:
        tmp[str(p.get("date"))] = p
    series2 = [tmp[k] for k in sorted(tmp.keys())]

    payload["series"] = series2
    payload["updated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    SERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERIES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"OK: ico_ia.json atualizado. Último ponto={latest_date} label={label} signal={signal}")

if __name__ == "__main__":
    main()
