import json
import zipfile
from io import BytesIO
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

OUT_PATH = Path("data/cafe/series/cot_report.json")

LOOKBACK_YEARS_LEVEL = 5

# Média móvel usada no site do COT:
MM_WEEKS = 12  # mm12w

META = {
    "id": "cot_report",
    "variavel": "COT REPORT",
    "frequencia": "Semanal",
    "descricao": "CFTC COT Disaggregated – Coffee C (ICE) – Managed Money NET + MM12W",
}

def download_disagg_year(year: int) -> pd.DataFrame | None:
    url = f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
    r = requests.get(url, timeout=60)
    if r.status_code != 200:
        return None

    z = zipfile.ZipFile(BytesIO(r.content))
    name = [n for n in z.namelist() if n.lower().endswith(".txt")][0]
    txt = z.read(name).decode("utf-8", errors="ignore")

    dfy = pd.read_csv(BytesIO(txt.encode()), sep=",")
    return dfy

def main():
    year_now = datetime.now().year
    years = range(year_now - LOOKBACK_YEARS_LEVEL - 1, year_now + 1)

    frames = []
    for y in years:
        dfy = download_disagg_year(y)
        if dfy is not None and not dfy.empty:
            frames.append(dfy)

    if not frames:
        raise RuntimeError("Falha ao baixar dados da CFTC (COT Disaggregated).")

    cot = pd.concat(frames, ignore_index=True)

    # Datas
    cot["date"] = pd.to_datetime(cot["Report_Date_as_YYYY-MM-DD"], errors="coerce")
    cot = cot.dropna(subset=["date"])

    # Filtro: Coffee C (ICE Futures)
    mkt = cot["Market_and_Exchange_Names"].astype(str).str.upper()
    mask = mkt.str.contains("COFFEE C", na=False) & mkt.str.contains("ICE FUTURES", na=False)
    cot = cot.loc[mask].copy()

    # Managed Money NET
    cot["long"] = pd.to_numeric(cot["M_Money_Positions_Long_All"], errors="coerce")
    cot["short"] = pd.to_numeric(cot["M_Money_Positions_Short_All"], errors="coerce")
    cot["close"] = cot["long"] - cot["short"]

    cot = cot.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    if len(cot) < 20:
        raise RuntimeError(f"Série COT curta demais (n={len(cot)}).")

    # MM12W
    cot["mm12w"] = cot["close"].rolling(MM_WEEKS).mean()
    cot = cot.dropna(subset=["mm12w"]).reset_index(drop=True)

    series = []
    for _, row in cot.iterrows():
        series.append({
            "date": row["date"].strftime("%Y-%m-%d"),
            "close": int(round(float(row["close"]), 0)),
            "mm12w": float(row["mm12w"]),
        })

    payload = {
        "meta": META,
        "series": series
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: cot_report.json atualizado.")
    print("Última data:", series[-1]["date"])
    print("Último close:", series[-1]["close"])
    print("Último mm12w:", series[-1]["mm12w"])

if __name__ == "__main__":
    main()
