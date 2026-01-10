import json
import calendar
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests

OUT_PATH = Path("data/cafe/series/gscpi_fretes.json")

# Fonte oficial (NY Fed – GSCPI)
GSCPI_URL_XLSX = "https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx"

def to_eom(dt: pd.Timestamp) -> pd.Timestamp:
    """Converte uma data qualquer para o fim do mês (EOM), preservando ano/mês."""
    year = int(dt.year)
    month = int(dt.month)
    last_day = calendar.monthrange(year, month)[1]
    return pd.Timestamp(year=year, month=month, day=last_day)

def load_source_series() -> pd.DataFrame:
    """
    Baixa o XLSX do NY Fed e encontra um sheet que contenha colunas Date e GSCPI.
    Retorna DataFrame com colunas: date (Timestamp EOM), close (float).
    """
    r = requests.get(GSCPI_URL_XLSX, timeout=120)
    r.raise_for_status()

    wb = pd.read_excel(BytesIO(r.content), sheet_name=None, engine="xlrd")

    series_df = None
    used_sheet = None

    for name, df in wb.items():
        if df is None or df.empty:
            continue
        cols_upper = [str(c).strip().upper() for c in df.columns]
        if "DATE" in cols_upper and "GSCPI" in cols_upper:
            # Preserva a capitalização original das colunas
            # (algumas abas podem estar como "Date" e "GSCPI")
            # Vamos acessar por nomes exatos via matching.
            date_col = df.columns[cols_upper.index("DATE")]
            gscpi_col = df.columns[cols_upper.index("GSCPI")]
            series_df = df[[date_col, gscpi_col]].copy()
            series_df.columns = ["Date", "GSCPI"]
            used_sheet = name
            break

    if series_df is None:
        raise RuntimeError("Não encontrei colunas Date/GSCPI no XLSX do NY Fed (layout pode ter mudado).")

    series_df["Date"] = pd.to_datetime(series_df["Date"], errors="coerce")
    series_df["GSCPI"] = pd.to_numeric(series_df["GSCPI"], errors="coerce")
    series_df = series_df.dropna().sort_values("Date").reset_index(drop=True)

    # Converte para fim de mês (EOM) e remove duplicatas (se houver)
    series_df["date"] = series_df["Date"].apply(to_eom)
    series_df["close"] = series_df["GSCPI"].astype(float)
    out = series_df[["date", "close"]].drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

    if len(out) < 3:
        raise RuntimeError("Série GSCPI tem menos de 3 pontos válidos após limpeza.")

    print(f"OK: XLSX lido. Sheet usado: {used_sheet}. Última data fonte (EOM): {out.iloc[-1]['date'].strftime('%Y-%m-%d')}")
    return out

def read_existing_json():
    """
    Lê o JSON atual do repo, se existir.
    Esperado:
    {
      "id": "gscpi",
      "name": "...",
      "source": "...",
      "series": [{"date":"YYYY-MM-DD","close":...,"mm50":null,"mm252":null}, ...]
    }
    """
    if not OUT_PATH.exists():
        return {
            "id": "gscpi",
            "name": "GSCPI (FRETES)",
            "source": "NY Fed – Global Supply Chain Pressure Index (GSCPI)",
            "series": []
        }

    payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))

    if "series" not in payload or not isinstance(payload["series"], list):
        raise RuntimeError("JSON existente de GSCPI não tem a chave 'series' como lista.")

    return payload

def main():
    source = load_source_series()
    payload = read_existing_json()

    # Determina max(date) existente
    existing_series = payload.get("series", [])
    if existing_series:
        existing_dates = []
        for p in existing_series:
            d = p.get("date")
            if d:
                existing_dates.append(pd.to_datetime(d, errors="coerce"))
        existing_dates = [d for d in existing_dates if pd.notna(d)]
        max_existing = max(existing_dates) if existing_dates else None
    else:
        max_existing = None

    # Filtra apenas meses novos (append-only)
    if max_existing is not None:
        to_add = source[source["date"] > max_existing].copy()
    else:
        to_add = source.copy()

    # Também evita duplicata por segurança
    existing_date_set = set(p.get("date") for p in existing_series if p.get("date"))
    to_add["date_str"] = to_add["date"].dt.strftime("%Y-%m-%d")
    to_add = to_add[~to_add["date_str"].isin(existing_date_set)].copy()

    added = 0
    if not to_add.empty:
        for _, row in to_add.iterrows():
            existing_series.append({
                "date": row["date"].strftime("%Y-%m-%d"),
                "close": float(row["close"]),
                # Mantém padrão do seu JSON atual
                "mm50": None,
                "mm252": None,
            })
        added = len(to_add)

    # Garante ordenação ascendente por date
    existing_series = sorted(existing_series, key=lambda x: x.get("date", ""))

    payload.update({
        "id": "gscpi",
        "name": "GSCPI (FRETES)",
        "source": "NY Fed – Global Supply Chain Pressure Index (GSCPI)",
        "series": existing_series,
        # opcional: metadado de update (não quebra o front, mas se você preferir remover, removemos depois)
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "append_only": True
    })

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: gscpi_fretes.json atualizado (append-only, NY Fed).")
    print("Meses novos adicionados:", added)
    if payload["series"]:
        print("Última data no JSON:", payload["series"][-1]["date"])
        print("Último close no JSON:", payload["series"][-1]["close"])

if __name__ == "__main__":
    main()
