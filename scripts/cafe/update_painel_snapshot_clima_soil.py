#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

SERIES_PATH = Path("data/cafe/series/clima_soil.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

# ===== Conceito NOVO (CLIMA – Soil Moisture) =====
WIN_ACC = 6  # meses no acumulado (stress_6m)
CRITICAL_MONTHS = {1, 2, 3, 4, 5, 6, 7, 9, 10, 11}  # Jan–Jul e Set–Nov
EPS = 1e-12
AREA = [-15, -55, -25, -40]

# ===== NOVO: tendência apenas em nível EXTREMO =====
EXTREME_VAL_NIVEL = 1.0  # "MUITO ALTO" no seu mapping atual de nível via z-score

def map_z_to_nivel(z: float | None):
    if z is None or pd.isna(z):
        return ("NEUTRO", 0.0)
    z = float(z)
    if z <= -1.0:
        return ("MUITO BAIXO", -1.0)
    if z <= -0.5:
        return ("BAIXO", -0.5)
    if z < 0.5:
        return ("NEUTRO", 0.0)
    if z < 1.5:
        return ("ALTO", 0.5)
    return ("MUITO ALTO", 1.0)

def tendencia_3p(d1: float, d2: float):
    if (d1 > EPS) and (d2 > EPS):
        return ("ALTA", 1.0)
    if (d1 < -EPS) and (d2 < -EPS):
        return ("QUEDA", -1.0)
    if (abs(d1) <= EPS) and (abs(d2) <= EPS):
        return ("LATERAL", 0.0)
    return ("INDEFINIDA", 0.0)

def momento_from_trend(tendencia: str, d1: float, d2: float):
    # Padrão do painel
    if tendencia == "ALTA":
        if d2 > d1 + EPS:
            return ("ALTA ACELERANDO", 1.0)
        if d2 < d1 - EPS:
            return ("ALTA DESACELERANDO", 0.5)
        return ("NEUTRO", 0.0)

    if tendencia == "QUEDA":
        if d2 < d1 - EPS:
            return ("QUEDA ACELERANDO", -1.0)
        if d2 > d1 + EPS:
            return ("QUEDA DESACELERANDO", -0.5)
        return ("NEUTRO", 0.0)

    return ("NEUTRO", 0.0)

def main():
    # 1) Ler série (close = swvl3 contínuo)
    s = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    df = pd.DataFrame(s.get("series", []))

    if df.empty or "date" not in df.columns or "close" not in df.columns:
        raise RuntimeError("clima_soil.json inválido: 'series' vazia ou faltam campos date/close.")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["swvl3"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "swvl3"]).sort_values("date").reset_index(drop=True)

    if len(df) < (WIN_ACC + 1):
        raise RuntimeError(f"Histórico insuficiente para clima_soil (mínimo {WIN_ACC+1} pontos).")

    df["month"] = df["date"].dt.month

    # 2) Baseline mensal (dessazonaliza)
    baseline_month = df.groupby("month")["swvl3"].mean()
    df = df.merge(baseline_month.rename("baseline"), on="month", how="left")

    # 3) Déficit mensal (seca): baseline - swvl3, truncado em 0
    df["deficit"] = (df["baseline"] - df["swvl3"]).clip(lower=0)

    # 4) Stress acumulado 6m (contínuo)
    df["stress_6m"] = df["deficit"].rolling(WIN_ACC, min_periods=WIN_ACC).sum()

    # 5) z-score global do stress_6m
    valid = df["stress_6m"].notna()
    mu = df.loc[valid, "stress_6m"].mean()
    sd = df.loc[valid, "stress_6m"].std(ddof=1)

    if sd is None or np.isnan(sd) or float(sd) < 1e-12:
        raise RuntimeError("Desvio-padrão muito pequeno/zero; não dá para calcular z-score.")

    df["z_stress_6m"] = np.nan
    df.loc[valid, "z_stress_6m"] = (df.loc[valid, "stress_6m"] - float(mu)) / float(sd)

    # 6) Só vale nos meses críticos (para score/tendência/momento)
    df["is_critical"] = df["month"].isin(CRITICAL_MONTHS)
    df["score_input"] = np.where(df["is_critical"], df["z_stress_6m"], np.nan)

    last_valid = df.dropna(subset=["score_input"]).tail(3).copy()
    if len(last_valid) < 3:
        raise RuntimeError("Não há 3 pontos válidos suficientes dentro dos meses críticos para CLIMA.")

    v1, v2, v3 = last_valid["score_input"].iloc[-3:].values.astype(float)
    d1 = float(v2 - v1)
    d2 = float(v3 - v2)

    z_last = float(last_valid["score_input"].iloc[-1])
    nivel_cat, val_nivel = map_z_to_nivel(z_last)

    # =========================================================
    # 6.1) TENDÊNCIA (NOVA REGRA): só calcula se nível for EXTREMO
    # - Fora do extremo: tendência = LATERAL (0.0)
    # - No extremo: usa a regra atual (2 deltas consecutivos)
    # =========================================================
    if float(val_nivel) == EXTREME_VAL_NIVEL:
        tendencia, val_tend = tendencia_3p(d1, d2)
    else:
        tendencia, val_tend = ("LATERAL", 0.0)

    # Momento: mantém a lógica do painel, aplicada sobre a tendência final
    momento, val_mom = momento_from_trend(tendencia, d1, d2)

    # percentil (opcional) — percentil do z-score dentro dos meses críticos
    zcrit = df["score_input"].dropna().astype(float).values
    percentil = float((zcrit < z_last).mean()) if len(zcrit) else None

    # último valor exibido no painel = swvl3 (umidade bruta)
    ultimo_valor_raw = float(df["swvl3"].iloc[-1])
    ultimo_date = df["date"].iloc[-1].strftime("%Y-%m-%d")

    # 7) Atualiza linha clima_soil no snapshot
    snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    if "rows" not in snap or not isinstance(snap["rows"], list):
        raise RuntimeError("painel_snapshot.json inválido: não encontrei 'rows' como lista.")

    row = None
    for r in snap["rows"]:
        if r.get("id") == "clima_soil":
            row = r
            break
    if row is None:
        raise RuntimeError("Não encontrei id='clima_soil' em painel_snapshot.json.")

    bloco = int(row.get("bloco", 1))
    mult_bloco = 1.0 if bloco == 1 else (-1.0 if bloco == 2 else 1.0)
    peso = float(row.get("peso", 1.0))

    score = (float(val_nivel) + float(val_tend) + float(val_mom)) * float(mult_bloco)
    score_ponderado = float(score) * float(peso)

    rule_txt = (
        "Este indicador mede o nível de estresse hídrico do solo nas principais regiões produtoras de café. Valores mais altos indicam solo mais seco, o que pode reduzir a produtividade.
O painel avalia o nível do estresse em relação ao histórico e só gera sinal de tendência quando a seca atinge níveis extremos, momento em que variações passam a ter impacto relevante sobre a oferta. Fora desses episódios, o clima é considerado neutro para a análise direcional."
    )

    fonte_txt = (
        "ERA5-Land (ECMWF/Copernicus) — swvl3 (Volumetric soil water layer 3)."
    )

    row.update({
        "ultimo_valor": ultimo_valor_raw,                 # swvl3
        "percentil": None if percentil is None else round(percentil, 4),
        "nivel": nivel_cat,
        "valor_nivel": float(val_nivel),
        "tendencia": tendencia,
        "valor_tendencia": float(val_tend),
        "momento": momento,
        "valor_momento": float(val_mom),
        "score": float(score),
        "score_ponderado": float(score_ponderado),
        "frequencia": "Mensal",
        "ultima_atualizacao": ultimo_date,
        "regra_de_sinal": rule_txt,
        "fonte": fonte_txt,
    })

    snap["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    SNAPSHOT_PATH.write_text(
        json.dumps(snap, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json atualizado (clima_soil — tendência apenas em nível extremo).")
    print("DEBUG:", "ultimo swvl3=", ultimo_valor_raw, "| z_last(crit)=", round(z_last, 4),
          "| nivel=", nivel_cat, val_nivel, "| tendencia=", tendencia, "| momento=", momento)
    print("DEBUG:", "score=", score, "| peso=", peso, "| score_ponderado=", score_ponderado)

if __name__ == "__main__":
    main()
