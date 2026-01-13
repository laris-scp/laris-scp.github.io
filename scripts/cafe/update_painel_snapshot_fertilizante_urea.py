import json
from pathlib import Path
from datetime import datetime
import pandas as pd

SERIES_PATH = Path("data/cafe/series/fertilizante_urea.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

SERIES_LAST_DATE_FIELD = "ultima_data_serie"


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
    pts = series_json.get("series", [])
    if not pts:
        raise RuntimeError("fertilizante_urea.json sem dados em 'series'.")

    series_last_date = pts[-1].get("date")
    if not series_last_date:
        raise RuntimeError("Não encontrei a última data da série no JSON de ureia.")

    df = pd.DataFrame(pts)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)

    # garante as colunas esperadas (padrão A)
    for c in ["close", "mm4m", "mm12m"]:
        if c not in df.columns:
            raise RuntimeError(f"Coluna '{c}' não encontrada em fertilizante_urea.json. Verifique a série.")

    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["mm4m"] = pd.to_numeric(df["mm4m"], errors="coerce")
    df["mm12m"] = pd.to_numeric(df["mm12m"], errors="coerce")

    df = df.dropna(subset=["close", "mm4m", "mm12m"]).reset_index(drop=True)
    if len(df) < 3:
        raise RuntimeError("Histórico insuficiente após MM4/MM12.")

    ult = float(df.iloc[-1]["close"])

    # -------------------------
    # 2) Carrega snapshot e localiza linha
    # -------------------------
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("painel_snapshot.json esperado com 'rows' (lista).")

    row = None
    for r in rows:
        if r.get("id") == "fertilizante_urea":
            row = r
            break
    if row is None:
        raise RuntimeError("Não encontrei id='fertilizante_urea' no painel_snapshot.json.")

    # -------------------------
    # 3) Early exit: se não mudou a data da série, loga e sai
    # -------------------------
    prev_last = row.get(SERIES_LAST_DATE_FIELD)
    if prev_last is not None and str(prev_last) == str(series_last_date):
        print(f"Sem dados novos para fertilizante_urea no snapshot. Última data: {series_last_date}")
        return

    # -------------------------
    # 4) NÍVEL (percentil histórico total)
    # -------------------------
    percentil = percentile_last(df["close"])
    nivel_txt, valor_nivel = pct_to_level_and_value(percentil)

    # -------------------------
    # 5) TENDÊNCIA (MM4 vs MM12)
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
    # 6) MOMENTO (MM4)
    # -------------------------
    d1 = float(df.iloc[-2]["mm4m"] - df.iloc[-3]["mm4m"])
    d2 = float(df.iloc[-1]["mm4m"] - df.iloc[-2]["mm4m"])

    momento = "NEUTRO"
    if tendencia == "ALTA":
        momento = "ALTA ACELERANDO" if d2 > d1 else "ALTA DESACELERANDO"
    elif tendencia == "QUEDA":
        momento = "QUEDA ACELERANDO" if d2 < d1 else "QUEDA DESACELERANDO"

    valor_momento = momento_to_value(momento)

    # -------------------------
    # 7) SCORE
    # -------------------------
    score = float(valor_nivel + valor_tendencia + valor_momento)

    # -------------------------
    # 8) Atualiza snapshot (sem tratar bloco aqui)
    # -------------------------
    peso = float(row.get("peso", 1.0))
    score_ponderado = float(score * peso)

    row.update({
        "ultimo_valor": ult,
        "percentil": round(float(percentil), 4),
        "nivel": nivel_txt,
        "valor_nivel": float(valor_nivel),
        "tendencia": tendencia,
        "valor_tendencia": float(valor_tendencia),
        "momento": momento,
        "valor_momento": float(valor_momento),
        "score": float(score),
        "score_ponderado": score_ponderado,
        "frequencia": "Mensal",
        "ultima_atualizacao": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fonte": "World Bank – Commodity Markets Outlook (Pink Sheet, Urea $/mt)",
        "regra_de_sinal": (
            "Nível indica se o preço da ureia está baixo ou alto no histórico. "
            "Tendência compara médias móveis de 4 e 12 meses. "
            "Momento avalia aceleração ou desaceleração da média curta. "
            "Ureia mais cara eleva custo de produção e tende a reduzir oferta futura de café."
        ),
        SERIES_LAST_DATE_FIELD: str(series_last_date),
    })

    snapshot["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json atualizado (fertilizante_urea).")
    print("Última data da série:", series_last_date)
    print("Último valor:", ult)
    print("Nível:", nivel_txt, "| Tendência:", tendencia, "| Momento:", momento)


if __name__ == "__main__":
    main()
