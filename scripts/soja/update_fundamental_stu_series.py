# scripts/soja/update_fundamental_stu_series.py
# =============================================================================
# FUNDAMENTAL S&D (STU Global) — Série + Decomposição (Top 6) via USDA FAS PSD API
# Objetivo: gerar/atualizar data/soja/series/fundamental_stu.json (consumido pelo site)
#
# Regras (contrato fechado):
# - Fonte: USDA/FAS PSD API (https://api.fas.usda.gov/api/psd/...)
# - Commodity: Oilseed, Soybean (PSD code 2222000)
# - Série (gráfico): STU World anual
# - Tabela (decomp): Top 6 países por peso de produção 5y (w_prod_5y) e ΔYoY
# - STU = ending_stocks / (domestic_consumption + exports)
# - Revisão: reprocessar os últimos 3 marketYears disponíveis (modo incremental)
# - Bootstrap: se o JSON não existe, busca histórico de 1960 ate o ano atual
# - Se API falhar: workflow deve falhar (vermelho)
# - Se não houver dado novo: workflow verde e "No changes to commit"
#
# Secrets:
# - FAS_PSD_API_KEY (GitHub Actions)
# =============================================================================

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

# -------------------------
# CONFIG
# -------------------------

# PSD commodityCode para "Oilseed, Soybean".
SOYBEAN_COMMODITY_CODE = os.getenv("PSD_SOYBEAN_CODE", "2222000")

# Attribute IDs conforme padrao USDA PSD
ATTR_PRODUCTION = 28
ATTR_EXPORTS = 88
ATTR_DOM_CONS = 125
ATTR_ENDING_STOCKS = 176

TOP_N = 6  # Top 6 países (contrato fechado)

OUT_PATH = os.path.join("data", "soja", "series", "fundamental_stu.json")

API_BASE = "https://api.fas.usda.gov/api/psd"
API_KEY = os.getenv("FAS_PSD_API_KEY", "").strip()
HEADERS = {"X-Api-Key": API_KEY} if API_KEY else {}

# Regras de requisição (respeitar 1 req/s)
REQ_SLEEP_S = 1.05
TIMEOUT_S = 45
MAX_RETRIES = 3

# Bootstrap: ano inicial do histórico (PSD tem dados desde 1960)
BOOTSTRAP_START_YEAR = 1960


# -------------------------
# HELPERS
# -------------------------

def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def die(msg: str) -> None:
    raise RuntimeError(msg)

def api_get(url: str) -> Any:
    if not API_KEY:
        die("Secret FAS_PSD_API_KEY não encontrado no ambiente. Configure no GitHub Secrets.")

    last_status: Optional[int] = None
    delay = 1.0

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                headers=HEADERS,
                params={"api_key": API_KEY},
                timeout=TIMEOUT_S,
            )
            last_status = resp.status_code
        except Exception as e:
            if attempt == MAX_RETRIES:
                die(f"[api_get] Erro de conexão após {MAX_RETRIES} tentativas: {repr(e)}")
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception as e:
                die(f"[api_get] Falha ao decodificar JSON: {repr(e)}")

        # 404 em year especifico do PSD costuma significar "ano sem dado" - retorna None
        if resp.status_code == 404:
            return None

        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt == MAX_RETRIES:
                die(f"[api_get] {url} -> status {resp.status_code} após {MAX_RETRIES} tentativas.")
            time.sleep(delay)
            delay *= 2
            continue

        die(f"[api_get] {url} -> status {resp.status_code}. Body: {resp.text[:300]}")

    die(f"[api_get] Falha inesperada. Último status: {last_status}")
    return None


def load_existing_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json_atomic(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)

def parse_years_from_release_dates(data: Any) -> List[int]:
    if not isinstance(data, list) or not data:
        return []
    years = []
    for row in data:
        try:
            y = int(row.get("marketYear"))
            years.append(y)
        except Exception:
            continue
    return sorted(set(years))

def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

def pick_values(records: List[Dict[str, Any]], attr_id: int) -> Optional[float]:
    val: Optional[float] = None
    for r in records:
        try:
            if int(r.get("attributeId")) == int(attr_id):
                val = safe_float(r.get("value"))
        except Exception:
            continue
    return val

def resolve_country_name(rec: Dict[str, Any]) -> str:
    for k in ("countryName", "country", "country_name"):
        v = rec.get(k)
        if v:
            return str(v)
    cc = rec.get("countryCode")
    return str(cc) if cc else "UNKNOWN"


# -------------------------
# PSD fetchers
# -------------------------

def get_available_market_years(commodity: str) -> List[int]:
    """Retorna apenas os marketYears que tiveram release - tipicamente os mais recentes."""
    url = f"{API_BASE}/commodity/{commodity}/dataReleaseDates"
    data = api_get(url)
    time.sleep(REQ_SLEEP_S)
    years = parse_years_from_release_dates(data)
    if not years:
        die("[dataReleaseDates] Não consegui obter marketYears disponíveis (resposta vazia/inesperada).")
    return years

def fetch_world_year(commodity: str, year: int) -> Optional[List[Dict[str, Any]]]:
    """Retorna None se o ano nao tiver dado (404)."""
    url = f"{API_BASE}/commodity/{commodity}/world/year/{year}"
    data = api_get(url)
    time.sleep(REQ_SLEEP_S)
    if data is None:
        return None
    if not isinstance(data, list):
        die(f"[world/year] Resposta inesperada (não-list) para year={year}.")
    return data

def fetch_all_countries_year(commodity: str, year: int) -> List[Dict[str, Any]]:
    url = f"{API_BASE}/commodity/{commodity}/country/all/year/{year}"
    data = api_get(url)
    time.sleep(REQ_SLEEP_S)
    if data is None:
        return []
    if not isinstance(data, list):
        die(f"[country/all/year] Resposta inesperada (não-list) para year={year}.")
    return data


# -------------------------
# Builders
# -------------------------

def compute_stu_for_year(world_records: List[Dict[str, Any]]) -> Optional[float]:
    ending = pick_values(world_records, ATTR_ENDING_STOCKS)
    dom = pick_values(world_records, ATTR_DOM_CONS)
    exp = pick_values(world_records, ATTR_EXPORTS)

    if ending is None or dom is None or exp is None:
        return None

    denom = (dom + exp)
    if denom == 0:
        return None

    return float(ending) / float(denom)

def series_update(existing_points: List[Dict[str, Any]], refresh_map: Dict[int, Optional[float]]) -> List[Dict[str, Any]]:
    refresh_dates = {f"{y}-01-01" for y in refresh_map.keys()}

    kept = []
    for p in existing_points:
        d = str(p.get("date", ""))
        if d in refresh_dates:
            continue
        kept.append({"date": d, "close": p.get("close")})

    for y, stu in refresh_map.items():
        kept.append({"date": f"{y}-01-01", "close": stu})

    kept.sort(key=lambda x: str(x.get("date", "")))
    return kept

def build_decomp(
    prod_by_year_country: Dict[int, Dict[str, Tuple[str, Optional[float]]]],
    prod_world_by_year: Dict[int, Optional[float]],
    latest_year: int,
    window_years: List[int],
    top_n: int,
) -> Tuple[Dict[str, Any], float]:
    world_vals = [prod_world_by_year.get(y) for y in window_years]
    world_vals = [v for v in world_vals if v is not None]
    if not world_vals:
        die("[decomp] Não consegui calcular mean_prod_5y_world (produção World ausente na janela 5y).")
    mean_world_5y = sum(world_vals) / len(world_vals)

    countries = set()
    for y in window_years:
        countries |= set(prod_by_year_country.get(y, {}).keys())

    rows = []
    for cc in countries:
        if str(cc) == "00":
            continue

        vals = []
        cname = None
        for y in window_years:
            rec = prod_by_year_country.get(y, {}).get(cc)
            if not rec:
                continue
            cname = rec[0]
            v = rec[1]
            if v is not None:
                vals.append(v)

        if not vals:
            continue

        mean_country_5y = sum(vals) / len(vals)
        w_prod_5y = (mean_country_5y / mean_world_5y) if mean_world_5y != 0 else 0.0

        prod_y = prod_by_year_country.get(latest_year, {}).get(cc, (cname or str(cc), None))[1]
        prod_prev = prod_by_year_country.get(latest_year - 1, {}).get(cc, (cname or str(cc), None))[1]
        if prod_y is None or prod_prev is None:
            d_yoy = None
            contrib = None
        else:
            d_yoy = float(prod_y) - float(prod_prev)
            contrib = float(w_prod_5y) * float(d_yoy)

        rows.append({
            "country": cname or str(cc),
            "country_resolved": cname or str(cc),
            "w_prod_5y": float(w_prod_5y),
            "prod": float(prod_y) if prod_y is not None else None,
            "prod_prev": float(prod_prev) if prod_prev is not None else None,
            "d_prod_yoy": float(d_yoy) if d_yoy is not None else None,
            "contrib": float(contrib) if contrib is not None else None,
        })

    rows.sort(key=lambda r: (r["w_prod_5y"] if r["w_prod_5y"] is not None else -1e18), reverse=True)
    rows = rows[:top_n]

    return {"year": latest_year, "rows": rows}, mean_world_5y


def main() -> None:
    existing = load_existing_json(OUT_PATH)
    existing_points = (existing or {}).get("data", []) if existing else []
    existing_meta = (existing or {}).get("meta", {}) if existing else {}

    is_bootstrap = len(existing_points) == 0

    years = get_available_market_years(SOYBEAN_COMMODITY_CODE)
    latest_year = max(years)

    # ----- Modo BOOTSTRAP (primeira execucao): buscar histórico completo -----
    if is_bootstrap:
        print(f"BOOTSTRAP: nenhum histórico encontrado. Buscando series completa de {BOOTSTRAP_START_YEAR} a {latest_year}.")
        full_refresh_map: Dict[int, Optional[float]] = {}
        for y in range(BOOTSTRAP_START_YEAR, latest_year + 1):
            recs = fetch_world_year(SOYBEAN_COMMODITY_CODE, y)
            if recs is None or len(recs) == 0:
                continue  # ano sem dado, pula
            stu = compute_stu_for_year(recs)
            if stu is not None:
                full_refresh_map[y] = stu

        refresh_map = full_refresh_map
        print(f"BOOTSTRAP: coletados {len(refresh_map)} anos de STU.")
    else:
        # ----- Modo INCREMENTAL: revisar últimos 3 marketYears -----
        refresh_years = [y for y in [latest_year - 2, latest_year - 1, latest_year] if y in years]
        if len(refresh_years) < 1:
            die("Não consegui determinar refresh_years (lista vazia).")

        refresh_map: Dict[int, Optional[float]] = {}
        for y in refresh_years:
            recs = fetch_world_year(SOYBEAN_COMMODITY_CODE, y)
            if recs is None:
                continue
            refresh_map[y] = compute_stu_for_year(recs)

    updated_points = series_update(existing_points, refresh_map)

    if len(updated_points) < 5:
        die(f"Apos atualizacao, serie ainda tem menos de 5 pontos (n={len(updated_points)}). Bootstrap pode ter falhado.")

    # ----- Decomposicao (sempre busca janela 5y para o latest_year) -----
    window_years = [latest_year - 4, latest_year - 3, latest_year - 2, latest_year - 1, latest_year]

    prod_world_by_year: Dict[int, Optional[float]] = {}
    for y in window_years:
        recs = fetch_world_year(SOYBEAN_COMMODITY_CODE, y)
        if recs is None:
            prod_world_by_year[y] = None
        else:
            prod_world_by_year[y] = pick_values(recs, ATTR_PRODUCTION)

    prod_by_year_country: Dict[int, Dict[str, Tuple[str, Optional[float]]]] = {}

    for y in window_years:
        data_all = fetch_all_countries_year(SOYBEAN_COMMODITY_CODE, y)
        by_cc: Dict[str, Tuple[str, Optional[float]]] = {}
        for r in data_all:
            try:
                if int(r.get("attributeId")) != int(ATTR_PRODUCTION):
                    continue
            except Exception:
                continue

            cc = str(r.get("countryCode", "")).strip()
            if not cc:
                continue
            cname = resolve_country_name(r)

            val = safe_float(r.get("value"))
            by_cc[cc] = (cname, val)

        prod_by_year_country[y] = by_cc

    decomp, mean_world_5y = build_decomp(
        prod_by_year_country=prod_by_year_country,
        prod_world_by_year=prod_world_by_year,
        latest_year=latest_year,
        window_years=window_years,
        top_n=TOP_N,
    )

    meta = {
        "id": "fundamental_stu",
        "title": "FUNDAMENTAL S&D (STU Global)",
        "frequency": "Anual",
        "updated_at": now_utc_str(),
        "world_name": "World",
        "weight_method": "w_prod_5y = mean(prod_country,last5y)/mean(prod_world,last5y)",
        "delta_method": "d_prod_yoy = prod(t) - prod(t-1)",
        "contrib_method": "contrib = w_prod_5y * d_prod_yoy",
        "window_years": window_years,
        "latest_year": latest_year,
    }

    for k, v in existing_meta.items():
        if k not in meta:
            meta[k] = v

    out = {
        "meta": meta,
        "data": updated_points,
        "decomp": decomp,
    }

    write_json_atomic(OUT_PATH, out)

    print("OK: data/soja/series/fundamental_stu.json atualizado.")
    print(f"Commodity: {SOYBEAN_COMMODITY_CODE} | latest_year={latest_year} | modo={'BOOTSTRAP' if is_bootstrap else 'INCREMENTAL'}")
    print(f"Total de pontos: {len(updated_points)}")
    last_pt = updated_points[-1] if updated_points else {}
    print(f"Último ponto: {last_pt}")
    print(f"Decomp rows: {len(decomp.get('rows', []))} | mean_world_5y={mean_world_5y:.4f}")


if __name__ == "__main__":
    main()
