import json
import calendar
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

OUT_PATH = Path("data/cafe/series/fertilizante_urea.json")

# Fonte oficial (World Bank - CMO / Pink Sheet)
URL_XLS = "https://thedocs.worldbank.org/en/doc/18675f1d1639c7a34d463f59263ba0a2-0050012025/related/CMO-Historical-Data-Monthly.xlsx"
SHEET_NAME = "Monthly Prices"

MM_SHORT = 4
MM_LONG = 12

def _norm(s: str) -> str:
    return (
        str(s)
        .replace("\u00a0", " ")
        .strip()
        .upper()
    )

def yyyymm_to_eom(yyyymm: int) -> str:
    """
    Converte 196001 -> '1960-01-31' (fim do mês).
    """
    year = yyyymm // 100
    month = yyyymm % 100
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"

def main():
    # 1) Baixa XLS
    r = requests.get(URL_XLS, timeout=120)
    r.raise_for_status()

    # 2) Lê sheet com header robusto (igual sua lógica do Colab)
    raw = pd.read_excel(pd.io.common.BytesIO(r.content), sheet_name=SHEET_NAME, header=None)

    # Linha 4 = nomes principais | Linha 5 = unidades (0-index)
    h1 = raw.iloc[4].astype(str)
    h2 = raw.iloc[5].astype(str)

    cols = []
    for a, b in zip(h1, h2):
        name = f"{a} {b}".strip()
        name = name.replace("nan", "").strip()
        cols.append(name)

    df = raw.iloc[6:].copy()
    df.columns = cols

    # Primeira coluna = data
    date_col = df.columns[0]
    df = df.rename(columns={date_col: "DATE_RAW"})

    # Identifica coluna da UREIA (procura "UREA" e "$")
    urea_cols = [c for c in df.columns if ("UREA" in _norm(c) and "$" in _norm(c))]
    if not urea_cols:
        raise RuntimeError(f"Coluna de UREIA não encontrada. Exemplo colunas: {df.columns.tolist()[:25]}")

    UREA_COL = urea_cols[0]

    df = df[["DATE_RAW", UREA_COL]].dropna().copy()

    # DATE_RAW vem como '1960M01' (ou similar)
    df["YYYYMM"] = df["DATE_RAW"].astype(str).str.replace("M", "", regex=False).astype(int)
    df["close"] = pd.to_numeric(df[UREA_COL], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("YYYYMM").reset_index(drop=True)

    if len(df) < MM_LONG + 3:
        raise RuntimeError(f"Série curta demais para MM{MM_LONG} (n={len(df)}).")

    # 3) Datas fim de mês
    df["date"] = df["YYYYMM"].apply(yyyymm_to_eom)
    df["date"] = pd.to_datetime(df["date"])

    # 4) Médias móveis
    s = pd.Series(df["close"].values, index=df["date"])
    mm4m = s.rolling(MM_SHORT).mean()
    mm12m = s.rolling(MM_LONG).mean()

    out_series = []
    for dt in df["date"]:
        out_series.append({
            "date": dt.strftime("%Y-%m-%d"),
            "close": float(s.loc[dt]),
            "mm4m": None if pd.isna(mm4m.loc[dt]) else float(mm4m.loc[dt]),
            "mm12m": None if pd.isna(mm12m.loc[dt]) else float(mm12m.loc[dt]),
        })

    payload = {
        "id": "fertilizante_urea",
        "name": "FERTILIZANTE (UREA)",
        "unit": "US$/t",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "series": out_series,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: fertilizante_urea.json atualizado (World Bank XLS).")
    print("Última data:", out_series[-1]["date"])
    print("Último close:", out_series[-1]["close"])

if __name__ == "__main__":
    main()
