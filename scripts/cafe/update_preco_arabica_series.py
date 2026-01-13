import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

# =========================
# CONFIG (padrão USD/BRL)
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

# buffer para recalcular MM sem baixar tudo
RECALC_BUFFER_DAYS = 420
# janela curta para checar se há dado novo
PROBE_DAYS = 14


def get_close_series(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        raise RuntimeError("Yahoo retornou vazio para KC=F.")

    if isinstance(df.columns, pd.MultiIndex):
        try:
            close_obj = df["Close"]
        except KeyError:
            close_cols = [c for c in df.columns if str(c[0]).lower() == "close"]
            if not close_cols:
                raise RuntimeError(f"Não achei Close no MultiIndex. Colunas: {df.columns}")
            close_obj = df[close_cols]
    else:
        close_obj = df.get("Close")

    if close_obj is None:
        raise RuntimeError(f"Não achei coluna Close. Colunas: {df.columns}")

    if isinstance(close_obj, pd.DataFrame):
        if close_obj.shape[1] < 1:
            raise RuntimeError("Close retornou DataFrame vazio.")
        s = close_obj.iloc[:, 0].copy()
    else:
        s = close_obj.copy()

    s = s.dropna()
    s.index = pd.to_datetime(s.index)
    return s


def load_existing():
    if not OUT_PATH.exists():
        return pd.DataFrame(columns=["date", "close"]), None

    payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    pts = payload.get("series", [])
    if not pts:
        return pd.DataFrame(columns=["date", "close"]), None

    last_date_str = pts[-1].get("date")
    df = pd.DataFrame(pts)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna().sort_values("date").reset_index(drop=True)
    return df[["date", "close"]], last_date_str


def download_range(start: datetime, end: datetime) -> pd.Series:
    raw = yf.download(
        TICKER,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        progress=False,
        group_by="column",
    )
    return get_close_series(raw)


def main():
    # --- 1) Estado atual ---
    df_existing, last_date_existing_str = load_existing()

    end_dt = datetime.today()

    # --- 2) Probe curto: existe dado novo? ---
    probe_start = end_dt - timedelta(days=PROBE_DAYS)
    s_probe = download_range(probe_start, end_dt)

    if s_probe.empty:
        raise RuntimeError("Yahoo retornou vazio no probe curto.")

    last_date_yahoo = s_probe.index.max().date().isoformat()

    if last_date_existing_str is not None and last_date_yahoo <= last_date_existing_str:
        print(f"Sem dados novos. Última data no JSON: {last_date_existing_str} | Yahoo: {last_date_yahoo}")
        return

    # --- 3) Coleta principal ---
    if last_date_existing_str is None:
        # bootstrap: últimos 10 anos
        start_dt = end_dt - pd.DateOffset(years=LEVEL_YEARS)
        s_all = download_range(start_dt, end_dt)
        df_all = s_all.rename("close").to_frame().reset_index().rename(columns={"index": "date"})
    else:
        last_dt = datetime.fromisoformat(last_date_existing_str)
        start_dt = last_dt - timedelta(days=RECALC_BUFFER_DAYS)

        s_tail = download_range(start_dt, end_dt)
        df_tail = s_tail.rename("close").to_frame().reset_index().rename(columns={"index": "date"})

        df_prefix = df_existing[df_existing["date"] < pd.to_datetime(start_dt)].copy()
        df_all = pd.concat([df_prefix, df_tail], ignore_index=True)

    df_all = df_all.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

    # recorta exatamente os últimos 10 anos
    cutoff = df_all["date"].max() - pd.DateOffset(years=LEVEL_YEARS)
    df_all = df_all[df_all["date"] >= cutoff].reset_index(drop=True)

    # --- 4) Médias móveis ---
    df_all["mm50"] = df_all["close"].rolling(MM_SHORT).mean()
    df_all["mm252"] = df_all["close"].rolling(MM_LONG).mean()
    df_all = df_all.dropna().reset_index(drop=True)

    if len(df_all) < MM_LONG + 60:
        raise RuntimeError(f"Poucos dados após MM: {len(df_all)}")

    # --- 5) JSON ---
    series = []
    for _, row in df_all.iterrows():
        series.append({
            "date": row["date"].date().isoformat(),
            "close": float(row["close"]),
            "mm50": float(row["mm50"]),
            "mm252": float(row["mm252"]),
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
