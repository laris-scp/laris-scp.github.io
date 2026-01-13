# ==========================================================
# FERTILIZANTE (UREA) — SÉRIE (JSON) PARA O SITE
# Fonte: World Bank – Pink Sheet (CMO)
# Parsing: mesmo padrão robusto do seu Colab (linhas 4/5)
# ==========================================================

import json
import calendar
from io import BytesIO
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np
import requests
import unicodedata

# =========================
# CONFIG
# =========================
OUT_PATH = Path("data/cafe/series/fertilizante_urea.json")

MM_SHORT = 4
MM_LONG = 12

# Use exatamente a URL que você já usa no Colab (a mais recente)
URL_XLS = "https://thedocs.worldbank.org/en/doc/18675f1d1639c7a34d463f59263ba0a2-0050012025/related/CMO-Historical-Data-Monthly.xlsx"
SHEET = "Monthly Prices"

# =========================
# HELPERS
# =========================
def _norm(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ASCII", "ignore").decode("ASCII").upper().strip()

def yyyymm_to_eom(yyyymm: int) -> str:
    year = yyyymm // 100
    month = yyyymm % 100
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"

def load_existing_last_date():
    if not OUT_PATH.exists():
        return None
    payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    series = payload.get("series", [])
    if not series:
        return None
    return series[-1].get("date")

# =========================
# MAIN
# =========================
def main():
    last_saved_date = load_existing_last_date()

    # 1) DOWNLOAD XLS (robusto)
    r = requests.get(URL_XLS, timeout=120)
    r.raise_for_status()

    # 2) LOAD WORLD BANK XLS (HEADER ROBUSTO — IGUAL AO COLAB)
    raw = pd.read_excel(BytesIO(r.content), sheet_name=SHEET, header=None)

    if raw is None or raw.empty:
        raise RuntimeError("Aba 'Monthly Prices' veio vazia. Verifique URL/aba no XLS.")

    # Linha 4 = nomes principais | Linha 5 = unidades
    h1 = raw.iloc[4].astype(str)
    h2 = raw.iloc[5].astype(str)

    cols = []
    for a, b in zip(h1, h2):
        name = f"{a} {b}".strip()
        name = name.replace("nan", "").strip()
        cols.append(name)

    df = raw.iloc[6:].copy()
    df.columns = cols

    # Primeira coluna = data
    date_col = df.columns[0]
    df = df.rename(columns={date_col: "DATE_RAW"})

    # Identifica coluna da UREIA (mesma regra do Colab)
    urea_cols = [
        c for c in df.columns
        if "UREA" in _norm(c) and "$" in c
    ]
    if not urea_cols:
        raise RuntimeError(f"Coluna de UREIA não encontrada. Colunas disponíveis: {df.columns.tolist()}")

    UREA_COL = urea_cols[0]

    df = df[["DATE_RAW", UREA_COL]].dropna().copy()

    # DATE_RAW vem como '1960M01'
    df["YYYYMM"] = df["DATE_RAW"].astype(str).str.replace("M", "", regex=False).astype(int)
    df["close"] = pd.to_numeric(df[UREA_COL], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("YYYYMM").reset_index(drop=True)

    if df.empty:
        raise RuntimeError("Série de Ureia ficou vazia após limpeza.")

    # 3) DATA FIM DE MÊS
    df["date"] = df["YYYYMM"].apply(yyyymm_to_eom)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    # 4) EARLY-EXIT POR DATA (regra do projeto)
    last_date_new = df.iloc[-1]["date"].strftime("%Y-%m-%d")
    if last_saved_date is not None and last_saved_date == last_date_new:
        print(f"Sem dados novos para fertilizante_urea. Última data: {last_date_new}")
        return

    # 5) MÉDIAS MÓVEIS (padrão A: mm4m/mm12m)
    s = pd.Series(df["close"].values, index=df["date"])
    mm4m = s.rolling(MM_SHORT).mean()
    mm12m = s.rolling(MM_LONG).mean()

    # mantém somente pontos onde as MMs existem (igual sua lógica do painel)
    out = []
    for dt in df["date"]:
        if pd.isna(mm4m.loc[dt]) or pd.isna(mm12m.loc[dt]):
            continue
        out.append({
            "date": dt.strftime("%Y-%m-%d"),
            "close": float(s.loc[dt]),
            "mm4m": float(mm4m.loc[dt]),
            "mm12m": float(mm12m.loc[dt]),
        })

    if len(out) < 3:
        raise RuntimeError("Histórico insuficiente após cálculo de MM4M/MM12M.")

    payload = {
        "id": "fertilizante_urea",
        "name": "FERTILIZANTE (UREA)",
        "unit": "US$/t",
        "frequency": "Mensal",
        "source": "World Bank – Commodity Markets Outlook (Pink Sheet, Urea $/mt)",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "series": out
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK — FERTILIZANTE (UREA) série atualizada.")
    print("Última data:", out[-1]["date"], "| Último valor:", out[-1]["close"])


if __name__ == "__main__":
    main()
