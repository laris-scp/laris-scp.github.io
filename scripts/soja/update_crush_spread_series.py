import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

# =========================
# CONFIG
# =========================
OUT_PATH = Path("data/soja/series/crush_spread.json")

TICKERS = {
    "zs": "ZS=F",  # soja - ¢/bushel
    "zm": "ZM=F",  # farelo - $/short ton
    "zl": "ZL=F",  # óleo - ¢/lb
}

# Fórmula CME oficial:
#   Crush ($/bushel) = (ZM × 0.022) + (ZL × 0.11) - (ZS/100)
# Onde:
#   ZM em $/short ton (entra direto)
#   ZL em ¢/lb (entra em centavos como manda a fórmula CME)
#   ZS em ¢/bushel (dividir por 100 para ter $/bushel)
#
# Constantes 0.022 = 22 (lbs de óleo por bushel)/1000 → unidade casa
#            0.11  = 11 (lbs de óleo por bushel) ?? veja documentação CME

# Bootstrap: 10 anos para trás (captura ciclo completo pré e pós renewable diesel)
BOOTSTRAP_PERIOD = "10y"
INCREMENTAL_PERIOD = "60d"  # 60 dias para ter folga em fins de semana/feriados/revisões

# Retry leve (yfinance já tem retry interno)
MAX_TRIES = 3
SLEEP_BETWEEN_TICKERS = 0.5


# =========================
# HELPERS
# =========================
def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _download_ticker(symbol: str, period: str) -> pd.Series:
    """Baixa série Close de um ticker, retorna Series indexada por data."""
    last_err = None
    for attempt in range(1, MAX_TRIES + 1):
        try:
            df = yf.download(symbol, period=period, progress=False, auto_adjust=False)
            if df is None or df.empty:
                last_err = f"vazio em tentativa {attempt}"
                time.sleep(2)
                continue

            close = df["Close"]
            # yfinance recente retorna DataFrame com MultiIndex; achata para Series
            if hasattr(close, "ndim") and close.ndim > 1:
                close = close.iloc[:, 0]

            close = pd.to_numeric(close, errors="coerce").dropna()
            if len(close) == 0:
                last_err = f"sem dados numéricos em tentativa {attempt}"
                time.sleep(2)
                continue

            return close

        except Exception as e:
            last_err = repr(e)
            time.sleep(2)

    raise RuntimeError(f"Falha ao baixar {symbol} após {MAX_TRIES} tentativas. Último erro: {last_err}")


def _build_crush_df(period: str) -> pd.DataFrame:
    """Baixa os 3 tickers, alinha em mesmo índice de datas, calcula crush."""
    series = {}
    for key, sym in TICKERS.items():
        print(f"  Baixando {sym} (period={period})...")
        series[key] = _download_ticker(sym, period)
        time.sleep(SLEEP_BETWEEN_TICKERS)

    df = pd.DataFrame(series).dropna()
    if df.empty:
        raise RuntimeError("DataFrame vazio após alinhar os 3 tickers.")

    # Fórmula CME
    df["crush"] = (df["zm"] * 0.022) + (df["zl"] * 0.11) - (df["zs"] / 100.0)

    df = df.reset_index()
    if "Date" in df.columns:
        df = df.rename(columns={"Date": "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    return df


def _load_existing() -> dict | None:
    if not OUT_PATH.exists():
        return None
    return json.loads(OUT_PATH.read_text(encoding="utf-8"))


def _merge_points(existing: list[dict], new_pts: list[dict]) -> list[dict]:
    """Merge por date: novos sobrescrevem antigos."""
    by_date = {p["date"]: p for p in existing if p.get("date")}
    for p in new_pts:
        if p.get("date"):
            by_date[p["date"]] = p
    merged = list(by_date.values())
    merged.sort(key=lambda x: x["date"])
    return merged


# =========================
# MAIN
# =========================
def main():
    print(">> Crush Spread (CBOT) | Iniciando")
    existing = _load_existing()

    if existing is None:
        period = BOOTSTRAP_PERIOD
        mode = "BOOTSTRAP"
    else:
        period = INCREMENTAL_PERIOD
        mode = "INCREMENTAL"

    print(f">> Modo: {mode}")

    df = _build_crush_df(period)
    print(f">> Coletados {len(df)} pontos. Primeiro: {df['date'].iloc[0]} | Último: {df['date'].iloc[-1]}")

    # Constrói pontos no formato esperado pelo painel
    new_pts = []
    for _, row in df.iterrows():
        new_pts.append({
            "date": str(row["date"]),
            "close": round(float(row["crush"]), 4),
            "zs": round(float(row["zs"]), 2),
            "zm": round(float(row["zm"]), 2),
            "zl": round(float(row["zl"]), 2),
        })

    if mode == "BOOTSTRAP":
        merged = new_pts
    else:
        old_pts = (existing or {}).get("data", [])
        if not isinstance(old_pts, list):
            old_pts = []
        merged = _merge_points(old_pts, new_pts)

    if len(merged) < 30:
        raise RuntimeError(f"Série final tem só {len(merged)} pontos. Bootstrap pode ter falhado.")

    # Calcular MM50 e MM252 sobre a série completa (não só sobre o batch novo)
    # Garante consistência mesmo no modo incremental
    merged_df = pd.DataFrame(merged).sort_values("date").reset_index(drop=True)
    close = pd.to_numeric(merged_df["close"], errors="coerce")
    merged_df["mm50"] = close.rolling(window=50, min_periods=50).mean()
    merged_df["mm252"] = close.rolling(window=252, min_periods=252).mean()

    # Reescreve a lista com mm50/mm252 incluídos
    merged = []
    for _, r in merged_df.iterrows():
        pt = {
            "date": str(r["date"]),
            "close": round(float(r["close"]), 4),
            "zs": round(float(r["zs"]), 2),
            "zm": round(float(r["zm"]), 2),
            "zl": round(float(r["zl"]), 2),
        }
        if pd.notna(r["mm50"]):
            pt["mm50"] = round(float(r["mm50"]), 4)
        if pd.notna(r["mm252"]):
            pt["mm252"] = round(float(r["mm252"]), 4)
        merged.append(pt)

    out = {
        "meta": {
            "id": "crush_spread",
            "title": "CRUSH SPREAD (CBOT)",
            "frequency": "Diária",
            "formula": "(ZM × 0.022) + (ZL × 0.11) - (ZS/100)",
            "unit": "$/bushel",
            "source": "Yahoo Finance (ZS=F, ZM=F, ZL=F)",
            "updated_at": _now_utc_str(),
        },
        "data": merged,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    last = merged[-1]
    print(f">> OK ({mode}). Total: {len(merged)} pontos.")
    print(f">> Último: date={last['date']} | crush=${last['close']:.3f}/bu")
    print(f"   ZS={last['zs']} | ZM={last['zm']} | ZL={last['zl']}")
    print(f"   MM50={last.get('mm50', '—')} | MM252={last.get('mm252', '—')}")


if __name__ == "__main__":
    main()
