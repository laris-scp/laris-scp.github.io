import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

# =========================
# CONFIG (igual ao Colab)
# =========================
TICKER = "KC=F"
MM_LONG = 252
MM_SHORT = 50
LEVEL_YEARS = 10
MIN_POINTS_FOR_FULL = 260

OUT_PATH = Path("data/cafe/series/preco_arabica.json")

META = {
    "id": "preco_arabica",
    "name": "PREÇO ARABICA",
    "unit": "US¢/lb",
    "frequency": "Diária",
}

def get_close_series(df: pd.DataFrame) -> pd.Series:
    """Extrai Close de forma robusta (Yahoo às vezes vem MultiIndex)."""
    if df.empty:
        raise RuntimeError("Yahoo retornou vazio para KC=F.")

    if isinstance(df.columns, pd.MultiIndex):
        # tenta df["Close"] primeiro
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

def main():
    # 1) Baixa histórico máximo diário
    raw = yf.download(
        TICKER,
        period="max",
        interval="1d",
        auto_adjust=False,
        progress=False,
        group_by="column",
    )

    s = get_close_series(raw)

    # 2) Janela de 10 anos (igual ao Colab)
    cutoff = s.index.max() - pd.DateOffset(years=LEVEL_YEARS)
    s10 = s[s.index >= cutoff].copy()

    if len(s10) < MIN_POINTS_FOR_FULL:
        raise RuntimeError(f"Histórico insuficiente nos últimos {LEVEL_YEARS} anos ({len(s10)} pontos).")

    # 3) MMs
    mm252 = s10.rolling(MM_LONG).mean()
    mm50 = s10.rolling(MM_SHORT).mean()

    base = pd.concat(
        [s10.rename("close"), mm50.rename("mm50"), mm252.rename("mm252")],
        axis=1
    ).dropna().copy()

    if base.empty:
        raise RuntimeError("Após calcular MM50/MM252, a base ficou vazia.")
    if len(base) < 3:
        raise RuntimeError("Base com menos de 3 pontos após MMs.")

    # 4) Monta JSON no mesmo layout do arquivo atual
    series = []
    for idx, row in base.iterrows():
        series.append({
            "date": pd.to_datetime(idx).strftime("%Y-%m-%d"),
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

    print("OK: preco_arabica.json gerado.")
    print("Última data:", series[-1]["date"])
    print("Último close:", series[-1]["close"])

if __name__ == "__main__":
    main()
