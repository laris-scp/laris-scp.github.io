#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gera um diagnóstico textual (IA) para o Painel do Café, interpretando as variáveis do snapshot.
- Lê:  data/cafe/painel_snapshot.json
- Grava: data/cafe/ai_diagnostico.json
- NÃO usa histórico de termômetro
- NÃO usa termômetros (bloco_1/bloco_2/geral)
- Usa todas as variáveis presentes em rows[]
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import request, error


SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")
OUT_PATH = Path("data/cafe/ai_diagnostico.json")

OPENAI_API_URL = "https://api.openai.com/v1/responses"
MODEL = "gpt-4o-mini"

# Limites para controlar custo
MAX_OUTPUT_TOKENS = 650
TEMPERATURE = 0.2

ECONOMIC_TAXONOMY = {
    "preco_arabica": "Preço (variável dependente / timing)",
    "usd_brl": "Macro (câmbio / competitividade)",
    "cot_report": "Posicionamento financeiro (fundos)",
    "clima_soil": "Risco climático (oferta futura)",
    "fertilizante_urea": "Custo marginal de produção",
    "gscpi": "Fricção logística",
    "mdic_export": "Fluxo de exportação Brasil",
    "estoques_certificados": "Oferta imediata (ICE)",
    "fundamental_stu": "Oferta estrutural global (STU)",
    "ico_ia": "Leitura qualitativa externa (ICO)",
}


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _summarize_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    items = []

    for r in rows:
        vid = r.get("id", "")
        sp = _safe_float(r.get("score_ponderado"), 0.0)
        abs_sp = abs(sp)

        if abs_sp >= 6:
            relev = "ALTA"
        elif abs_sp >= 2:
            relev = "MEDIA"
        else:
            relev = "BAIXA"

        is_dependent = (vid == "preco_arabica")

        if sp > 0.25:
            contrib = "BULLISH"
        elif sp < -0.25:
            contrib = "BEARISH"
        else:
            contrib = "NEUTRO"

        items.append({
            "id": vid,
            "nome": r.get("variavel", vid),
            "papel_economico": ECONOMIC_TAXONOMY.get(vid, "Não classificado"),
            "frequencia": r.get("frequencia"),
            "ultima_atualizacao": r.get("ultima_atualizacao"),
            "score_ponderado": sp,
            "contribuicao": contrib,
            "relevancia": relev,
            "dependente": is_dependent,
        })

    items_sorted = sorted(items, key=lambda x: abs(x["score_ponderado"]), reverse=True)

    positives = [x for x in items_sorted if x["score_ponderado"] > 0.25]
    negatives = [x for x in items_sorted if x["score_ponderado"] < -0.25]

    return {
        "n_variaveis": len(items),
        "top_positivas": positives[:4],
        "top_negativas": negatives[:4],
        "variaveis": items_sorted,
    }


def _build_prompt(snapshot: Dict[str, Any], summary: Dict[str, Any]) -> Tuple[str, str]:
    instructions = (
        "Você é um analista de commodities especializado em café arábica. "
        "Você receberá um resumo estruturado das variáveis do painel. "

        "Tarefa: escrever um diagnóstico em Português (semi-técnico) com: "
        "(1) um parágrafo de síntese separando curto prazo (tático) e médio/longo prazo (estrutural); "
        "(2) bullets explicando apenas variáveis de relevância ALTA e MEDIA; "
        "(3) uma linha final listando variáveis de impacto limitado. "

        "REGRAS OBRIGATÓRIAS: "
        "- NÃO descreva nível/tendência/momento literalmente. "
        "- PREÇO ARABICA é variável DEPENDENTE: não pode ser causa, nem driver. "
        "- Use score_ponderado como direção econômica (positivo = altista, negativo = baixista). "
        "- Variáveis de relevância BAIXA entram apenas como impacto limitado. "
        "- Se houver forças altistas e baixistas relevantes, confiança não pode ser ALTA. "

        "Saída em JSON estrito com: "
        "{summary, drivers_bull, drivers_bear, limited_impact, bias, confidence}."
    )

    user_content = {
        "contexto": {
            "updated_at": snapshot.get("updated_at"),
        },
        "resumo": summary,
    }

    return instructions, json.dumps(user_content, ensure_ascii=False)


def _openai_call(instructions: str, user_text: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada.")

    payload = {
        "model": MODEL,
        "instructions": instructions,
        "input": [{"role": "user", "content": user_text}],
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "temperature": TEMPERATURE,
    }

    req = request.Request(
        OPENAI_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_output_text(resp: Dict[str, Any]) -> str:
    if isinstance(resp.get("output_text"), str):
        return resp["output_text"].strip()

    for item in resp.get("output", []):
        for c in item.get("content", []):
            if c.get("type") in ("output_text", "text"):
                return c.get("text", "").strip()

    raise ValueError("Texto não encontrado na resposta da OpenAI.")


def _parse_strict_json(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _validate_schema(obj: Dict[str, Any]) -> Dict[str, Any]:
    required = ["summary", "drivers_bull", "drivers_bear", "limited_impact", "bias", "confidence"]
    for k in required:
        if k not in obj:
            raise ValueError(f"Campo ausente: {k}")

    return {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "summary": obj["summary"],
        "drivers_bull": obj["drivers_bull"],
        "drivers_bear": obj["drivers_bear"],
        "limited_impact": obj["limited_impact"],
        "bias": obj["bias"],
        "confidence": obj["confidence"],
    }


def main() -> int:
    snapshot = _load_json(SNAPSHOT_PATH)
    rows = snapshot.get("rows", [])

    if not rows:
        raise RuntimeError("Snapshot sem variáveis.")

    summary = _summarize_rows(rows)

    instructions, user_text = _build_prompt(snapshot, summary)
    resp = _openai_call(instructions, user_text)

    text = _extract_output_text(resp)
    parsed = _parse_strict_json(text)
    final_obj = _validate_schema(parsed)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(final_obj, f, ensure_ascii=False, indent=2)

    print(f"OK: ai_diagnostico gerado em {OUT_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERRO: {e}", file=sys.stderr)
        raise
