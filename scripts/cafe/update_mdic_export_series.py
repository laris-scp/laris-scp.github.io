import json
from pathlib import Path
from datetime import datetime

import requests
import urllib3

# =========================
# CONFIGURAÇÕES GERAIS
# =========================

# evita warnings de SSL no runner do GitHub
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://api-comexstat.mdic.gov.br"
HISTORICAL_ENDPOINT = f"{BASE_URL}/historical-data"

OUT_PATH = Path("data/cafe/series/mdic_export.json")

# Produto: café verde (decisão final)
NCM_CAFE_VERDE = [
    "09011100",  # Café não torrado, não descafeinado
    "09011200",  # Café não torrado, descafeinado
]

COUNTRY_BR = "076"  # Brasil (padrão COMEXSTAT)

START_YM = "1996-01"  # histórico completo
END_YM = datetime.today().strftime("%Y-%m")


# =========================
# FUNÇÕES AUXILIARES
# =========================

def _request_json(method: str, url: str, **kwargs) -> dict:
    """
    Wrapper simples para requests com JSON.
    """
    r = requests.request(
        method,
        url,
        verify=False,  # necessário no GitHub Actions
        headers={"Content-Type": "application/json"},
        **kwargs,
    )
    r.raise_for_status()
    return r.json()


def _post_historical(body: dict) -> list:
    """
    POST no endpoint /historical-data
    """
    resp = _request_json(
        "POST",
        HISTORICAL_ENDPOINT,
        params={"language": "pt"},
        json=body,
        timeout=120,
    )

    if not resp.get("success", False):
        raise RuntimeError(f"API retornou erro: {resp}")

    return resp.get("data", [])


# =========================
# MAIN
# =========================

def main():
    print(">> MDIC Export | Café Verde | Iniciando coleta")

    body = {
        "flow": "export",
        "monthDetail": True,
        "period": {
            "from": START_YM,
            "to": END_YM,
        },
        "filters": [
            {
                "filter": "country",
                "values": [COUNTRY_BR],
            },
            {
                "filter": "ncm",
                "values": NCM_CAFE_VERDE,
            },
        ],
        "details": [],
        "metrics": ["metricKG"],
    }

    rows = _post_historical(body)

    if not rows:
        raise RuntimeError("Nenhum dado retornado pela API do MDIC")

    # =========================
    # AGREGAÇÃO MENSAL
    # =========================
    series = {}

    for r in rows:
        year = r["year"]
        month = r["monthNumber"]
        kg = float(r.get("metricKG", 0))

        ym = f"{year}-{month:02d}"

        if ym not in series:
            series[ym] = 0.0

        series[ym] += kg

    # ordenar cronologicamente
    out = []
    for ym in sorted(series.keys()):
        kg = series[ym]
        sacks_60kg = kg / 60.0

        out.append({
            "date": f"{ym}-01",
            "kg": round(kg, 2),
            "sacks_60kg": round(sacks_60kg, 2),
        })

    # =========================
    # SALVAR JSON
    # =========================
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "source": "MDIC / COMEXSTAT",
        "commodity": "coffee",
        "product": "green_coffee",
        "ncm": NCM_CAFE_VERDE,
        "country": "Brazil",
        "unit": "kg",
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "series": out,
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f">> Arquivo gerado com sucesso: {OUT_PATH}")
    print(f">> Registros mensais: {len(out)}")


if __name__ == "__main__":
    main()
