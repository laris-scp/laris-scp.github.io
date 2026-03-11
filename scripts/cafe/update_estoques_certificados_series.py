import json
from pathlib import Path
from datetime import datetime
from io import BytesIO
import re
from difflib import get_close_matches

import pandas as pd
import requests


REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = REPO_ROOT / "data/cafe/series/estoques_certificados.json"

ICE_XLS_URL = "https://www.ice.com/publicdocs/futures_us_reports/coffee/EOM_KC_cert_stox_by_port_nov96-present.xls"


# Correção de escala (se XLS vier ~10x maior que o histórico)
RATIO_MIN = 8.0
RATIO_MAX = 12.0

# Backfill 1x: se o json existente tiver menos pontos que isso, refaz tudo (1996–hoje).
# Depois disso, vira append-only normal.
BACKFILL_MIN_POINTS = 200


def load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def as_date_str(d) -> str:
    # d pode ser datetime/date/string já ISO
    if isinstance(d, str):
        return d[:10]
    return pd.to_datetime(d).date().isoformat()


def ensure_series_list(obj):
    """
    Formato esperado:
    { "meta": {...}, "series": [ { "date":"YYYY-MM-DD", "value":..., "mm12m":... }, ... ] }
    Aceita legado com chave 'data'.
    """
    if isinstance(obj, dict):
        if isinstance(obj.get("series"), list):
            return obj.get("meta", {}), obj["series"]
        if isinstance(obj.get("data"), list):
            return obj.get("meta", {}), obj["data"]
    return {}, []

MONTH_MAP = {
    "january": "January",
    "jan": "January",
    "janeiro": "January",
    "fevereiro": "February",
    "february": "February",
    "feb": "February",
    "março": "March",
    "marco": "March",
    "march": "March",
    "mar": "March",
    "abril": "April",
    "april": "April",
    "abr": "April",
    "apr": "April",
    "maio": "May",
    "may": "May",
    "junho": "June",
    "june": "June",
    "jun": "June",
    "julho": "July",
    "july": "July",
    "jul": "July",
    "agosto": "August",
    "august": "August",
    "ago": "August",
    "aug": "August",
    "setembro": "September",
    "september": "September",
    "sep": "September",
    "sept": "September",
    "outubro": "October",
    "october": "October",
    "out": "October",
    "oct": "October",
    "novembro": "November",
    "november": "November",
    "nov": "November",
    "dezembro": "December",
    "december": "December",
    "dez": "December",
    "dec": "December",
}

MONTH_KEYS = list(MONTH_MAP.keys())


def _clean_date_text(x) -> str:
    if pd.isna(x):
        return ""

    s = str(x).strip()
    if not s:
        return ""

    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r",\s*(\d{4})$", r", \1", s)

    return s


def _normalize_month_token(token: str) -> str:
    t = token.strip().lower()

    # remove pontuação nas pontas
    t = re.sub(r"^[^A-Za-zÀ-ÿ]+|[^A-Za-zÀ-ÿ]+$", "", t)

    # correções diretas conhecidas / idioma
    if t in MONTH_MAP:
        return MONTH_MAP[t]

    # tentativa por similaridade
    match = get_close_matches(t, MONTH_KEYS, n=1, cutoff=0.70)
    if match:
        return MONTH_MAP[match[0]]

    return token


def _parse_ice_date(x):
    # se já veio como data do Excel / timestamp, deixa o pandas resolver direto
    if isinstance(x, (datetime, pd.Timestamp)):
        return pd.to_datetime(x, errors="coerce")

    s = _clean_date_text(x)
    if not s:
        return pd.NaT

    # tenta achar o primeiro token textual como mês
    parts = s.split(" ")
    if parts:
        parts[0] = _normalize_month_token(parts[0])
        s = " ".join(parts)

    # tentativa principal
    dt = pd.to_datetime(s, errors="coerce")
    if pd.notna(dt):
        return dt

    # fallback explícito: "Month DD, YYYY"
    try:
        return pd.to_datetime(datetime.strptime(s, "%B %d, %Y"))
    except Exception:
        return pd.NaT


def download_ice_xls(url: str) -> pd.DataFrame:
    r = requests.get(url, timeout=60)
    r.raise_for_status()

    raw = pd.read_excel(BytesIO(r.content), header=None, engine="xlrd")

    # Padrão: data na col 1 e total na col 10
    ice = raw.iloc[:, [1, 10]].copy()
    ice.columns = ["DATA", "TOTAL"]

    # guarda o texto bruto para debug/validação
    ice["DATA_RAW"] = ice["DATA"]

    # parsing robusto: aceita inglês, português e pequenos erros de digitação
    ice["DATA"] = ice["DATA"].apply(_parse_ice_date)
    ice["TOTAL"] = pd.to_numeric(ice["TOTAL"], errors="coerce")

    # validação: se as últimas linhas com TOTAL tiverem data inválida, quebra o job
    recent_check = ice.loc[ice["TOTAL"].notna(), ["DATA_RAW", "DATA", "TOTAL"]].tail(3)
    bad_recent = recent_check[recent_check["DATA"].isna()]

    if not bad_recent.empty:
        exemplos = " | ".join(str(x) for x in bad_recent["DATA_RAW"].tolist())
        raise ValueError(
            f"Falha ao interpretar data nas linhas mais recentes do XLS da ICE: {exemplos}"
        )

    # limpeza final
    ice = ice.dropna(subset=["DATA", "TOTAL"]).sort_values("DATA").reset_index(drop=True)

    # O arquivo já é EOM; guardamos como YYYY-MM-DD
    ice["DATE"] = ice["DATA"].dt.date.astype(str)
    ice["TOTAL"] = ice["TOTAL"].astype(float)

    return ice[["DATE", "TOTAL"]]


def main():
    existing_raw = load_json(OUT_PATH)
    meta_in, series_in = ensure_series_list(existing_raw)

    existing_df = pd.DataFrame(series_in) if series_in else pd.DataFrame(columns=["date", "value", "mm12m"])

    # limpa e normaliza histórico existente
    if not existing_df.empty:
        existing_df["date"] = existing_df["date"].apply(as_date_str)
        existing_df["value"] = pd.to_numeric(existing_df.get("value"), errors="coerce")
        existing_df = (
            existing_df
            .dropna(subset=["date", "value"])
            .sort_values("date")
            .reset_index(drop=True)
        )

    if existing_df.empty:
        max_date = None
        last_hist_total = None
    else:
        max_date = existing_df["date"].max()
        last_hist_total = float(
            existing_df.loc[existing_df["date"] == max_date, "value"].iloc[-1]
        )

    # baixa ICE
    ice = download_ice_xls(ICE_XLS_URL)

    # ajuste de escala (10x)
    scale_factor = 1.0
    if max_date is not None and last_hist_total is not None and last_hist_total > 0:
        # tenta pegar o total do ICE na mesma data; senão usa o último do ICE
        ice_same = ice.loc[ice["DATE"] == max_date, "TOTAL"]
        ice_ref = float(ice_same.iloc[-1]) if len(ice_same) else float(ice.iloc[-1]["TOTAL"])
        ratio = ice_ref / last_hist_total
        if RATIO_MIN <= ratio <= RATIO_MAX:
            scale_factor = 0.1

    if scale_factor != 1.0:
        ice["TOTAL"] = ice["TOTAL"] * scale_factor

    # decide o que adicionar (backfill 1x se histórico ainda é curto)
    need_backfill = (len(existing_df) < BACKFILL_MIN_POINTS)

    if max_date is None or need_backfill:
        ice_new = ice.copy()
    else:
        ice_new = ice.loc[ice["DATE"] > max_date].copy()

    # merge (append-only) + dedupe
    base = existing_df[["date", "value"]].copy() if not existing_df.empty else pd.DataFrame(columns=["date", "value"])
    add = pd.DataFrame({"date": ice_new["DATE"], "value": ice_new["TOTAL"]})

    merged = pd.concat([base, add], ignore_index=True)
    merged["date"] = merged["date"].apply(as_date_str)
    merged["value"] = pd.to_numeric(merged["value"], errors="coerce")
    merged = (
        merged
        .dropna(subset=["date", "value"])
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )

    # calcula MM12M (12 pontos)
    merged["mm12m"] = merged["value"].rolling(window=12, min_periods=12).mean()

    # monta saída no padrão do site: value = TOTAL, mm12m opcional
    series_out = []
    for _, row in merged.iterrows():
        item = {"date": row["date"], "value": float(row["value"])}
        if pd.notna(row["mm12m"]):
            item["mm12m"] = float(row["mm12m"])
        series_out.append(item)

    meta_out = {
        **(meta_in or {}),
        "id": "estoques_certificados",
        "title": "ICE Certified Stocks (EOM) — Coffee C",
        "source": ICE_XLS_URL,
        "frequency": "Mensal (EOM)",
        "unit": "contratos (ICE) / total reportado",
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": f"append-only; full history; escala x{scale_factor}",
    }

    out = {"meta": meta_out, "series": series_out}
    save_json(OUT_PATH, out)

    print(f"OK: estoques_certificados.json atualizado. Pontos: {len(series_out)} | escala x{scale_factor} | novos: {len(add)}")


if __name__ == "__main__":
    main()
