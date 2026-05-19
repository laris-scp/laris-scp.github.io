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
OUT_PATH = Path("data/soja/series/mdic_export.json")

BASE_URL = "https://api-comexstat.mdic.gov.br"
ENDPOINT = f"{BASE_URL}/general"

HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

# NCM da soja em grão (exceto semeadura) - 99% do volume exportado
NCM_SOJA = ["12019000"]

# Estados do Brasil (código do ComexStat)
UF_BRASIL = [
    11, 12, 13, 14, 15, 16, 17,
    21, 22, 23, 24, 25, 26, 27, 28, 29,
    31, 32, 33, 35,
    41, 42, 43,
    50, 51, 52, 53
]

START_FULL = "1997-01"
ROLLING_MONTHS = 3

# Rate limit: a própria API sugere ~10s
WAIT_429_SECONDS = 12
MAX_TRIES_429 = 10

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================
# HELPERS (date)
# =========================
def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _ym_to_tuple(ym: str) -> tuple[int, int]:
    y, m = ym.split("-")
    return int(y), int(m)

def _tuple_to_ym(y: int, m: int) -> str:
    return f"{y:04d}-{m:02d}"

def _add_months(y: int, m: int, delta: int) -> tuple[int, int]:
    total = y * 12 + (m - 1) + delta
    ny = total // 12
    nm = (total % 12) + 1
    return ny, nm

def _month_range(start_ym: str, end_ym: str) -> list[tuple[int, int]]:
    sy, sm = _ym_to_tuple(start_ym)
    ey, em = _ym_to_tuple(end_ym)
    out = []
    y, m = sy, sm
    while (y < ey) or (y == ey and m <= em):
        out.append((y, m))
        y, m = _add_months(y, m, 1)
    return out

# =========================
# HELPERS (api)
# =========================
def _extract_list(payload: dict) -> list:
    data = payload.get("data", {})
    if isinstance(data, dict) and isinstance(data.get("list"), list):
        return data["list"]
    raise RuntimeError(f"Estrutura inesperada no payload: {str(payload)[:400]}")

def _post_with_retry(body: dict) -> dict:
    for attempt in range(1, MAX_TRIES_429 + 1):
        r = requests.post(
            ENDPOINT,
            headers=HEADERS,
            json=body,
            timeout=120,
            verify=False,  # necessário (SSL falha no Python)
        )

        if r.status_code == 429:
            print(f"HTTP 429 (rate limit). Tentativa {attempt}/{MAX_TRIES_429}. Aguardando {WAIT_429_SECONDS}s...")
            time.sleep(WAIT_429_SECONDS)
            continue

        if r.status_code >= 400:
            print("HTTP:", r.status_code)
            print("Resposta (inicio):", r.text[:600])

        r.raise_for_status()
        return r.json()

    raise RuntimeError("Estourei o limite de tentativas por 429. A API está limitando no momento.")

def _make_body(period_from_ym: str, period_to_ym: str) -> dict:
    return {
        "flow": "export",
        "monthDetail": True,
        "period": {"from": period_from_ym, "to": period_to_ym},
        "filters": [
            {"filter": "state", "values": UF_BRASIL},
            {"filter": "ncm", "values": NCM_SOJA},
        ],
        "details": ["ncm"],
        "metrics": ["metricKG"],
    }

# =========================
# IO
# =========================
def _load_existing() -> dict | None:
    if not OUT_PATH.exists():
        return None
    return json.loads(OUT_PATH.read_text(encoding="utf-8"))

def _series_to_map(series: list[dict]) -> dict[str, dict]:
    out = {}
    for r in series:
        d = str(r.get("date", "")).strip()
        if d:
            out[d] = r
    return out

def _map_to_series(m: dict[str, dict]) -> list[dict]:
    return [m[k] for k in sorted(m.keys())]

def _deep_equal(a, b) -> bool:
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)

# =========================
# CORE FETCH
# =========================
def _fetch_period(start_ym: str, end_ym: str) -> dict[str, float]:
    """
    Retorna dict {"YYYY-MM": kg_total} para os meses no intervalo.
    """
    print(f">> MDIC Export Soja | Coletando {start_ym} -> {end_ym}")
    months = _month_range(start_ym, end_ym)

    # Agrupa por blocos contínuos, NÃO deixando bloco cruzar ano
    # (evita comportamento inconsistente da API observado no projeto do café)
    blocks = []
    if months:
        by, bm = months[0]
        py, pm = months[0]

        for (y, m) in months[1:]:
            ny, nm = _add_months(py, pm, 1)

            is_next_month = (y == ny and m == nm)

            # Se não for mês seguinte OU mudou o ano, fecha bloco.
            if (not is_next_month) or (y != py):
                blocks.append((by, bm, py, pm))
                by, bm = y, m

            py, pm = y, m

        blocks.append((by, bm, py, pm))

    agg = defaultdict(float)

    for (y1, m1, y2, m2) in blocks:
        period_from = _tuple_to_ym(y1, m1)
        period_to = _tuple_to_ym(y2, m2)

        body = _make_body(period_from, period_to)
        payload = _post_with_retry(body)
        rows = _extract_list(payload)
        years_seen = sorted({(r.get("year"), r.get("monthNumber")) for r in rows if "year" in r and "monthNumber" in r})
        print(f">> MDIC Export Soja | Linhas recebidas: {len(rows)} | YM únicos (amostra): {years_seen[-6:]}")

        # agrega por YM usando coNcm (código)
        for r in rows:
            co = str(r.get("coNcm", "")).strip()
            if co not in set(NCM_SOJA):
                continue
            year = int(r["year"])
            month = int(r["monthNumber"])
            kg = float(r.get("metricKG", 0) or 0)
            ym = _tuple_to_ym(year, month)
            agg[ym] += kg

    return dict(agg)

# =========================
# MAIN
# =========================
def main():
    print(">> MDIC Export | Soja em Grão | Iniciando")

    existing = _load_existing()
    now = datetime.now(timezone.utc)
    end_ym = f"{now.year:04d}-{now.month:02d}"

    if existing is None:
        # FULL HISTORY (1x)
        start_ym = START_FULL
        mode = "FULL"
    else:
        # INCREMENTAL: últimos 3 meses (inclui o mês atual)
        y, m = _ym_to_tuple(end_ym)
        sy, sm = _add_months(y, m, -(ROLLING_MONTHS - 1))
        start_ym = _tuple_to_ym(sy, sm)
        mode = "INCREMENTAL"

    print(f">> Modo: {mode} | Janela: {start_ym} -> {end_ym}")

    fetched = _fetch_period(start_ym, end_ym)  # {"YYYY-MM": kg}

    # Monta linhas no padrão do site: data + kg + toneladas
    new_rows_map = {}
    for ym, kg in fetched.items():
        new_rows_map[f"{ym}-01"] = {
            "date": f"{ym}-01",
            "kg": round(float(kg), 2),
            "toneladas": round(float(kg) / 1000.0, 2),
        }

    if existing is None:
        out = {
            "source": "MDIC / COMEXSTAT",
            "endpoint": "/general",
            "flow": "export",
            "product": "Soja em Grão",
            "ncm": NCM_SOJA,
            "frequency": "monthly",
            "updated_at": _now_utc_str(),
            "series": _map_to_series(new_rows_map),
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f">> OK (full). Gravado: {OUT_PATH}")
        return

    # Merge inteligente
    old_series = existing.get("series", [])
    old_map = _series_to_map(old_series)

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

    OUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f">> OK (incremental). Atualizado: {OUT_PATH}")

if __name__ == "__main__":
    main()
