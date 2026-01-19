#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from pathlib import Path
from datetime import datetime


SERIES_PATH = Path("data/cafe/series/ico_ia.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

VAR_ID = "ico_ia"
VAR_NAME = "ICO (IA)"
BLOCO = 1
PESO = 1.0


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def pick_latest_point(series: list[dict]) -> dict:
    if not series:
        raise RuntimeError("ico_ia.json inválido: 'series' vazia.")

    # Garante ordenação por date (YYYY-MM-01), pega o último
    series_sorted = sorted(series, key=lambda x: str(x.get("date", "")))
    last = series_sorted[-1]

    for k in ("date", "signal", "label"):
        if k not in last:
            raise RuntimeError(f"ico_ia.json inválido: último ponto sem campo obrigatório '{k}'.")

    return last


def main():
    # 1) Lê série
    s = load_json(SERIES_PATH)
    series = s.get("series", [])
    if not isinstance(series, list):
        raise RuntimeError("ico_ia.json inválido: 'series' não é uma lista.")

    last = pick_latest_point(series)

    last_date = str(last["date"])
    signal = float(last["signal"])
    label = str(last["label"]).upper().strip()

    if label not in {"BULLISH", "BEARISH", "NEUTRAL"}:
        raise RuntimeError(f"Label inválido no ico_ia.json: {label}")
    if signal not in (-1.0, 0.0, 1.0):
        raise RuntimeError(f"Signal inválido no ico_ia.json: {signal}")

    # 2) Lê snapshot
    snap = load_json(SNAPSHOT_PATH)
    if "rows" not in snap or not isinstance(snap["rows"], list):
        raise RuntimeError("painel_snapshot.json inválido: não encontrei 'rows' como lista.")

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

    # 5) Texto amigável (sem “Nível/Tendência/Momento” técnico)
    regra_txt = (
        "Este indicador lê o relatório mensal do ICO e classifica o impacto para o preço do café. "
        "Quando o relatório sugere mais oferta (produção maior / revisão positiva), o sinal tende a ser BEARISH. "
        "Quando sugere aperto de oferta (estoques baixos / riscos de quebra / restrições), tende a ser BULLISH. "
        "Se os sinais forem mistos, fica NEUTRAL."
    )

    fonte_txt = "International Coffee Organization (ICO) — Monthly Coffee Market Report (CMR)."

    # 6) Atualiza row no formato do painel (mantém chaves padrão para não quebrar o site)
    row.update({
        "bloco": bloco,
        "variavel": row.get("variavel", VAR_NAME),

        # Para consistência visual, colocamos o “diagnóstico” em Nível
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
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json atualizado (ico_ia).")
    print("DEBUG:", "date=", last_date, "| label=", label, "| signal=", signal,
          "| bloco=", bloco, "| peso=", peso, "| score=", score, "| score_ponderado=", score_ponderado)


if __name__ == "__main__":
    main()
