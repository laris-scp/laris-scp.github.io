import json
import os
import calendar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np
import xarray as xr
import cdsapi


SERIES_PATH = Path("data/cafe/series/clima_soil.json")

# CDS / ERA5-Land
CDS_URL = "https://cds.climate.copernicus.eu/api"
DATASET = "reanalysis-era5-land-monthly-means"
VARIABLE = "volumetric_soil_water_layer_3"  # swvl3
AREA = [-15, -55, -25, -40]  # N, W, S, E (conforme seu código)

# Revisão: reprocessar e sobrescrever os últimos N meses
REVISION_MONTHS = 6

#teste novamente.

# Metadados do JSON (mantém padrão do seu arquivo)
SERIES_ID = "clima_soil"
SERIES_NAME = "CLIMA (SOIL MOISTURE)"
UNIT = "m³/m³"
FREQUENCY = "Mensal"

@dataclass
class Window:
    start: pd.Timestamp
    end: pd.Timestamp  # inclusive, mês corrente

def month_floor(ts: pd.Timestamp) -> pd.Timestamp:
    """Retorna primeiro dia do mês (YYYY-MM-01)."""
    return pd.Timestamp(year=int(ts.year), month=int(ts.month), day=1)

def add_months(ts: pd.Timestamp, n: int) -> pd.Timestamp:
    """Soma n meses mantendo sempre no dia 1."""
    y = int(ts.year)
    m = int(ts.month) + n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return pd.Timestamp(year=y, month=m, day=1)

def iter_months(start: pd.Timestamp, end: pd.Timestamp):
    """Gera meses (YYYY-MM-01) de start até end inclusive."""
    cur = month_floor(start)
    end = month_floor(end)
    while cur <= end:
        yield cur
        cur = add_months(cur, 1)

def ensure_cdsapirc_from_env():
    """
    Cria ~/.cdsapirc no runner a partir de env var CDS_API_KEY.
    Espera token puro (sem UID:), conforme CDS atual.
    """
    key = os.environ.get("CDS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CDS_API_KEY não encontrado no ambiente (GitHub Secrets).")

    p = Path.home() / ".cdsapirc"
    p.write_text(f"url: {CDS_URL}\nkey: {key}\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass

def load_existing_series():
    if not SERIES_PATH.exists():
        raise RuntimeError(f"Arquivo não encontrado no repo: {SERIES_PATH}")

    payload = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    series = payload.get("series", [])
    if not isinstance(series, list) or len(series) == 0:
        raise RuntimeError("clima_soil.json existe, mas 'series' está vazia ou inválida.")

    df = pd.DataFrame(series)
    if "date" not in df.columns or "close" not in df.columns:
        raise RuntimeError("clima_soil.json inválido: faltam campos 'date' e/ou 'close'.")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

    if df.empty:
        raise RuntimeError("Histórico existente ficou vazio após limpeza.")
    return payload, df

def compute_download_window(df_hist: pd.DataFrame) -> Window:
    last_date = pd.to_datetime(df_hist["date"].max())
    last_month = month_floor(last_date)

    # Janela revisada: últimos 6 meses (incluindo o último mês do histórico)
    start = add_months(last_month, -REVISION_MONTHS)
    # Baixa até o mês atual (mensal) — mantém no 1o dia do mês
    end = month_floor(pd.Timestamp.utcnow())

    # Garantia de sanidade: start não pode ser maior que end
    if start > end:
        start = end

    return Window(start=start, end=end)

def cds_retrieve_monthly_swvl3(window: Window, out_nc: Path) -> Path:
    months = list(iter_months(window.start, window.end))

    years = sorted({str(int(m.year)) for m in months})
    mm = sorted({f"{int(m.month):02d}" for m in months})

    client = cdsapi.Client()
    req = {
        "product_type": ["monthly_averaged_reanalysis"],
        "variable": [VARIABLE],
        "year": years,
        "month": mm,
        "time": ["00:00"],
        "data_format": "netcdf",
        "download_format": "unarchived",
        "area": AREA,
    }

    print(f"Baixando CDS: {DATASET} | {VARIABLE} | years={years[0]}..{years[-1]} | months={mm[0]}..{mm[-1]}")
    client.retrieve(DATASET, req).download(str(out_nc))
    if not out_nc.exists():
        raise RuntimeError("Download do NetCDF não gerou arquivo (out_nc inexistente).")
    return out_nc

def netcdf_to_series_df(nc_path: Path) -> pd.DataFrame:
    ds = xr.open_dataset(str(nc_path))

    # A variável efetiva no NetCDF costuma ser "swvl3"
    if "swvl3" in ds.variables:
        da = ds["swvl3"]
    else:
        # fallback: tenta inferir a variável de solo
        cand = [v for v in ds.data_vars if "swvl" in v.lower()]
        if not cand:
            raise RuntimeError(f"Não encontrei 'swvl3' no NetCDF. Variáveis disponíveis: {list(ds.data_vars)}")
        da = ds[cand[0]]

    # Descobrir dimensão temporal
    time_dim = None
    for d in ["valid_time", "time", "date"]:
        if d in da.dims or d in ds.coords:
            time_dim = d
            break
    if time_dim is None:
        # tenta o primeiro eixo que pareça temporal
        for d in da.dims:
            if "time" in d.lower():
                time_dim = d
                break
    if time_dim is None:
        raise RuntimeError(f"Não consegui identificar dimensão temporal. dims={da.dims}")

    # Agrega bbox: média lat/lon
    # ERA5-Land geralmente vem com latitude/longitude
    dims = set(da.dims)
    for dim in ["latitude", "longitude"]:
        if dim in dims:
            da = da.mean(dim=dim, skipna=True)

    # Converte para DataFrame
    df = da.to_dataframe(name="swvl3").reset_index()

    # Nome da coluna temporal pode variar
    if "valid_time" in df.columns:
        df = df.rename(columns={"valid_time": "date"})
    elif "time" in df.columns:
        df = df.rename(columns={"time": "date"})
    elif time_dim in df.columns:
        df = df.rename(columns={time_dim: "date"})
    else:
        # tenta achar coluna datetime
        dt_cols = [c for c in df.columns if "time" in c.lower() or "date" in c.lower()]
        if not dt_cols:
            raise RuntimeError(f"Não achei coluna temporal no dataframe. cols={df.columns.tolist()}")
        df = df.rename(columns={dt_cols[0]: "date"})

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["swvl3"] = pd.to_numeric(df["swvl3"], errors="coerce")
    df = df.dropna(subset=["date", "swvl3"]).sort_values("date").reset_index(drop=True)

    # Padroniza para 1º dia do mês (YYYY-MM-01), conforme seu JSON atual
    df["date"] = df["date"].apply(month_floor)

   # close = swvl3
    out = df[["date", "swvl3"]].rename(columns={"swvl3": "close"}).drop_duplicates(subset=["date"], keep="last")
    out = out.sort_values("date").reset_index(drop=True)
    
    # ========= NOVO: stress hídrico 6m (contínuo) =========
    WIN_ACC = 6  # meses no acumulado
    
    tmp = out.copy()
    tmp["date"] = pd.to_datetime(tmp["date"])
    tmp = tmp.sort_values("date").reset_index(drop=True)
    
    # baseline mensal (média histórica do mês)
    tmp["month"] = tmp["date"].dt.month
    baseline_month = tmp.groupby("month")["close"].mean()
    tmp = tmp.merge(baseline_month.rename("baseline"), on="month", how="left")
    
    # deficit = baseline - swvl3 (truncado em 0)
    tmp["deficit"] = (tmp["baseline"] - tmp["close"]).clip(lower=0)
    
    # stress_6m = soma do deficit nos últimos 6 meses
    tmp["stress_6m"] = tmp["deficit"].rolling(WIN_ACC, min_periods=WIN_ACC).sum()
    
    # devolve só o que importa
    out = tmp[["date", "close", "stress_6m"]].copy()
    out = out.sort_values("date").reset_index(drop=True)
    # ======================================================


    if out.empty:
        raise RuntimeError("Série nova ficou vazia após processamento do NetCDF.")

    return out

def merge_revision_window(df_hist: pd.DataFrame, df_new: pd.DataFrame, window: Window) -> pd.DataFrame:
    # Parte antiga (antes do start da revisão) fica intocada
    cutoff = window.start
    df_old = df_hist[df_hist["date"] < cutoff].copy()

    # Tudo >= cutoff vem da fonte (sobrescreve)
    df_new2 = df_new[df_new["date"] >= cutoff].copy()

    # Merge final
    df_all = pd.concat([df_old, df_new2], ignore_index=True)
    df_all = df_all.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)
    return df_all

def write_json(payload_base: dict, df_all: pd.DataFrame):
    # ========= NOVO: calcular stress_6m no histórico final =========
    WIN_ACC = 6
    
    tmp = df_all.copy()
    tmp["date"] = pd.to_datetime(tmp["date"], errors="coerce")
    tmp["close"] = pd.to_numeric(tmp["close"], errors="coerce")
    tmp = tmp.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    
    tmp["month"] = tmp["date"].dt.month
    
    baseline_month = tmp.groupby("month")["close"].mean()
    tmp = tmp.merge(baseline_month.rename("baseline"), on="month", how="left")
    
    tmp["deficit"] = (tmp["baseline"] - tmp["close"]).clip(lower=0)
    tmp["stress_6m"] = tmp["deficit"].rolling(WIN_ACC, min_periods=WIN_ACC).sum()
    # ===============================================================

    series_out = []
    for _, r in tmp.iterrows():
        series_out.append({
            "date": pd.to_datetime(r["date"]).strftime("%Y-%m-%d"),
            "close": float(r["close"]),
            "stress_6m": None if pd.isna(r.get("stress_6m")) else float(r["stress_6m"]),
        })


    payload = {
        "id": SERIES_ID,
        "name": SERIES_NAME,
        "unit": UNIT,
        "frequency": FREQUENCY,
        "series": series_out,
        # metadados úteis (não quebram seu front; se você preferir, removemos depois)
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "revision_months": REVISION_MONTHS,
        "source": "ERA5-Land (ECMWF/Copernicus) — swvl3 (Volumetric soil water layer 3), média regional Brasil cafeeiro (bbox [-15, -55, -25, -40]).",
    }

    SERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SERIES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    ensure_cdsapirc_from_env()

    payload_base, df_hist = load_existing_series()
    window = compute_download_window(df_hist)

    print("Histórico existente:",
          "n=", len(df_hist),
          "| min=", df_hist["date"].min().date(),
          "| max=", df_hist["date"].max().date())

    print("Janela revisão:",
          window.start.strftime("%Y-%m-%d"),
          "->",
          window.end.strftime("%Y-%m-%d"),
          f"(REVISION_MONTHS={REVISION_MONTHS})")

    out_nc = Path("tmp_clima_soil.nc")
    nc_path = cds_retrieve_monthly_swvl3(window, out_nc)

    df_new = netcdf_to_series_df(nc_path)
    print("Série nova (fonte):",
          "n=", len(df_new),
          "| min=", df_new["date"].min().date(),
          "| max=", df_new["date"].max().date())

    df_all = merge_revision_window(df_hist, df_new, window)

    print("Série final (merge):",
          "n=", len(df_all),
          "| min=", df_all["date"].min().date(),
          "| max=", df_all["date"].max().date())

    # Sanidade: deve sempre ter ao menos o que já existia (não pode encolher drasticamente)
    if len(df_all) < len(df_hist) * 0.95:
        raise RuntimeError("Merge reduziu demais o histórico. Abortando para evitar truncamento indevido.")

    write_json(payload_base, df_all)
    print("OK: clima_soil.json atualizado com revisão dos últimos 6 meses.")

if __name__ == "__main__":
    main()
