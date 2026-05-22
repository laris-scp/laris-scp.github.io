import os
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

import requests
import urllib3

# =========================
# CONFIG
# =========================
OUT_PATH = Path("data/soja/series/esr.json")

BASE_URL = "https://api.fas.usda.gov/api/esr"

# Chave da API FAS do USDA — reaproveita o secret FAS_PSD_API_KEY do repositório.
# A API ESR e a API PSD compartilham a mesma chave FAS.
API_KEY = os.environ.get("FAS_PSD_API_KEY", "").strip()

HEADERS = {"X-Api-Key": API_KEY, "Accept": "application/json"}

# Soybeans no ESR
COMMODITY_CODE = 801

# Safra (marketing year) da soja EUA: 1 set -> 31 ago.
# O marketYear é nomeado pelo ano de TÉRMINO (safra set/2025-ago/2026 = MY 2026).
# Primeiro MY com histórico confiável no ESR.
FIRST_MARKET_YEAR = 2000

# Quantos marketYears recentes reprocessar no modo incremental
# (3 cobre: MY corrente + anterior + um de folga, p/ revisões)
INCREMENTAL_MY_BACK = 2

# Rate limit
WAIT_429_SECONDS = 12
MAX_TRIES_429 = 10
PAUSE_BETWEEN_CALLS = 0.6

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =========================
# HELPERS (date)
# =========================
def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_week(raw: str):
    """weekEndingDate -> 'YYYY-MM-DD'."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date().isoformat()
    except ValueError:
        pass
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _crop_year_of(market_year: int) -> tuple[str, str]:
    """Retorna (inicio, fim) da safra de um marketYear. MY 2026 = set/2025 a ago/2026."""
    return (f"{market_year - 1}-09-01", f"{market_year}-08-31")


def _week_index_in_crop(week_iso: str, market_year: int) -> int:
    """
    Índice da semana dentro da safra (1 = primeira semana de setembro).
    Usado para alinhar semanas equivalentes entre safras.
    """
    start, _ = _crop_year_of(market_year)
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d = datetime.strptime(week_iso, "%Y-%m-%d").date()
    return ((d - d0).days // 7) + 1


# =========================
# HELPERS (api)
# =========================
def _get_with_retry(url: str):
    for attempt in range(1, MAX_TRIES_429 + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=120, verify=False)
        except Exception as e:
            print(f"   Erro de conexão (tentativa {attempt}): {e}")
            time.sleep(WAIT_429_SECONDS)
            continue

        if r.status_code == 429:
            print(f"   HTTP 429 (rate limit). Tentativa {attempt}/{MAX_TRIES_429}. Aguardando {WAIT_429_SECONDS}s...")
            time.sleep(WAIT_429_SECONDS)
            continue

        if r.status_code == 404:
            return []  # sem dados para esse MY (normal)

        if r.status_code >= 400:
            print(f"   HTTP {r.status_code}: {r.text[:300]}")

        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    raise RuntimeError("Estourei o limite de tentativas por 429.")


def _fetch_market_year(market_year: int) -> list[dict]:
    """
    Busca todos os países (allCountries) de Soybeans para um marketYear.
    O endpoint allCountries já retorna apenas países individuais + UNKNOWN
    (NÃO inclui o agregado EUROPEAN, code 1), então somar tudo dá o WORLD
    sem risco de dupla contagem.
    """
    url = f"{BASE_URL}/exports/commodityCode/{COMMODITY_CODE}/allCountries/marketYear/{market_year}"
    rows = _get_with_retry(url)
    print(f">> ESR Soybeans | MY {market_year} | registros: {len(rows)}")
    return rows


def _aggregate_world(rows: list[dict], market_year: int) -> dict[str, dict]:
    """
    Agrega o WORLD por semana (soma de todos os countryCode).
    Retorna {week_iso: {campos somados}}.
    """
    by_week = defaultdict(lambda: {
        "weekly_exports": 0.0,
        "accumulated_exports": 0.0,
        "outstanding_sales": 0.0,
        "gross_new_sales": 0.0,
        "net_sales": 0.0,
        "total_commitment": 0.0,
    })

    for r in rows:
        wk = _parse_week(r.get("weekEndingDate"))
        if wk is None:
            continue
        agg = by_week[wk]
        agg["weekly_exports"] += float(r.get("weeklyExports") or 0)
        agg["accumulated_exports"] += float(r.get("accumulatedExports") or 0)
        agg["outstanding_sales"] += float(r.get("outstandingSales") or 0)
        agg["gross_new_sales"] += float(r.get("grossNewSales") or 0)
        agg["net_sales"] += float(r.get("currentMYNetSales") or 0)
        agg["total_commitment"] += float(r.get("currentMYTotalCommitment") or 0)

    out = {}
    for wk, agg in by_week.items():
        out[wk] = {
            "date": wk,
            "market_year": market_year,
            "week_index": _week_index_in_crop(wk, market_year),
            "weekly_exports": round(agg["weekly_exports"], 2),
            "accumulated_exports": round(agg["accumulated_exports"], 2),
            "outstanding_sales": round(agg["outstanding_sales"], 2),
            "gross_new_sales": round(agg["gross_new_sales"], 2),
            "net_sales": round(agg["net_sales"], 2),
            "total_commitment": round(agg["total_commitment"], 2),
        }
    return out


# =========================
# IO
# =========================
def _load_existing():
    if not OUT_PATH.exists():
        return None
    return json.loads(OUT_PATH.read_text(encoding="utf-8"))


def _series_to_map(series: list[dict]) -> dict[str, dict]:
    return {str(r.get("date", "")).strip(): r for r in series if r.get("date")}


def _map_to_series(m: dict[str, dict]) -> list[dict]:
    return [m[k] for k in sorted(m.keys())]


def _deep_equal(a, b) -> bool:
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


def _current_market_year() -> int:
    """MY corrente da soja: a safra começa em setembro."""
    now = datetime.now(timezone.utc)
    return now.year + 1 if now.month >= 9 else now.year


# =========================
# MAIN
# =========================
def main():
    print(">> ESR Export | Soybeans (USDA FAS) | Iniciando")

    if not API_KEY:
        raise RuntimeError("FAS_PSD_API_KEY não definido no ambiente (secret do GitHub).")

    existing = _load_existing()
    cur_my = _current_market_year()

    if existing is None:
        market_years = list(range(FIRST_MARKET_YEAR, cur_my + 1))
        mode = "FULL"
    else:
        market_years = list(range(cur_my - INCREMENTAL_MY_BACK, cur_my + 1))
        mode = "INCREMENTAL"

    print(f">> Modo: {mode} | MarketYears: {market_years[0]}..{market_years[-1]}")

    new_rows_map = {}
    for my in market_years:
        rows = _fetch_market_year(my)
        world = _aggregate_world(rows, my)
        new_rows_map.update(world)
        time.sleep(PAUSE_BETWEEN_CALLS)

    if not new_rows_map and existing is None:
        raise RuntimeError("Nenhum dado retornado da API ESR no modo FULL.")

    if existing is None:
        out = {
            "source": "USDA FAS - Export Sales Reporting (ESR)",
            "endpoint": "/api/esr/exports",
            "commodity": "Soybeans",
            "commodity_code": COMMODITY_CODE,
            "destination": "WORLD (soma de todos os destinos)",
            "metric_principal": "total_commitment",
            "frequency": "weekly",
            "crop_year": "set-ago",
            "updated_at": _now_utc_str(),
            "series": _map_to_series(new_rows_map),
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f">> OK (full). Gravado: {OUT_PATH} | {len(new_rows_map)} semanas")
        return

    # Merge incremental
    old_map = _series_to_map(existing.get("series", []))
    changed = False
    for d, row in new_rows_map.items():
        if d not in old_map or not _deep_equal(old_map[d], row):
            old_map[d] = row
            changed = True

    if not changed:
        print(">> Sem mudanças na janela incremental. Nada a gravar.")
        return

    existing["series"] = _map_to_series(old_map)
    existing["updated_at"] = _now_utc_str()
    existing["destination"] = "WORLD (soma de todos os destinos)"
    existing["metric_principal"] = "total_commitment"

    OUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f">> OK (incremental). Atualizado: {OUT_PATH} | total {len(old_map)} semanas")


if __name__ == "__main__":
    main()
