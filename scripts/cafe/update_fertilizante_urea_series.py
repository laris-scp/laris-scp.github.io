import json
import calendar
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSkvsFzKf5EGcSrtAgFZDwMHkmxRxUs-PxbWq6cOGwvgX63ePsV0xCiBEb9_XJLnpVY-aRm-dQYKq14/pub?gid=78854352&single=true&output=csv"

OUT_PATH = Path("data/cafe/series/fertilizante_urea.json")

META = {
    "id": "fertilizante_urea",
    "name": "FERTILIZANTE (UREA)",
    "unit": "US$/t",
    "frequency": "Mensal",
}

MM_SHORT = 4
MM_LONG = 12

def yyyymm_to_eom(yyyymm_m: str) -> str:
    """
    Converte '1960M01' para '1960-01-31' (fim do mês).
    """
    s = str(yyyymm_m).strip()
    if "M" not in s:
        raise ValueError(f"DATA inválida (esperado YYYYMmm): {s}")
    y, m = s.split("M")
    year = int(y)
    month = int(m)
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"

def parse_ptbr_number(x) -> float:
    """
    Converte '42,25' -> 42.25
    """
    if x is None:
        return float("nan")
    s = str(x).strip()
    if s == "":
        return float("nan")
    # remove separador de milhar se existir e troca vírgula por ponto
    s = s.replace(".", "").replace(",", ".")
    return float(s)

def main():
    # 1) Baixar CSV publicado
    r = requests.get(CSV_URL, timeout=60)
    r.raise_for_status()

    df = pd.read_csv(pd.io.common.BytesIO(r.content))
    # Esperado: colunas ["DATA", "Urea ($/mt)"]
    if "DATA" not in df.columns:
        raise RuntimeError(f"CSV não contém coluna DATA. Colunas: {df.columns.tolist()}")

    # Detecta a coluna do valor (segunda coluna)
    value_col = None
    for c in df.columns:
        if c != "DATA":
            value_col = c
            break
    if value_col is None:
        raise RuntimeError("CSV não contém coluna de valor para Ureia.")

    df["date"] = df["DATA"].apply(yyyymm_to_eom)
    df["close"] = df[value_col].apply(parse_ptbr_number)

    df = df.dropna(subset=["close"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    if len(df) < MM_LONG + 3:
        raise RuntimeError(f"Série curta demais para MM{MM_LONG} (n={len(df)}).")

    # 2) Médias móveis mensais
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
        "id": META["id"],
        "name": META["name"],
        "unit": META["unit"],
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "series": out_series,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: fertilizante_urea.json atualizado.")
    print("Última data:", out_series[-1]["date"])
    print("Último close:", out_series[-1]["close"])

if __name__ == "__main__":
    main()
