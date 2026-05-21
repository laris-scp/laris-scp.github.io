#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gera um diagnóstico textual (IA) para o Painel da Soja, interpretando as
variáveis do snapshot.
- Lê:  data/soja/painel_snapshot.json
- Grava: data/soja/ai_diagnostico.json
- NÃO usa histórico de termômetro
- NÃO usa termômetros (bloco_1/bloco_2/geral)
- Usa todas as variáveis presentes em rows[]

Espelha scripts/cafe/update_ai_diagnostico.py, com taxonomia e regras
econômicas reescritas para as variáveis da soja.
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

import anthropic


SNAPSHOT_PATH = Path("data/soja/painel_snapshot.json")
OUT_PATH = Path("data/soja/ai_diagnostico.json")

# Anthropic Claude
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# Limites para controlar custo
MAX_OUTPUT_TOKENS = 1500

# Taxonomia econômica (embutida no prompt) — variáveis da SOJA
ECONOMIC_TAXONOMY = {
    "preco_soja": "Preço (variável dependente / timing / reflexividade do mercado)",
    "usd_brl": "Macro (amplificador de preço em BRL e competitividade/exportações)",
    "cot_report": "Posicionamento financeiro (fundos/Managed Money)",
    "crush_spread": "Demanda derivada (margem de esmagamento — incentivo industrial)",
    "crop_condition": "Risco de oferta (saúde da lavoura de soja dos EUA)",
    "mdic_export": "Fluxo efetivo (exportação de soja em grão do Brasil)",
    "fundamental_stu": "Oferta estrutural global (stock-to-use)",
    "oil_crops_outlook": "Síntese qualitativa externa (relatório Oil Crops Outlook via IA)",
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
        is_dependent = (vid == "preco_soja")

        peso = _safe_float(r.get("peso"), 0.0)
        bloco = r.get("bloco", None)

        # score_ponderado "econômico": desfaz a inversão de sinal do bloco 2.
        # No painel, o bloco 2 aplica mult_bloco = -1, então o score_ponderado
        # de uma variável de bloco 2 vem com o sinal invertido em relação à
        # sua direção econômica real. Aqui revertemos isso para que o agente
        # interprete sempre no sentido correto para o preço.
        try:
            bloco_int = int(bloco)
        except Exception:
            bloco_int = None
        sp_economico = (-sp) if bloco_int == 2 else sp

        # Direção de contribuição: baseada no score_ponderado ECONÔMICO
        # (>0 bullish, <0 bearish, ~0 neutro)
        if sp_economico > 0.25:
            contrib = "BULLISH"
        elif sp_economico < -0.25:
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
            "score_ponderado_economico": sp_economico,
            "contribuicao": contrib,
            "relevancia": relev,
            "dependente": is_dependent,
        })

    items_sorted_abs = sorted(items, key=lambda x: abs(_safe_float(x["score_ponderado_economico"])), reverse=True)

    positives = [x for x in items_sorted_abs if _safe_float(x["score_ponderado_economico"]) > 0.25]
    negatives = [x for x in items_sorted_abs if _safe_float(x["score_ponderado_economico"]) < -0.25]

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
    Retorna (system, user_content).
    Mantém o payload pequeno para reduzir custo.
    """
    updated_at = snapshot.get("updated_at", "")
    commodity = snapshot.get("commodity", "soja")

    system = (
        "Você é um analista de commodities especializado em soja.\n\n"
        "Tarefa:\n"
        "Gerar um diagnóstico econômico interpretativo para a tendência futura do preço da soja com base nas variáveis fornecidas.\n\n"
        "FORMATO OBRIGATÓRIO (JSON estrito, sem markdown, sem ```json, sem texto adicional):\n"
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
        "- crush_spread → Margem de Esmagamento\n"
        "- crop_condition → Condição da Lavoura dos EUA\n"
        "- cot_report → Posição de fundos na soja\n"
        "- mdic_export → Exportações de soja em grão do Brasil\n"
        "- oil_crops_outlook → Relatório Oil Crops Outlook\n"
        "- preco_soja → Preço da soja (ver regra específica abaixo)\n\n"
        "Nunca use os nomes técnicos (ids) no texto final.\n\n"
        "1) Estoques Globais (STU Global / fundamental_stu)\n"
        "- Estoques globais baixos indicam aperto estrutural de oferta e são altistas para o preço no médio e longo prazo.\n"
        "- Queda dos estoques globais reforça o viés altista.\n"
        "- Desaceleração da queda mantém o viés altista, porém com menor força marginal.\n"
        "- Nunca use os termos “otimista” ou “pessimista” para estoques.\n"
        "- Use sempre “altista” ou “baixista para o preço”.\n\n"
        "2) Dólar (USD/BRL)\n"
        "- Dólar em queda (real forte) gera viés baixista no curto prazo, pois reduz o preço em reais, "
        "reduz o incentivo do exportador e reduz a sustentação nominal do mercado.\n"
        "- Dólar em alta (real fraco) gera viés altista no curto prazo.\n"
        "- É terminantemente proibido afirmar que dólar em queda favorece exportações.\n"
        "- O Dólar é um amplificador de curto prazo, não estrutural.\n\n"
        "3) Exportações do Brasil (MDIC)\n"
        "- Exportações fortes ou acelerando tendem a ser altistas, pois indicam maior fluxo efetivo de demanda no curto prazo.\n"
        "- Exportações fracas ou desacelerando tendem a ser baixistas, por indicarem fraqueza da demanda de curto prazo.\n"
        "- Esta variável deve ser interpretada exclusivamente como fluxo físico de oferta/demanda.\n\n"
        "4) Posição de fundos na soja (COT)\n"
        "- Aumento de posições compradas dos fundos é altista.\n"
        "- Redução de posições ou aumento de posições vendidas é baixista.\n"
        "- Quando neutra, exerce impacto limitado.\n"
        "- Atua principalmente em timing e volatilidade.\n\n"
        "5) Margem de Esmagamento (crush_spread)\n"
        "- Margem de esmagamento alta ou subindo é altista: indica esmagador com lucro gordo, "
        "comprando mais soja, demanda industrial forte.\n"
        "- Margem baixa ou caindo é baixista: indica esmagador recuando, demanda industrial fraca.\n"
        "- É a variável que captura a demanda derivada (incentivo industrial) em tempo real.\n\n"
        "6) Condição da Lavoura dos EUA (crop_condition)\n"
        "- Lavoura em boa condição (percentual Good+Excellent alto ou melhorando) é baixista, "
        "pois sinaliza expectativa de safra grande.\n"
        "- Lavoura em má condição ou deteriorando é altista, pois sinaliza risco de oferta menor.\n"
        "- Fora da janela de safra dos EUA esta variável fica neutra e deve ser tratada como impacto limitado.\n\n"
        "7) Relatório Oil Crops Outlook (oil_crops_outlook)\n"
        "- É uma síntese qualitativa do relatório mensal do USDA, já classificada como altista, baixista ou neutra.\n"
        "- Quando indica viés altista (aperto de oferta, demanda forte), trate como altista.\n"
        "- Quando indica viés baixista (oferta ampla, safra recorde), trate como baixista.\n"
        "- Quando neutro, deve ser tratado como impacto limitado e apenas validação cruzada.\n"
        "- Não reinterprete o relatório: apenas reflita a classificação já atribuída.\n\n"
        "8) Preço da soja (regra crítica)\n"
        "- O preço da soja é uma variável dependente, ou seja, nunca deve ser utilizada e mencionada na lista de drivers_bull ou drivers_bear ou limited_impact.\n"
        "- É proibido usá-lo como causa, driver, justificativa ou explicação.\n"
        "- O preço da soja deverá ser utilizado como uma consequência de todas as outras variáveis acima, independente do peso dela.\n"
        "- O resultado direcional deve ser expresso exclusivamente por bias e confidence.\n"
    )

    user_content = {
        "contexto": {
            "commodity": commodity,
            "updated_at": updated_at,
            "observacao": "Use SEMPRE o campo 'score_ponderado_economico' e 'contribuicao' como medida de direção de cada variável: eles já estão no sentido econômico correto para o preço da soja (positivo = altista, negativo = baixista). O campo 'score_ponderado' bruto NÃO deve ser usado para julgar direção, pois variáveis do bloco 2 vêm com o sinal invertido pela mecânica do painel. A magnitude (valor absoluto) indica a relevância.",
        },
        "resumo": {
            "n_variaveis": summary["n_variaveis"],
            "sum_score_ponderado_por_bloco": summary["sum_score_ponderado_por_bloco"],
            "top_positivas": summary["top_positivas"],
            "top_negativas": summary["top_negativas"],
        },
        "variaveis_ordenadas_por_impacto": summary["variaveis"],
    }

    return system, json.dumps(user_content, ensure_ascii=False)


def _claude_call(system: str, user_text: str) -> str:
    """
    Chama Claude via SDK Anthropic. Retorna o texto bruto da resposta.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY não encontrado no ambiente. Configure o secret no GitHub Actions.")

    client = anthropic.Anthropic()

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_text}],
    )

    out = ""
    for block in resp.content:
        if hasattr(block, "text"):
            out += block.text
    return out.strip()


def _parse_strict_json(text: str) -> Dict[str, Any]:
    """
    Espera JSON estrito. Se o modelo vier com lixo, tenta recuperar o objeto JSON principal.
    """
    text = text.strip()
    if not text:
        raise ValueError("Resposta vazia do modelo.")

    try:
        return json.loads(text)
    except Exception:
        pass

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

    system, user_text = _build_prompt(snapshot, summary)
    out_text = _claude_call(system, user_text)

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
