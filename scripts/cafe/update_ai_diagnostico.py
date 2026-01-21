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

import unicodedata
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
MAX_OUTPUT_TOKENS = 650  # suficiente para parágrafo + bullets
TEMPERATURE = 0.2        # mais estável / menos aleatório

# Taxonomia econômica (embutida no prompt, como você aprovou)
ECONOMIC_TAXONOMY = {
    "preco_arabica": "Preço (timing / reflexividade do mercado)",
    "usd_brl": "Macro (amplificador de preço em BRL e competitividade/exportações)",
    "cot_report": "Posicionamento financeiro (fundos/Managed Money)",
    "clima_soil": "Risco de oferta futura (clima/estresse hídrico)",
    "fertilizante_urea": "Custo marginal de produção (insumo)",
    "gscpi": "Fricção logística (frete/cadeia global)",
    "mdic_export": "Fluxo efetivo (exportação Brasil)",
    "estoques_certificados": "Oferta disponível imediata (estoques ICE)",
    "fundamental_stu": "Oferta estrutural global (stock-to-use)",
    "ico_ia": "Síntese qualitativa externa (relatório ICO via IA)",
}


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {path.as_posix()}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _summarize_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Cria um resumo compacto para reduzir tokens e custo:
    - ranking de contribuições (score_ponderado)
    - direção implícita por variável (a partir do score_ponderado)
    - metadados úteis (frequência, última atualização)
    """
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

        # PREÇO é variável dependente (não pode ser driver causal)
        is_dependent = (vid == "preco_arabica")

        peso = _safe_float(r.get("peso"), 0.0)
        bloco = r.get("bloco", None)

        # Direção de contribuição: >0 bullish, <0 bearish, ~0 neutro
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
            "bloco": bloco,
            "frequencia": r.get("frequencia"),
            "ultima_atualizacao": r.get("ultima_atualizacao"),
            "ultimo_valor": r.get("ultimo_valor"),
            "nivel": r.get("nivel"),
            "tendencia": r.get("tendencia"),
            "momento": r.get("momento"),
            "peso": peso,
            "score": _safe_float(r.get("score"), 0.0),
            "score_ponderado": sp,
            "contribuicao": contrib,
            "relevancia": relev,
            "dependente": is_dependent,
        })

    items_sorted_abs = sorted(items, key=lambda x: abs(_safe_float(x["score_ponderado"])), reverse=True)

    positives = [x for x in items_sorted_abs if _safe_float(x["score_ponderado"]) > 0.25]
    negatives = [x for x in items_sorted_abs if _safe_float(x["score_ponderado"]) < -0.25]

    sum_by_block: Dict[str, float] = {}
    for x in items:
        b = str(x.get("bloco"))
        sum_by_block[b] = sum_by_block.get(b, 0.0) + _safe_float(x["score_ponderado"])

    return {
        "n_variaveis": len(items),
        "sum_score_ponderado_por_bloco": sum_by_block,
        "top_positivas": positives[:4],
        "top_negativas": negatives[:4],
        "variaveis": items_sorted_abs,
    }


def _build_prompt(snapshot: Dict[str, Any], summary: Dict[str, Any]) -> Tuple[str, str]:
    """
    Retorna (instructions, user_content).
    Mantém o payload pequeno para reduzir custo.
    """
    updated_at = snapshot.get("updated_at", "")
    commodity = snapshot.get("commodity", "cafe")

    instructions = (
        "Você é um analista de commodities especializado em café arábica.\n\n"
        "Tarefa:\n"
        "Gerar um diagnóstico econômico interpretativo para a tendência futura do preço do café com base nas variáveis fornecidas.\n\n"
        "FORMATO OBRIGATÓRIO (JSON estrito):\n"
        "{\n"
        '  "summary": "...",\n'
        '  "drivers_bull": ["..."],\n'
        '  "drivers_bear": ["..."],\n'
        '  "limited_impact": ["..."],\n'
        '  "bias": "ALTA|QUEDA|LATERAL",\n'
        '  "confidence": "BAIXA|MEDIA|ALTA"\n'
        "}\n\n"
        "REGRAS ECONÔMICAS OBRIGATÓRIAS:\n\n"
        "Você deve interpretar as variáveis exclusivamente conforme as regras abaixo. "
        "Não improvise, não use interpretações alternativas e não inverta causalidades.\n\n"
        "Padronização de nomes (obrigatória):\n"
        "Ao mencionar as variáveis no texto, use sempre os nomes abaixo:\n\n"
        "- usd_brl → Dólar\n"
        "- fundamental_stu → Estoques Globais\n"
        "- estoques_certificados → Estoques Certificados na ICE\n"
        "- fertilizante_urea → Fertilizante (ureia)\n"
        "- gscpi → Fretes\n"
        "- clima_soil → Clima ou Estresse hídrico\n"
        "- cot_report → Posição de fundos no café\n"
        "- mdic_export → Exportações de café verde do Brasil\n"
        "- ico_ia → Relatório da ICO\n"
        "- preco_arabica → Preço do café arábica (ver regra específica abaixo)\n\n"
        "Nunca use os nomes técnicos (ids) no texto final.\n\n"
        "1) Estoques Globais (STU Global)\n"
        "- Estoques globais baixos indicam aperto estrutural de oferta e são altistas para o preço no médio e longo prazo.\n"
        "- Queda dos estoques globais reforça o viés altista.\n"
        "- Desaceleração da queda mantém o viés altista, porém com menor força marginal.\n"
        "- Nunca use os termos “otimista” ou “pessimista” para estoques.\n"
        "- Use sempre “altista” ou “baixista para o preço”.\n\n"
        "2) Estoques Certificados na ICE\n"
        "- Estoques certificados muito baixos são altistas no curto prazo, pois indicam oferta disponível restrita.\n"
        "- Estoques baixos porém lateralizados continuam altistas estruturalmente, mas não geram novo impulso direcional.\n"
        "- Esta variável representa disponibilidade imediata, não expectativa futura.\n\n"
        "3) Dólar (USD/BRL)\n"
        "- Dólar em queda (real forte) gera viés baixista no curto prazo, pois reduz o preço em reais, "
        "reduz o incentivo do exportador e reduz a sustentação nominal do mercado.\n"
        "- Dólar em alta (real fraco) gera viés altista no curto prazo.\n"
        "- É terminantemente proibido afirmar que dólar em queda favorece exportações.\n"
        "- O Dólar é um amplificador de curto prazo, não estrutural.\n\n"
        "4) Exportações do Brasil (MDIC)\n"
        "- Exportações fortes ou acelerando tendem a ser altistas, pois indicam maior fluxo efetivo no curto prazo\n"
        "- Exportações fracas ou desacelerando tendem a ser baixistas, por indicarem frqueza da demanda de curto prazo.\n"
        "- Esta variável deve ser interpretada exclusivamente como fluxo físico de oferta.\n\n"
        "5) Posição de fundos no café (COT)\n"
        "- Aumento de posições compradas dos fundos é altista.\n"
        "- Redução de posições ou aumento de posições vendidas é baixista.\n"
        "- Quando neutra, exerce impacto limitado.\n"
        "- Atua principalmente em timing e volatilidade.\n\n"
        "6) Clima / Estresse hídrico\n"
        "- Estresse hídrico elevado é altista estrutural, pois aumenta o risco de quebra de safra futura.\n"
        "- Clima normal ou sem estresse relevante é neutro a levemente baixista, pois remove prêmio de risco.\n"
        "- Atua com defasagem temporal e só deve ser driver quando o risco for relevante.\n\n"
        "7) Fertilizante\n"
        "- Fertilizante caro é altista estrutural, pois eleva o custo marginal e pode reduzir oferta futura.\n"
        "- Fertilizante em queda é baixista, pois alivia custos e facilita a produção.\n"
        "- Nunca escreva frases incoerentes como “queda do fertilizante pressiona custos”.\n\n"
        "8) Fretes (GSCPI)\n"
        "- Fretes caros ou fricção logística elevada são altistas, pois dificultam o escoamento e aumentam o custo all-in.\n"
        "- Normalização de fretes é baixista, pois facilita a oferta.\n"
        "- Fretes altos porém estáveis devem ser tratados como impacto marginal reduzido.\n\n"
        "9) Relatório da ICO\n"
        "- Quando indica aperto de oferta ou riscos relevantes, é altista.\n"
        "- Quando indica conforto de oferta ou normalização, é baixista.\n"
        "- Quando neutro, deve ser tratado como impacto limitado e apenas validação cruzada.\n\n"
        "10) Preço do café arábica (regra crítica)\n"
        "- O preço do café arábica é uma variável dependente.\n"
        "- É proibido usá-lo como causa, driver, justificativa ou explicação.\n"
        "- O preço não deve ser mencionado em summary, drivers ou limited_impact.\n"
        "- O resultado direcional deve ser expresso exclusivamente por bias e confidence.\n"
    )

    user_content = {
        "contexto": {
            "commodity": commodity,
            "updated_at": updated_at,
            "observacao": "O score_ponderado já incorpora peso e lógica do painel; use-o como medida de contribuição.",
        },
        "resumo": {
            "n_variaveis": summary["n_variaveis"],
            "sum_score_ponderado_por_bloco": summary["sum_score_ponderado_por_bloco"],
            "top_positivas": summary["top_positivas"],
            "top_negativas": summary["top_negativas"],
        },
        "variaveis_ordenadas_por_impacto": summary["variaveis"],
    }

    return instructions, json.dumps(user_content, ensure_ascii=False)


def _openai_call(instructions: str, user_text: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY não encontrado no ambiente. Configure o secret no GitHub Actions.")

    payload = {
        "model": MODEL,
        "instructions": instructions,
        "input": [
            {"role": "user", "content": user_text}
        ],
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "temperature": TEMPERATURE,
        "store": False,
    }

    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        OPENAI_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        raise RuntimeError(f"Erro HTTP OpenAI: {e.code} {e.reason} | body={body[:2000]}")
    except Exception as e:
        raise RuntimeError(f"Falha ao chamar OpenAI: {e}")


def _extract_output_text(resp: Dict[str, Any]) -> str:
    """
    Extrai texto do Responses API de forma robusta.
    """
    if isinstance(resp, dict) and isinstance(resp.get("output_text"), str) and resp["output_text"].strip():
        return resp["output_text"].strip()

    out = resp.get("output")
    if isinstance(out, list):
        chunks: List[str] = []
        for item in out:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                    chunks.append(c["text"])
        txt = "\n".join(chunks).strip()
        if txt:
            return txt

    return ""


def _parse_strict_json(text: str) -> Dict[str, Any]:
    """
    Espera JSON estrito. Se o modelo vier com lixo, tenta recuperar o objeto JSON principal.
    """
    text = text.strip()
    if not text:
        raise ValueError("Resposta vazia do modelo.")

    # Tentativa direta
    try:
        return json.loads(text)
    except Exception:
        pass

    # Recupera o primeiro bloco JSON entre { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Não foi possível localizar bloco JSON na resposta.")

    candidate = text[start:end + 1].strip()
    return json.loads(candidate)
    
def _normalize_enum(s: str) -> str:
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.upper()


def _validate_schema(obj: Dict[str, Any]) -> Dict[str, Any]:
    required = ["summary", "drivers_bull", "drivers_bear", "limited_impact", "bias", "confidence"]
    for k in required:
        if k not in obj:
            raise ValueError(f"Campo obrigatório ausente: {k}")

    summary = str(obj["summary"]).strip()

    bull = obj["drivers_bull"]
    bear = obj["drivers_bear"]
    lim = obj["limited_impact"]

    if not isinstance(bull, list) or not isinstance(bear, list) or not isinstance(lim, list):
        raise ValueError("drivers_bull, drivers_bear e limited_impact devem ser listas.")

    bull = [str(x).strip() for x in bull if str(x).strip()]
    bear = [str(x).strip() for x in bear if str(x).strip()]
    lim = [str(x).strip() for x in lim if str(x).strip()]

    bias = _normalize_enum(str(obj["bias"]))
    conf = _normalize_enum(str(obj["confidence"]))

    if bias not in ("ALTA", "QUEDA", "LATERAL"):
        raise ValueError("bias inválido. Use: ALTA | QUEDA | LATERAL")
    if conf not in ("BAIXA", "MEDIA", "ALTA"):
        raise ValueError("confidence inválido. Use: BAIXA | MEDIA | ALTA")

    return {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "summary": summary,
        "drivers_bull": bull[:8],
        "drivers_bear": bear[:8],
        "limited_impact": lim[:10],
        "bias": bias,
        "confidence": conf,
    }


def main() -> int:
    t0 = time.time()

    snapshot = _load_json(SNAPSHOT_PATH)
    rows = snapshot.get("rows", [])
    if not isinstance(rows, list) or len(rows) == 0:
        raise RuntimeError("painel_snapshot.json não contém rows válidos.")

    summary = _summarize_rows(rows)

    instructions, user_text = _build_prompt(snapshot, summary)
    resp = _openai_call(instructions, user_text)

    out_text = _extract_output_text(resp)
    parsed = _parse_strict_json(out_text)
    final_obj = _validate_schema(parsed)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(final_obj, f, ensure_ascii=False, indent=2)

    dt = time.time() - t0
    print(f"OK: ai_diagnostico gerado em {OUT_PATH.as_posix()} ({dt:.2f}s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERRO: {e}", file=sys.stderr)
        raise
