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

    # Ordena por impacto absoluto (mais “importantes” primeiro)
    items_sorted_abs = sorted(items, key=lambda x: abs(_safe_float(x["score_ponderado"])), reverse=True)

    # Top drivers positivos e negativos
    positives = [x for x in items_sorted_abs if _safe_float(x["score_ponderado"]) > 0.25]
    negatives = [x for x in items_sorted_abs if _safe_float(x["score_ponderado"]) < -0.25]

    # Somas por bloco (para contexto, SEM usar termômetro oficial)
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
        "Você é um analista de commodities especializado em café arábica. "
        "Você receberá um resumo estruturado das variáveis do painel (inclui score_ponderado, relevancia e papel econômico). "

        "Tarefa: escrever um diagnóstico em Português (semi-técnico) com: "
        "(1) 1 parágrafo de síntese (2–4 frases) explicando o cenário e o viés (alta/queda/lateral) "
        "separando curto prazo (tático) vs médio/longo prazo (estrutural); "
        "(2) bullets curtos com os porquês, priorizando apenas as variáveis de relevância ALTA e MEDIA; "
        "(3) uma linha final listando as variáveis de relevância BAIXA como 'impacto limitado no momento' (sem explicar nível/tendência/momento). "

        "REGRAS OBRIGATÓRIAS: "
        "- NÃO descreva nível/tendência/momento literalmente para todas as variáveis. "
        "Use essas dimensões apenas quando a variável tiver relevância ALTA ou MEDIA e de forma natural (sem jargão repetitivo). "
        "- A variável PREÇO ARABICA é DEPENDENTE: NÃO pode ser tratada como causa, "
        "NÃO pode aparecer como vetor altista/baixista. "
        "Você pode mencioná-la apenas como confirmação/timing no parágrafo de síntese, e nunca como explicação causal. "
        "- Você DEVE considerar TODAS as variáveis: "
        "ALTA/MEDIA entram nos bullets explicados; BAIXA entra só na linha de 'impacto limitado'. "
        "- Interprete o sentido econômico respeitando o sinal do score_ponderado: "
        "score_ponderado positivo = força altista; negativo = força baixista. "
        "NÃO produza frases logicamente incoerentes como 'preço em queda pressiona custos' ou 'queda de fertilizante pressiona custos'. "
        "Se fertilizante estiver em queda, trate como redução de custo (o efeito no preço deve ser coerente com o sinal do score_ponderado fornecido). "
        "- Confiança: se houver forças altistas e baixistas relevantes simultaneamente, a confiança NÃO pode ser ALTA. "

        "Saída obrigatoriamente em JSON estrito com as chaves: "
        "{'summary': string, "
        "'drivers_bull': [string,...], "
        "'drivers_bear': [string,...], "
        "'limited_impact': [string,...], "
        "'bias': 'ALTA'|'QUEDA'|'LATERAL', "
        "'confidence': 'BAIXA'|'MEDIA'|'ALTA'}."
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
    # Caso algum wrapper forneça output_text diretamente
    if isinstance(resp, dict) and isinstance(resp.get("output_text"), str) and resp["output_text"].strip():
        return resp["output_text"].strip()

    # Forma comum: resp["output"] -> itens -> content -> {type:"output_text", text:"..."}
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

    # fallback: tenta olhar campos conhecidos
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
    if start >= 0 and end > start:
        candidate = text[start:end+1]
        return json.loads(candidate)

    raise ValueError("Não foi possível interpretar a resposta como JSON.")


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

    bias = str(obj["bias"]).strip().upper()
    conf = str(obj["confidence"]).strip().upper()

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

    # Garante que estamos usando todas as variáveis presentes
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
