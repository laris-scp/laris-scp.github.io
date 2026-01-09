import json
from pathlib import Path
from datetime import datetime

import pandas as pd

SERIES_PATH = Path("data/cafe/series/cot_report.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

LOOKBACK_YEARS_LEVEL = 5
WEEKS_A = (25, 36)
WEEKS_B = (13, 24)
WEEKS_C = (0, 12)

def percentile_rank(series, value):
    s = pd.to_numeric(series, errors="coerce").dropna()
    return float((s <= value).sum() / len(s)) if len(s) else float("nan")

def pct_to_level_and_value(p):
    if pd.isna(p):
        return "INDEFINIDO", 0.0
    if p < 0.20:
        return "MUITO BAIXO", -1.0
    if p < 0.40:
        return "BAIXO", -0.5
    if p < 0.60:
        return "NEUTRO", 0.0
    if p < 0.80:
        return "ALTO", 0.5
    return "MUITO ALTO", 1.0

def tendencia_to_value(t):
    if t == "ALTA":
        return 1.0
    if t == "QUEDA":
        return -1.0
    return 0.0

def momento_to_value(m):
    if m == "QUEDA ACELERANDO":
        return -1.0
    if m == "QUEDA DESACELERANDO":
        return -0.5
    if m == "ALTA DESACELERANDO":
        return 0.5
    if m == "ALTA ACELERANDO":
        return 1.0
    return 0.0

def window_mean_weeks(df, end_date, w_start, w_end):
    ini = end_date - pd.DateOffset(weeks=w_start)
    fim = end_date - pd.DateOffset(weeks=w_end)
    s = df.loc[(df["date"] > ini) & (df["date"] <= fim), "close"]
    return float(s.mean()) if len(s) else float("nan")

def main():
    # ---- Load series ----
    series_json = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    df = pd.DataFrame(series_json["series"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    ult = float(df.iloc[-1]["close"])
    last_date = df.iloc[-1]["date"]

    # ---- Nível (percentil 5y) ----
    cutoff = last_date - pd.DateOffset(years=LOOKBACK_YEARS_LEVEL)
    w_level = df.loc[df["date"] >= cutoff, "close"]
    percentil = percentile_rank(w_level, ult)
    nivel_txt, valor_nivel = pct_to_level_and_value(percentil)

    # ---- Tendência & Momento ----
    A = window_mean_weeks(df, last_date, *WEEKS_A)
    B = window_mean_weeks(df, last_date, *WEEKS_B)
    C = window_mean_weeks(df, last_date, *WEEKS_C)

    tendencia = "INDEFINIDA"
    momento = "NEUTRO"

    if pd.notna(A) and pd.notna(B) and pd.notna(C):
        if A < B < C:
            tendencia = "ALTA"
        elif A > B > C:
            tendencia = "QUEDA"
        else:
            tendencia = "LATERAL"

        d1 = B - A
        d2 = C - B

        if tendencia == "ALTA":
            momento = "ALTA ACELERANDO" if abs(d2) > abs(d1) else "ALTA DESACELERANDO"
        elif tendencia == "QUEDA":
            momento = "QUEDA ACELERANDO" if abs(d2) > abs(d1) else "QUEDA DESACELERANDO"

    valor_tendencia = tendencia_to_value(tendencia)
    valor_momento = momento_to_value(momento)

    # ---- Score ----
    score = valor_nivel + valor_tendencia + valor_momento

    # ---- Load snapshot ----
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    row = next(r for r in snapshot["rows"] if r["id"] == "cot_report")

    peso = float(row.get("peso", 1.0))
    score_ponderado = score * peso

    row.update({
        "ultimo_valor": ult,
        "percentil": round(percentil, 4),
        "nivel": nivel_txt,
        "valor_nivel": valor_nivel,
        "tendencia": tendencia,
        "valor_tendencia": valor_tendencia,
        "momento": momento,
        "valor_momento": valor_momento,
        "score": score,
        "score_ponderado": score_ponderado,
        "frequencia": "Semanal",
        "ultima_atualizacao": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fonte": "CFTC COT (Disaggregated) | Coffee C (ICE) | Managed Money (net long - short)",
        "regra_de_sinal": (
            "Nível mostra se o posicionamento líquido dos fundos (Managed Money) está baixo ou alto em relação aos últimos 5 anos; "
            "Tendência indica se esse posicionamento vem aumentando, diminuindo ou ficando estável ao comparar médias de períodos mais antigos e mais recentes; "
            "Momento mostra se essa tendência está ganhando ou perdendo força ao comparar a mudança entre as janelas; "
            "Score combina Nível, Tendência e Momento (com peso) em um indicador único."
        )
    })

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json atualizado (COT Report).")
    print("Última data:", last_date.date(), "| Último valor:", ult)

if __name__ == "__main__":
    main()
