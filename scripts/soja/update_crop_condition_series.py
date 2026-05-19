# scripts/soja/update_crop_condition_series.py
# =============================================================================
# CROP CONDITION (Soja EUA - G+E) - Série semanal via USDA NASS Quick Stats
# Objetivo: gerar/atualizar data/soja/series/crop_condition.json (consumido pelo site)
#
# Regras (contrato fechado):
# - Fonte: USDA NASS Quick Stats API (https://quickstats.nass.usda.gov/api)
# - Filtros: source=SURVEY, sector=CROPS, agg_level=NATIONAL, state=US TOTAL,
#            freq=WEEKLY, short_desc IN (SOYBEANS - CONDITION ... GOOD/EXCELLENT)
# - Métrica: G+E = GOOD + EXCELLENT em pontos percentuais
# - Janela de publicação: jun a set (mas o script não trava por mês - confia na API)
# - Revisão: refaz os últimos 60 dias (margem de segurança para revisões do USDA)
# - Bootstrap: se o JSON não existe, busca de 2010 até hoje
# - Se API falhar: workflow falha (vermelho)
# - Se não houver dado novo: workflow verde e "No changes to commit"
#
# Secrets necessários:
# - NASS_API_KEY (GitHub Actions)
# =============================================================================

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, date, timedelta
from typing import Any, Dict, List, Optional

import requests

# -------------------------
# CONFIG
# -------------------------

BASE_URL = "https://quickstats.nass.usda.gov/api"
API_KEY = os.getenv("NASS_API_KEY", "").strip()

OUT_PATH = os.path.join("data", "soja", "series", "crop_condition.json")

SHORT_DESC_GOOD = "SOYBEANS - CONDITION, MEASURED IN PCT GOOD"
SHORT_DESC_EXCELLENT = "SOYBEANS - CONDITION, MEASURED IN PCT EXCELLENT"

YEAR_START_BOOTSTRAP = 2010
INCREMENTAL_LOOKBACK_DAYS = 60

REQ_SLEEP_S = 0.8
TIMEOUT_S = 90
MAX_RETRIES = 4


# -------------------------
# HELPERS
# -------------------------

def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def die(msg: str) -> None:
    raise RuntimeError(msg)


def qs_get(endpoint: str, params: dict) -> Any:
    if not API_KEY:
        die("Secret NASS_API_KEY não encontrado no ambiente. Configure no GitHub Secrets.")

    url = f"{BASE_URL}/{endpoint}/"
    p = dict(params)
    p["key"] = API_KEY

    last_err: Optional[Exception] = None
    last_status: Optional[int] = None
    delay = 1.0

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQ_SLEEP_S)
            r = requests.get(url, params=p, timeout=TIMEOUT_S)
            last_status = r.status_code

            if r.status_code == 200:
                return r.json()

            # NASS retorna 400 quando não há dado para o filtro (ex: fora de janela de safra)
            # Tratamos como "sem dado" e seguimos
            if r.status_code == 400:
                body = r.text or ""
                if "exceeds" in body.lower() or "no data" in body.lower() or "error" in body.lower():
                    # se for "exceeds 50000 records" deixa estourar pra investigar
                    if "50000" in body or "exceeds" in body.lower():
                        die(f"[qs_get] {endpoint} -> 400 com 'exceeds': {body[:300]}")
                    # caso contrário, considera "sem dado" para o filtro
                    return None

            if 500 <= r.status_code < 600 or r.status_code == 429:
                if attempt == MAX_RETRIES:
                    die(f"[qs_get] {endpoint} -> status {r.status_code} após {MAX_RETRIES} tentativas.")
                time.sleep(delay)
                delay *= 2
                continue

            die(f"[qs_get] {endpoint} -> status {r.status_code}. Body: {r.text[:300]}")

        except requests.RequestException as e:
            last_err = e
            if attempt == MAX_RETRIES:
                die(f"[qs_get] Erro de conexão após {MAX_RETRIES} tentativas: {repr(e)}")
            time.sleep(delay)
            delay *= 2
            continue

    die(f"[qs_get] Falha inesperada. Último status: {last_status} | Último erro: {last_err}")
    return None


def normalize_value(row: dict) -> Optional[float]:
    v = row.get("value", None)
    if v is None and "Value" in row:
        v = row.get("Value")
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.upper() in {"NA", "N/A", "(NA)", "(D)", "(Z)"}:
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
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


# -------------------------
# Fetchers
# -------------------------

def fetch_condition_rows_for_year(short_desc: str, year: int) -> List[Dict[str, Any]]:
    """Busca todas as observações WEEKLY/NATIONAL/US TOTAL para um short_desc + ano."""
    params = {
        "source_desc": "SURVEY",
        "sector_desc": "CROPS",
        "agg_level_desc": "NATIONAL",
        "state_name": "US TOTAL",
        "freq_desc": "WEEKLY",
        "statisticcat_desc__LIKE": "%CONDITION%",
        "short_desc": short_desc,
        "year": str(year),
        "format": "JSON",
    }

    # Volume esperado: ~25 obs/ano por short_desc - bem abaixo do limite de 50k
    resp = qs_get("api_GET", params)
    if resp is None:
        return []
    if not isinstance(resp, dict):
        die(f"[fetch_condition] Resposta inesperada para year={year}, short_desc={short_desc}.")

    data = resp.get("data", [])
    if not isinstance(data, list):
        return []
    return data


def fetch_condition_year_range(start_year: int, end_year: int) -> Dict[str, Dict[str, float]]:
    """
    Retorna dict[week_ending] = {"good": x, "excellent": y}
    Faz uma chamada por (short_desc, ano).
    """
    out: Dict[str, Dict[str, float]] = {}

    for short_desc, key_short in [
        (SHORT_DESC_GOOD, "good"),
        (SHORT_DESC_EXCELLENT, "excellent"),
    ]:
        for year in range(start_year, end_year + 1):
            rows = fetch_condition_rows_for_year(short_desc, year)
            for r in rows:
                week_ending = r.get("week_ending")
                if not week_ending:
                    continue
                val = normalize_value(r)
                if val is None:
                    continue
                bucket = out.setdefault(str(week_ending), {})
                bucket[key_short] = val

    return out


# -------------------------
# Builders
# -------------------------

def build_points(by_week: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
    """Converte dict[week_ending] -> lista ordenada de pontos {date, close, good, excellent}."""
    pts: List[Dict[str, Any]] = []
    for week_ending, vals in by_week.items():
        good = vals.get("good")
        excellent = vals.get("excellent")
        if good is None or excellent is None:
            # ponto incompleto - descarta para garantir que close seja sempre coerente
            continue
        close = float(good) + float(excellent)
        pts.append({
            "date": week_ending,
            "close": float(close),
            "good": float(good),
            "excellent": float(excellent),
        })
    pts.sort(key=lambda p: str(p["date"]))
    return pts


def merge_points(existing: List[Dict[str, Any]], new_pts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Faz merge: pontos com date em new_pts substituem os existentes (revisões),
    pontos não presentes em new_pts são mantidos.
    """
    by_date: Dict[str, Dict[str, Any]] = {}
    for p in existing:
        d = str(p.get("date", ""))
        if d:
            by_date[d] = p
    for p in new_pts:
        d = str(p.get("date", ""))
        if d:
            by_date[d] = p
    merged = list(by_date.values())
    merged.sort(key=lambda p: str(p["date"]))
    return merged


def determine_year_window(existing_points: List[Dict[str, Any]]) -> tuple[int, int, str]:
    """
    Retorna (start_year, end_year, mode).
    - Bootstrap: 2010 -> ano atual
    - Incremental: ano de (last_date - 60d) -> ano atual
    """
    today = date.today()
    end_year = today.year

    if not existing_points:
        return (YEAR_START_BOOTSTRAP, end_year, "BOOTSTRAP")

    last_date_str = str(existing_points[-1].get("date", ""))
    try:
        y, m, d = map(int, last_date_str.split("-"))
        last_d = date(y, m, d)
    except Exception:
        return (YEAR_START_BOOTSTRAP, end_year, "BOOTSTRAP")

    cutoff = last_d - timedelta(days=INCREMENTAL_LOOKBACK_DAYS)
    return (cutoff.year, end_year, "INCREMENTAL")


def main() -> None:
    existing = load_existing_json(OUT_PATH)
    existing_points: List[Dict[str, Any]] = (existing or {}).get("data", []) if existing else []
    existing_meta: Dict[str, Any] = (existing or {}).get("meta", {}) if existing else {}

    start_year, end_year, mode = determine_year_window(existing_points)

    print(f"Modo: {mode} | Buscando anos {start_year}..{end_year}")

    by_week = fetch_condition_year_range(start_year, end_year)
    new_pts = build_points(by_week)

    print(f"Pontos coletados na janela: {len(new_pts)}")

    if mode == "BOOTSTRAP":
        if not new_pts:
            die("BOOTSTRAP: nenhum ponto coletado. Verifique API key e filtros.")
        merged = new_pts
    else:
        merged = merge_points(existing_points, new_pts)

    if len(merged) < 5:
        die(f"Após atualização, série ainda tem menos de 5 pontos (n={len(merged)}).")

    meta = {
        "id": "crop_condition",
        "title": "CROP CONDITION (Soja EUA · G+E)",
        "frequency": "Semanal (jun-set)",
        "metric": "good + excellent (pct)",
        "source": "USDA NASS Quick Stats",
        "updated_at": now_utc_str(),
    }
    # preservar metadados extras que possam ter sido adicionados manualmente
    for k, v in existing_meta.items():
        if k not in meta:
            meta[k] = v

    out = {
        "meta": meta,
        "data": merged,
    }

    write_json_atomic(OUT_PATH, out)

    print(f"OK: {OUT_PATH} atualizado.")
    print(f"Total de pontos: {len(merged)}")
    if merged:
        last = merged[-1]
        print(f"Último ponto: date={last['date']} | G+E={last['close']:.1f} (G={last['good']:.1f} + E={last['excellent']:.1f})")


if __name__ == "__main__":
    main()
