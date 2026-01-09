import json
from pathlib import Path
from datetime import datetime

import pandas as pd

SERIES_PATH = Path("data/cafe/series/fertilizante_urea.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

MM_SHORT = 4
MM_LONG = 12

def percentile_last(series):
    arr = pd.to_numeric(series, errors="coerce").dropna().values
    if len(arr) == 0:
        return float("nan")
    return float((arr < arr[-1]).mean())

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

def main():
    # -------------------------
    # 1) Carrega série
    # -------------------------
    series_json = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    df = pd.DataFrame(series_json["series"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    df = df.dropna(subset=["mm4m", "mm12m"])
    if len(df) < 3:
        raise RuntimeError("Histórico insuficiente após MM4/MM12.")

    ult = float(df.iloc[-1]["close"])

    # -------------------------
    # 2) NÍVEL (percentil histórico total)
    # -------------------------
    percentil = percentile_last(df["close"])
    nivel_txt, valor_nivel = pct_to_level_and_value(percentil)

    # -------------------------
    # 3) TENDÊNCIA
    # -------------------------
    last_mm4 = float(df.iloc[-1]["mm4m"])
    last_mm12 = float(df.iloc[-1]["mm12m"])

    if last_mm4 > last_mm12:
        tendencia = "ALTA"
    elif last_mm4 < last_mm12:
        tendencia = "QUEDA"
    else:
        tendencia = "LATERAL"

    valor_tendencia = tendencia_to_value(tendencia)

    # -------------------------
    # 4) MOMENTO (MM4)
    # -------------------------
    d1 = df.iloc[-2]["mm4m"] - df.iloc[-3]["mm4m"]
    d2 = df.iloc[-1]["mm4m"] - df.iloc[-2]["mm4m"]

    momento = "NEUTRO"
    if tendencia == "ALTA":
        momento = "ALTA ACELERANDO" if d2 > d1 else "ALTA DESACELERANDO"
    elif tendencia == "QUEDA":
        momento = "QUEDA ACELERANDO" if d2 < d1 else "QUEDA DESACELERANDO"

    valor_momento = momento_to_value(momento)

    # -------------------------
    # 5) SCORE
    # -------------------------
    score = valor_nivel + valor_tendencia + valor_momento

    # -------------------------
    # 6) Atualiza snapshot
    # -------------------------
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    row = next(r for r in snapshot["rows"] if r["id"] == "fertilizante_urea")

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
        "frequencia": "Mensal",
        "ultima_atualizacao": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fonte": "World Bank – Commodity Markets Outlook (Pink Sheet, Urea $/mt)",
        "regra_de_sinal": (
            "Nível indica se o preço da ureia está baixo ou alto no histórico. "
            "Tendência compara médias móveis de 4 e 12 meses. "
            "Momento avalia aceleração ou desaceleração da média curta. "
            "Ureia mais cara eleva custo de produção e tende a reduzir oferta futura de café."
        )
    })

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json atualizado (fertilizante_urea).")
    print("Último valor:", ult)
    print("Nível:", nivel_txt, "| Tendência:", tendencia, "| Momento:", momento)

if __name__ == "__main__":
    main()
