import json
import zipfile
from io import BytesIO
from datetime import datetime
from pathlib import Path
import time

import pandas as pd
import requests

OUT_PATH = Path("data/soja/series/cot_report.json")

LOOKBACK_YEARS_LEVEL = 5
MM_WEEKS = 12  # mm12w

META = {
    "id": "cot_report",
    "variavel": "COT REPORT",
    "frequencia": "Semanal",
    "descricao": "CFTC COT Disaggregated – Soybeans (CBOT) – Managed Money NET + MM12W",
}

CFTC_URL = "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"


def load_existing_last_date():
    if not OUT_PATH.exists():
        return None

    payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    series = payload.get("series", [])
    if not series:
        return None

    return series[-1].get("date")


def download_disagg_year(year: int, retries: int = 3) -> pd.DataFrame | None:
    url = CFTC_URL.format(year=year)

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")

            z = zipfile.ZipFile(BytesIO(r.content))

            txt_files = [n for n in z.namelist() if n.lower().endswith(".txt")]
            if not txt_files:
                raise RuntimeError("Nenhum .txt encontrado no ZIP")

            name = txt_files[0]
            txt = z.read(name).decode("utf-8", errors="ignore")
            return pd.read_csv(BytesIO(txt.encode()), sep=",", low_memory=False)

        except Exception as e:
            if attempt == retries:
                raise RuntimeError(f"Falha ao baixar COT {year}: {e}")
            time.sleep(2)

    return None


def main():
    # --- estado atual ---
    last_saved_date = load_existing_last_date()

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

    # Filtro: Soybeans (CBOT) contrato cheio
    # - Inclui "SOYBEANS - CHICAGO BOARD OF TRADE"
    # - Exclui MEAL e OIL (para nao pegar Soybean Meal/Oil)
    # - Exclui MINI (o contrato mini-Soybeans tem baixo volume e muitas
    #   linhas zeradas, gerando duplicacao por data se nao excluido)
    mkt = cot["Market_and_Exchange_Names"].astype(str).str.upper()
    mask = (
        mkt.str.contains("SOYBEANS", na=False)
        & mkt.str.contains("CHICAGO BOARD OF TRADE", na=False)
        & ~mkt.str.contains("MEAL", na=False)
        & ~mkt.str.contains("OIL", na=False)
        & ~mkt.str.contains("MINI", na=False)
    )
    cot = cot.loc[mask].copy()

    # Sanity check: garantir que sobrou apenas 1 mercado apos o filtro
    n_markets = cot["Market_and_Exchange_Names"].nunique()
    if n_markets != 1:
        raise RuntimeError(
            f"Filtro retornou {n_markets} mercados (esperado: 1). "
            f"Mercados: {sorted(cot['Market_and_Exchange_Names'].unique().tolist())}"
        )

    # Managed Money NET
    cot["long"] = pd.to_numeric(cot["M_Money_Positions_Long_All"], errors="coerce")
    cot["short"] = pd.to_numeric(cot["M_Money_Positions_Short_All"], errors="coerce")
    cot["close"] = cot["long"] - cot["short"]

    cot = cot.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

    # Sanity check extra: nao pode haver duplicatas de data
    dups = cot["date"].duplicated().sum()
    if dups > 0:
        raise RuntimeError(f"Encontradas {dups} datas duplicadas apos o filtro. Algo errado.")

    if len(cot) < 20:
        raise RuntimeError(f"Série COT curta demais (n={len(cot)}).")

    # Early-exit por data
    last_date_new = cot.iloc[-1]["date"].strftime("%Y-%m-%d")
    if last_saved_date is not None and last_saved_date == last_date_new:
        print(f"Sem dados novos no COT Report (soja). Última data: {last_date_new}")
        return

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

    print("OK: data/soja/series/cot_report.json atualizado.")
    print("Última data:", series[-1]["date"])
    print("Último close:", series[-1]["close"])
    print("Último mm12w:", series[-1]["mm12w"])


if __name__ == "__main__":
    main()
