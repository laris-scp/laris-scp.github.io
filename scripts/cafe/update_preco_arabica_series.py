import json
from datetime import datetime, timedelta, date
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

# Se o último dado vier mais velho que isso, considera truncado/stale no Actions
MAX_AGE_DAYS = 10

# Se o JSON existente estiver muito desatualizado, ignora incremental e faz bootstrap
FORCE_BOOTSTRAP_IF_OLDER_THAN_DAYS = 60


def _today_utc() -> date:
    return datetime.utcnow().date()


def assert_series_is_fresh(s: pd.Series, label: str) -> None:
    if s is None or s.empty:
        raise RuntimeError(f"{label}: série vazia.")

    last = pd.to_datetime(s.index.max()).date()
    age = (_today_utc() - last).days
    if age > MAX_AGE_DAYS:
        raise RuntimeError(
            f"{label}: dado desatualizado (stale). "
            f"Última data={last} | hoje(UTC)={_today_utc()} | age_days={age}"
        )


def get_close_series(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        raise RuntimeError("Yahoo retornou vazio para KC=F.")

    # Pode vir MultiIndex
    if isinstance(df.columns, pd.MultiIndex):
        if "Close" in df.columns.get_level_values(0):
            close_obj = df["Close"]
        else:
            close_cols = [c for c in df.columns if str(c[0]).lower() == "close"]
            if not close_cols:
                raise RuntimeError(f"Não achei Close no MultiIndex. Colunas: {df.columns}")
            close_obj = df[close_cols]
    else:
        if "Close" not in df.columns:
            raise RuntimeError(f"Coluna Close não encontrada. Colunas: {list(df.columns)}")
        close_obj = df["Close"]

    # Se vier DataFrame, pega 1ª coluna
    if isinstance(close_obj, pd.DataFrame):
        if close_obj.shape[1] < 1:
            raise RuntimeError("Close retornou DataFrame vazio.")
        close_obj = close_obj.iloc[:, 0]

    s = close_obj.dropna().copy()

    idx = pd.to_datetime(s.index)
    
    # Se vier com timezone (tz-aware), remove o timezone para ficar tz-naive
    # (padroniza para evitar "Cannot compare tz-naive and tz-aware timestamps")
    try:
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
    except TypeError:
        # Em alguns casos o idx pode não suportar tz_convert; remove tz diretamente
        try:
            idx = idx.tz_localize(None)
        except Exception:
            pass
    
    s.index = idx
    return s



def series_to_df(s: pd.Series) -> pd.DataFrame:
    df = s.rename("close").to_frame().reset_index()
    df = df.rename(columns={df.columns[0]: "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return df


def load_existing():
    if not OUT_PATH.exists():
        return pd.DataFrame(columns=["date", "close"]), None

    payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    pts = payload.get("series", [])
    if not pts:
        return pd.DataFrame(columns=["date", "close"]), None

    df = pd.DataFrame(pts)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return df[["date", "close"]], df["date"].max().date().isoformat()


def fetch_close_history(start: datetime, end: datetime) -> pd.Series:
    t = yf.Ticker(TICKER)
    hist = t.history(
        start=start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        actions=False,
    )
    print("DEBUG hist.tail(5):")
    print(hist.tail(5).to_string())
    return get_close_series(hist)


def fetch_close_download(start: datetime, end: datetime) -> pd.Series:
    raw = yf.download(
        TICKER,
        start=start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        progress=False,
        group_by="column",
        threads=False,
    )
    print("DEBUG raw.tail(5):")
    print(raw.tail(5).to_string())
    return get_close_series(raw)


def download_range_with_fallback(start: datetime, end: datetime) -> pd.Series:
    # 1) tenta history()
    s = fetch_close_history(start, end)
    assert_series_is_fresh(s, "history()")
    return s


def probe_last_date(end_dt: datetime) -> str:
    start = end_dt - timedelta(days=PROBE_DAYS)

    # tenta history()
    try:
        s = fetch_close_history(start, end_dt)
        assert_series_is_fresh(s, "probe history()")
        return pd.to_datetime(s.index.max()).date().isoformat()
    except Exception as e1:
        # fallback: download()
        s = fetch_close_download(start, end_dt)
        assert_series_is_fresh(s, "probe download()")
        return pd.to_datetime(s.index.max()).date().isoformat()


def main():
    df_existing, last_date_existing = load_existing()
    end_dt = datetime.utcnow() - timedelta(days=1)

    # --- Probe curto: existe dado novo? (com validação real) ---
    last_yahoo = probe_last_date(end_dt)
    print("DEBUG | last_yahoo:", last_yahoo, "| last_existing:", last_date_existing)

    # --- Se NÃO tem data nova, ainda assim pode ter revisão no último close ---
    if last_date_existing and last_yahoo <= last_date_existing:
        # pega o close mais recente do Yahoo (janela curta)
        start_probe = end_dt - timedelta(days=PROBE_DAYS)
        try:
            s_probe = fetch_close_history(start_probe, end_dt)
            assert_series_is_fresh(s_probe, "probe history()")
        except Exception:
            s_probe = fetch_close_download(start_probe, end_dt)
            assert_series_is_fresh(s_probe, "probe download()")
    
        s_probe = s_probe.dropna()
        yahoo_last_date = pd.to_datetime(s_probe.index.max()).date().isoformat()
        yahoo_last_close = float(s_probe.loc[s_probe.index.max()])
    
        # pega o último close gravado no JSON existente
        existing_last_close = None
        if not df_existing.empty:
            mask = df_existing["date"].dt.date == datetime.fromisoformat(last_date_existing).date()
            if mask.any():
                existing_last_close = float(df_existing.loc[mask, "close"].iloc[-1])
    
        # se é o mesmo dia e o close mudou, NÃO sai; força refresh do tail
        if (yahoo_last_date == last_date_existing) and (existing_last_close is not None) and (abs(yahoo_last_close - existing_last_close) > 1e-6):
            print(f"DEBUG | mesma data ({yahoo_last_date}), mas close mudou: existing={existing_last_close} yahoo={yahoo_last_close}. Forçando refresh.")
            # deixa passar (não retorna) para reconstruir df_all
        else:
            print("Sem dados novos.")
            return


    # --- Decide incremental vs bootstrap ---
    force_bootstrap = False
    if last_date_existing:
        age_existing = (_today_utc() - datetime.fromisoformat(last_date_existing).date()).days
        if age_existing > FORCE_BOOTSTRAP_IF_OLDER_THAN_DAYS:
            force_bootstrap = True

    if df_existing.empty or force_bootstrap:
        # Bootstrap: baixa 11 anos para garantir MM252 dentro dos 10 anos finais
        start_dt = (end_dt - pd.DateOffset(years=LEVEL_YEARS + 1)).to_pydatetime()

        # tenta history() com fallback para download() se necessário
        try:
            s_all = fetch_close_history(start_dt, end_dt)
            assert_series_is_fresh(s_all, "bootstrap history()")
        except Exception as e_hist:
            s_all = fetch_close_download(start_dt, end_dt)
            assert_series_is_fresh(s_all, "bootstrap download()")

        df_all = series_to_df(s_all)
    else:
        last_dt = pd.to_datetime(last_date_existing)
        start_dt = (last_dt - timedelta(days=RECALC_BUFFER_DAYS)).to_pydatetime()

        try:
            s_tail = fetch_close_history(start_dt, end_dt)
            assert_series_is_fresh(s_tail, "tail history()")
        except Exception as e_hist:
            s_tail = fetch_close_download(start_dt, end_dt)
            assert_series_is_fresh(s_tail, "tail download()")

        df_tail = series_to_df(s_tail)
        df_prefix = df_existing[df_existing["date"] < pd.to_datetime(start_dt)]
        df_all = pd.concat([df_prefix, df_tail], ignore_index=True)

    df_all = df_all.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

    # --- Médias móveis (SEM remover linhas) ---
    df_all["mm50"] = df_all["close"].rolling(MM_SHORT).mean()
    df_all["mm252"] = df_all["close"].rolling(MM_LONG).mean()

    # --- Corte FINAL para 10 anos ---
    cutoff = df_all["date"].max() - pd.DateOffset(years=LEVEL_YEARS)
    df_all = df_all[df_all["date"] >= cutoff].reset_index(drop=True)

    # --- JSON ---
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
