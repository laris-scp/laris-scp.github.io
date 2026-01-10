import json
from pathlib import Path
from datetime import datetime
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]

SERIES_PATH = REPO_ROOT / "data/cafe/series/estoques_certificados.json"
SNAPSHOT_PATH = REPO_ROOT / "data/cafe/painel_snapshot.json"

VAR_ID = "estoques_certificados"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def percentile_rank(series, value):
    s = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    return float((s <= value).sum() / len(s)) if len(s) else None


def map_percentil_to_nivel(p):
    if p is None: return ("NEUTRO", 0.0)
    if p < 0.20: return ("MUITO BAIXO", -1.0)
    if p < 0.40: return ("BAIXO", -0.5)
    if p < 0.60: return ("NEUTRO", 0.0)
    if p < 0.80: return ("ALTO", 0.5)
    return ("MUITO ALTO", 1.0)


def map_tendencia(A, B, C):
    if A > B > C:
        return "QUEDA", -1.0
    if A < B < C:
        return "ALTA", 1.0
    return "LATERAL", 0.0


def map_momento(d1, d2, tendencia):
    if tendencia == "QUEDA":
        return ("QUEDA ACELERANDO", -1.0) if abs(d2) > abs(d1) else ("QUEDA DESACELERANDO", -0.5)
    if tendencia == "ALTA":
        return ("ALTA ACELERANDO", 1.0) if abs(d2) > abs(d1) else ("ALTA DESACELERANDO", 0.5)
    return ("NEUTRO", 0.0)


def main():
    series = load_json(SERIES_PATH)
    snap = load_json(SNAPSHOT_PATH)

    df = pd.DataFrame(series["series"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    if len(df) < 3:
        raise RuntimeError("Histórico insuficiente para snapshot (mín. 3 pontos).")

    v0 = float(df.iloc[-1]["value"])
    v1 = float(df.iloc[-2]["value"])
    v2 = float(df.iloc[-3]["value"])

    percentil = percentile_rank(df["value"], v0)
    nivel_cat, val_nivel = map_percentil_to_nivel(percentil)

    A = df.iloc[-36:-24]["value"].mean()
    B = df.iloc[-24:-12]["value"].mean()
    C = df.iloc[-12:]["value"].mean()

    tendencia, val_tend = map_tendencia(A, B, C)
    d1, d2 = B - A, C - B
    momento, val_mom = map_momento(d1, d2, tendencia)

    row = next(r for r in snap["rows"] if r["id"] == VAR_ID)

    bloco = row["bloco"]
    mult_bloco = -1.0 if bloco == 2 else 1.0
    peso = float(row.get("peso", 1.0))

    score = (val_nivel + val_tend + val_mom) * mult_bloco
    score_pond = score * peso

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    row.update({
        "ultimo_valor": v0,
        "percentil": percentil,
        "nivel": nivel_cat,
        "tendencia": tendencia,
        "momento": momento,
        "score": score,
        "score_ponderado": score_pond,
        "frequencia": "Mensal",
        "ultima_atualizacao": now,
        "fonte": "ICE – Certified Stocks (EOM)",
    })

    snap["updated_at"] = now
    snap["thermometers"]["geral"] = snap["thermometers"]["geral"]

    save_json(SNAPSHOT_PATH, snap)
    print("OK: painel_snapshot atualizado (estoques certificados).")


if __name__ == "__main__":
    main()
