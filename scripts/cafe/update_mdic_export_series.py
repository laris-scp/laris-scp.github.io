from pathlib import Path
from datetime import datetime
import json

import requests
from requests.exceptions import SSLError
import certifi
import urllib3

# =========================
# CONFIG GERAL
# =========================
OUT_PATH = Path("data/cafe/series/mdic_export.json")

BASE_URL = "https://api-comexstat.mdic.gov.br"

HISTORICAL_ENDPOINT = f"{BASE_URL}/historical-data"
UPDATED_ENDPOINT = f"{BASE_URL}/general/dates/updated"

# evita warning quando cair em verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================
# DEFINIÇÕES DA VARIÁVEL
# =========================
# Café verde (Brasil)
NCM_CAFE_VERDE = ["090111", "090112"]

FLOW = "export"
METRIC = "metricKG"
MONTH_DETAIL = False

KG_PER_BAG = 60.0

# =========================
# HELPERS DE REQUEST
# =========================
def _request_json(method: str, url: str, *, params=None, json_body=None, timeout=120):
    """
    Faz request HTTP e retorna JSON.
    Estratégia:
      1) SSL padrão
      2) SSL com certifi
      3) fallback verify=False (contorno p/ problema de cadeia no MDIC)
    """
    # tentativa padrão
    try:
        r = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()
    except SSLError:
        pass

    # tentativa com certifi
    try:
        r = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            timeout=timeout,
            verify=certifi.where(),
        )
        r.raise_for_status()
        return r.json()
    except SSLError:
        pass

    # contorno final
    r = requests.request(
        method,
        url,
        params=params,
        json=json_body,
        timeout=timeout,
        verify=False,
    )
    r.raise_for_status()
    return r.json()

# =========================
# METADATA DE ATUALIZAÇÃO
# =========================
def _safe_get_updated_to() -> str:
    """
    Retorna YYYY-MM do último mês disponível segundo o ComexStat.
    """
    data = _request_json("GET", UPDATED_ENDPOINT, timeout=60)

    # formato padrão da API
    # { "data": { "year": "2025", "monthNumber": "12", ... }, ... }
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        inner = data["data"]
        y = inner.get("year")
        m = inner.get("monthNumber")
        if isinstance(y, str) and isinstance(m, str):
            return f"{y}-{int(m):02d}"

    raise RuntimeError(f"Não consegui interpretar updated endpoint: {data}")

# =========================
# CHAMADA HISTÓRICA
# =========================
def _post_historical(body: dict) -> list:
    """
    POST /historical-data
    Retorna lista de linhas mensais.
    """
    data = _request_json(
        "POST",
        HISTORICAL_ENDPOINT,
        params={"language": "pt"},
        json_body=body,
        timeout=180,
    )

    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]

    raise RuntimeError(f"Formato inesperado do /historical-data: {data}")

# =========================
# MAIN
# =========================
def main():
    # intervalo
    from_ym = "1997-01"
    to_ym = _safe_get_updated_to()

    body = {
        "flow": FLOW,
        "monthDetail": MONTH_DETAIL,
        "period": {
            "from": from_ym,
            "to": to_ym,
        },
        "filters": [
            {
                "filter": "ncm",
                "values": NCM_CAFE_VERDE,
            }
        ],
        "metrics": [METRIC],
    }

    rows = _post_historical(body)

    series = []
    for r in rows:
        try:
            year = int(r["year"])
            month = int(r["month"])
            kg = float(r[METRIC])
        except Exception:
            continue

        date = datetime(year, month, 1).strftime("%Y-%m-%d")

        series.append(
            {
                "date": date,
                "kg_total": round(kg, 2),
                "bags_60kg": round(kg / KG_PER_BAG, 2),
            }
        )

    series = sorted(series, key=lambda x: x["date"])

    payload = {
        "source": "MDIC / ComexStat",
        "variable": "Exportação de Café Verde – Brasil",
        "filters": {
            "ncm": NCM_CAFE_VERDE,
            "flow": FLOW,
            "metric": METRIC,
        },
        "unit": {
            "kg": "quilogramas",
            "bags_60kg": "sacas de 60kg",
        },
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "series": series,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"OK: série MDIC exportação café verde salva em {OUT_PATH}")

# =========================
if __name__ == "__main__":
    main()
