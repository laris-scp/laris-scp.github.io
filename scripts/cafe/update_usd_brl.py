import json
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# =========================
# CONFIG
# =========================
LOOKBACK_YEARS = 10  # compatível com seu Colab (limite prático para série diária do BCB)
MM_LONG = 252
MM_SHORT = 50

OUT_PATH = Path("data/cafe/series/usd_brl.json")

# Metadados (mantém o "contrato" do JSON)
META = {
    "id": "usd_brl",
    "name": "USD/BRL",
    "unit": "R$",
    "frequency": "Diária",
}

def fetch_bcb_sgs_1(start: str, end: str) -> pd.DataFrame:
    url = (
        "https://api.bcb.gov.br/dados/serie/bcdata.sgs.1/dados"
        f"?formato=json&dataInicial={start}&dataFinal={end}"
    )
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    df = pd.DataFrame(r.json())
    df["data"] = pd.to_datetime(df["data"], dayfirst=True, errors="coerce")
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce")
    df = df.dropna().sort_values("data").reset_index(drop=True)
    return df

def main():
    end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=LOOKBACK_YEARS * 365 - 5)

    start = start_dt.strftime("%d/%m/%Y")
    end = end_dt.strftime("%d/%m/%Y")

    df = fetch_bcb_sgs_1(start, end)

    # Médias móveis
    df["mm252"] = df["valor"].rolling(MM_LONG).mean()
    df["mm50"] = df["valor"].rolling(MM_SHORT).mean()

    # Remove NaNs (igual ao Colab)
    df = df.dropna().reset_index(drop=True)

    if len(df) < MM_LONG + 60:
        raise RuntimeError(f"Poucos dados após MM: {len(df)}")

    series = []
    for _, row in df.iterrows():
        series.append(
            {
                "date": row["data"].strftime("%Y-%m-%d"),
                "close": float(row["valor"]),
                "mm50": float(row["mm50"]),
                "mm252": float(row["mm252"]),
            }
        )

    payload = {**META, "series": series}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    print("OK: usd_brl.json gerado.")
    print("Última data:", series[-1]["date"])
    print("Último close:", series[-1]["close"])

if __name__ == "__main__":
    main()
