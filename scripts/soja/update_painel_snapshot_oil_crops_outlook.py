#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atualiza data/soja/painel_snapshot.json com a variável Oil Crops Outlook (IA).

Lê o último ponto de data/soja/series/oil_crops_outlook.json e escreve/atualiza
a row correspondente no snapshot, no mesmo formato das demais variáveis do
painel da soja. Espelha scripts/cafe/update_painel_snapshot_ico_ia.py.
"""

import json
from pathlib import Path
from datetime import datetime


SERIES_PATH = Path("data/soja/series/oil_crops_outlook.json")
SNAPSHOT_PATH = Path("data/soja/painel_snapshot.json")

VAR_ID = "oil_crops_outlook"
VAR_NAME = "OIL CROPS OUTLOOK (IA)"
BLOCO = 1          # bloco 1: sem inversão de sinal (igual ICO do café)
PESO = 1.0


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def pick_latest_point(series: list[dict]) -> dict:
    if not series:
        raise RuntimeError("oil_crops_outlook.json inválido: 'series' vazia.")

    series_sorted = sorted(series, key=lambda x: str(x.get("date", "")))
    last = series_sorted[-1]

    for k in ("date", "signal", "label"):
        if k not in last:
            raise RuntimeError(
                f"oil_crops_outlook.json inválido: último ponto sem campo '{k}'."
            )
    return last


def main():
    # 1) Lê série
    s = load_json(SERIES_PATH)
    series = s.get("series", [])
    if not isinstance(series, list):
        raise RuntimeError("oil_crops_outlook.json inválido: 'series' não é lista.")

    last = pick_latest_point(series)

    last_date = str(last["date"])
    signal = float(last["signal"])
    label = str(last["label"]).upper().strip()

    if label not in {"BULLISH", "BEARISH", "NEUTRAL"}:
        raise RuntimeError(f"Label inválido no oil_crops_outlook.json: {label}")
    if signal not in (-1.0, 0.0, 1.0):
        raise RuntimeError(f"Signal inválido no oil_crops_outlook.json: {signal}")

    # 2) Lê snapshot
    snap = load_json(SNAPSHOT_PATH)
    if "rows" not in snap or not isinstance(snap["rows"], list):
        raise RuntimeError("painel_snapshot.json inválido: não encontrei 'rows'.")

    rows = snap["rows"]

    # 3) Procura row existente (se não existir, cria)
    row = None
    for r in rows:
        if r.get("id") == VAR_ID:
            row = r
            break

    if row is None:
        row = {
            "id": VAR_ID,
            "bloco": BLOCO,
            "variavel": VAR_NAME,
            "peso": PESO,
        }
        rows.append(row)

    # 4) Cálculo do score (qualitativo: o signal já é o score base)
    bloco = int(row.get("bloco", BLOCO))
    mult_bloco = 1.0 if bloco == 1 else (-1.0 if bloco == 2 else 1.0)
    peso = float(row.get("peso", PESO))

    score = float(signal) * float(mult_bloco)
    score_ponderado = float(score) * float(peso)

    # 5) Texto amigável
    regra_txt = (
        "Este indicador lê o relatório mensal Oil Crops Outlook (USDA/ERS) e "
        "classifica o impacto para o preço da soja. O agente pondera cada fato "
        "de oferta e demanda: mais oferta (produção/estoques/área) tende a "
        "BEARISH; mais demanda (esmagamento/exportações) tende a BULLISH. "
        "O veredito sai pela diferença de evidências (margem mínima de 2). "
        "Se os sinais forem equilibrados, fica NEUTRAL."
    )

    fonte_txt = "USDA, Economic Research Service — Oil Crops Outlook."

    # 6) Atualiza row no formato do painel
    row.update({
        "bloco": bloco,
        "variavel": row.get("variavel", VAR_NAME),

        "ultimo_valor": float(signal),
        "percentil": None,
        "nivel": label,              # BULLISH/BEARISH/NEUTRAL (qualitativo)
        "valor_nivel": float(signal),

        "tendencia": "NEUTRO",
        "valor_tendencia": 0.0,
        "momento": "NEUTRO",
        "valor_momento": 0.0,

        "score": float(score),
        "peso": float(peso),
        "score_ponderado": float(score_ponderado),

        "frequencia": "Mensal",
        "ultima_atualizacao": last_date,
        "regra_de_sinal": regra_txt,
        "fonte": fonte_txt,
        "ultima_data_serie": last_date,
    })

    snap["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    SNAPSHOT_PATH.write_text(
        json.dumps(snap, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("OK: painel_snapshot.json atualizado (oil_crops_outlook).")
    print("DEBUG:", "date=", last_date, "| label=", label, "| signal=", signal,
          "| bloco=", bloco, "| peso=", peso, "| score=", score,
          "| score_ponderado=", score_ponderado)


if __name__ == "__main__":
    main()
