import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

# =========================
# CONFIG
# =========================
TICKER = "KC=F"
MM_LONG = 252
MM_SHORT = 50
LEVEL_YEARS = 10

OUT_PATH = Path("data/cafe/series/preco_arabica.json")

META = {
    "id": "preco_arabica",
    "name": "PREÇO ARABICA",
    "unit": "US¢/lb",
    "frequency": "Diária",
}

RECALC_BUFFER_DAYS = 420
PROBE_DAYS = 14


def get_close_series(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        raise RuntimeError("Yahoo retornou vazio para KC=F.")

    close = df["Close"] if "Close" in df else None
    if close is None:
        raise RuntimeError("Coluna Close não encontrada.")

    s = close.dropna().copy()
    s.index = pd.to_datetime(s.index)
    return s


def load_existing():
    if not OUT_PATH.exists():
        return pd.DataFrame(columns=["date", "close"]), None

    payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    pts = payload.get("series", [])

    if not pts:
        return pd.DataFrame(columns=["date", "close"]), None

    df = pd.DataFrame(pts)
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

    return df[["date", "close"]], df["date"].max().date().isoformat()


def download_range(start: datetime, end: datetime) -> pd.Series:
    raw = yf.download(
        TICKER,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    return get_close_series(raw)


def main():
    df_existing, last_date_existing = load_existing()
    end_dt = datetime.today()

    # -------- Probe curto --------
    s_probe = download_range(end_dt - timedelta(days=PROBE_DAYS), end_dt)
    if s_probe.empty:
        raise RuntimeError("Yahoo retornou vazio no probe.")

    last_yahoo = s_probe.index.max().date().isoformat()

    if last_date_existing and last_yahoo <= last_date_existing:
        print("Sem dados novos.")
        return

    # -------- Coleta principal --------
    if df_existing.empty:
        start_dt = end_dt - pd.DateOffset(years=LEVEL_YEARS + 1)
        s_all = download_range(start_dt, end_dt)
        df_all = s_all.rename("close").to_frame().reset_index().rename(columns={"index": "date"})
    else:
        last_dt = pd.to_datetime(last_date_existing)
        start_dt = last_dt - timedelta(days=RECALC_BUFFER_DAYS)

        s_tail = download_range(start_dt, end_dt)
        df_tail = s_tail.rename("close").to_frame().reset_index().rename(columns={"index": "date"})

        df_prefix = df_existing[df_existing["date"] < start_dt]
        df_all = pd.concat([df_prefix, df_tail], ignore_index=True)

    df_all = df_all.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

    # -------- Médias móveis (SEM remover linhas) --------
    df_all["mm50"] = df_all["close"].rolling(MM_SHORT).mean()
    df_all["mm252"] = df_all["close"].rolling(MM_LONG).mean()

    # -------- Corte FINAL para 10 anos --------
    cutoff = df_all["date"].max() - pd.DateOffset(years=LEVEL_YEARS)
    df_all = df_all[df_all["date"] >= cutoff].reset_index(drop=True)

    # -------- JSON --------
    series = []
    for _, row in df_all.iterrows():
        series.append({
            "date": row["date"].date().isoformat(),
            "close": float(row["close"]),
            "mm50": None if pd.isna(row["mm50"]) else float(row["mm50"]),
            "mm252": None if pd.isna(row["mm252"]) else float(row["mm252"]),
        })

    payload = {**META, "series": series}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: preco_arabica.json atualizado.")
    print("Última data:", series[-1]["date"])
    print("Último close:", series[-1]["close"])


if __name__ == "__main__":
    main()
