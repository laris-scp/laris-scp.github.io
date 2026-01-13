import json
from pathlib import Path
from datetime import datetime

import pandas as pd
import requests

OUT_PATH = Path("data/cafe/series/fertilizante_urea.json")

URL_XLS = "https://thedocs.worldbank.org/en/doc/186749e1dbe2a9b8a5a66e62c8c3d7a4-0350012023/original/CMO-Historical-Data-Monthly.xlsx"

META = {
    "id": "fertilizante_urea",
    "name": "FERTILIZANTE (UREIA)",
    "unit": "US$/t",
    "frequency": "Mensal",
    "source": "World Bank – Commodity Markets Outlook (CMO)",
}

MM_SHORT = 4
MM_LONG = 12


def load_existing_last_date():
    if not OUT_PATH.exists():
        return None

    payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    series = payload.get("series", [])
    if not series:
        return None

    return series[-1].get("date")


def main():
    # --- estado atual ---
    last_saved_date = load_existing_last_date()

    # --- download do XLS ---
    r = requests.get(URL_XLS, timeout=60)
    r.raise_for_status()

    df = pd.read_excel(r.content)

    # coluna de data
    date_col = df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")

    # identifica coluna da ureia
    urea_cols = [c for c in df.columns if isinstance(c, str) and "urea" in c.lower()]
    if not urea_cols:
        raise RuntimeError("Não encontrei coluna de Ureia no XLS do World Bank.")

    col_urea = urea_cols[0]

    df = df[[date_col, col_urea]].dropna()
    df.columns = ["date", "close"]

    # fim de mês
    df["date"] = df["date"] + pd.offsets.MonthEnd(0)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna().sort_values("date").reset_index(drop=True)

    if df.empty:
        raise RuntimeError("Série de ureia vazia após limpeza.")

    # --- early-exit por data ---
    last_date_new = df.iloc[-1]["date"].strftime("%Y-%m-%d")
    if last_saved_date is not None and last_saved_date == last_date_new:
        print(f"Sem dados novos para fertilizante_urea. Última data: {last_date_new}")
        return

    # --- médias móveis ---
    df["mm4m"] = df["close"].rolling(MM_SHORT).mean()
    df["mm12m"] = df["close"].rolling(MM_LONG).mean()

    df = df.dropna().reset_index(drop=True)

    if len(df) < MM_LONG + 2:
        raise RuntimeError(f"Série curta demais após MM (n={len(df)}).")

    series = []
    for _, row in df.iterrows():
        series.append({
            "date": row["date"].strftime("%Y-%m-%d"),
            "close": float(row["close"]),
            "mm4m": float(row["mm4m"]),
            "mm12m": float(row["mm12m"]),
        })

    payload = {
        **META,
        "series": series
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: fertilizante_urea.json atualizado.")
    print("Última data:", series[-1]["date"])
    print("Último valor:", series[-1]["close"])


if __name__ == "__main__":
    main()
