import json
from pathlib import Path
from datetime import datetime

import pandas as pd

SERIES_PATH = Path("data/cafe/series/gscpi_fretes.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

EPS_TREND = 0.05  # conforme seu Colab

def percentile_rank_leq(values, value) -> float:
    """
    Percentil conforme seu Colab: (s <= value).sum / len(s)
    """
    s = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if len(s) == 0:
        return float("nan")
    return float((s <= value).sum() / len(s))

def map_percentil_to_nivel(p):
    if pd.isna(p): return ("NEUTRO", 0.0)
    if p < 0.20: return ("MUITO BAIXO", -1.0)
    if p < 0.40: return ("BAIXO", -0.5)
    if p < 0.60: return ("NEUTRO", 0.0)
    if p < 0.80: return ("ALTO", 0.5)
    return ("MUITO ALTO", 1.0)

def map_tendencia_to_val(t):
    if t == "ALTA": return 1.0
    if t == "QUEDA": return -1.0
    return 0.0

def map_momento_to_val(m):
    if m == "QUEDA ACELERANDO": return -1.0
    if m == "QUEDA DESACELERANDO": return -0.5
    if m == "ALTA ACELERANDO": return 1.0
    if m == "ALTA DESACELERANDO": return 0.5
    return 0.0

def main():
    # 1) Carrega série
    series_json = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    df = pd.DataFrame(series_json["series"])

    if df.empty or "date" not in df.columns or "close" not in df.columns:
        raise RuntimeError("Série GSCPI inválida: faltam colunas 'date' e/ou 'close'.")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    ultimo_date = df.iloc[-1]["date"].strftime("%Y-%m-%d")

    if len(df) < 3:
        raise RuntimeError("Histórico insuficiente para GSCPI (mínimo 3 pontos).")

    v0 = float(df.iloc[-1]["close"])
    v1 = float(df.iloc[-2]["close"])
    v2 = float(df.iloc[-3]["close"])

    d1 = v1 - v2
    d2 = v0 - v1

    # 2) Nível (percentil no histórico completo)
    percentil = percentile_rank_leq(df["close"], v0)
    nivel_cat, val_nivel = map_percentil_to_nivel(percentil)

    # 3) Tendência (3 pontos com EPS)
    if (d1 > EPS_TREND) and (d2 > EPS_TREND):
        tendencia = "ALTA"
    elif (d1 < -EPS_TREND) and (d2 < -EPS_TREND):
        tendencia = "QUEDA"
    elif (abs(d1) <= EPS_TREND) and (abs(d2) <= EPS_TREND):
        tendencia = "LATERAL"
    else:
        tendencia = "INDEFINIDA"

    # 4) Momento
    if tendencia == "ALTA":
        momento = "ALTA ACELERANDO" if d2 > d1 else "ALTA DESACELERANDO"
    elif tendencia == "QUEDA":
        momento = "QUEDA ACELERANDO" if abs(d2) > abs(d1) else "QUEDA DESACELERANDO"
    else:
        momento = "NEUTRO"

    val_tend = map_tendencia_to_val(tendencia)
    val_mom = map_momento_to_val(momento)

    # 5) Score ajustado por bloco e ponderado por peso (conforme seu Colab)
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    row = next(r for r in snapshot["rows"] if r["id"] == "gscpi")

    bloco = int(row.get("bloco", 1))
    mult_bloco = 1.0 if bloco == 1 else (-1.0 if bloco == 2 else 1.0)

    score = (val_nivel + val_tend + val_mom) * mult_bloco

    peso = float(row.get("peso", 1.0))
    score_ponderado = float(score) * float(peso)

    
    rule_txt = (
        "Nível = percentil do GSCPI no histórico completo. "
        f"Tendência usa os 3 últimos pontos com eps={EPS_TREND:.2f}: "
        "ALTA (d1,d2>eps), QUEDA (d1,d2<-eps), LATERAL (|d1|,|d2|<=eps), senão INDEFINIDA. "
        "Momento compara d2 vs d1 (acelera/desacelera). "
        "Score ajustado por BLOCO e multiplicado pelo PESO."
    )

    row.update({
        "ultimo_valor": v0,
        "percentil": round(percentil, 4) if not pd.isna(percentil) else None,
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
        "fonte": "NY Fed – Global Supply Chain Pressure Index (GSCPI)",
    })

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json atualizado (gscpi).")
    print("Último:", v0, "| d1:", round(d1, 4), "| d2:", round(d2, 4), "| tendência:", tendencia, "| momento:", momento)
    print("Score:", score, "| Peso:", peso, "| Score ponderado:", score_ponderado)

if __name__ == "__main__":
    main()
