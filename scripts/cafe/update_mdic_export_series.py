# ============================================================
# MDIC / COMEXSTAT — Exportação de Café Verde (Brasil)
# Endpoint: POST /general
# Produto: Café Verde (NCM 090111 + 090112)
# Frequência: Mensal
# Métrica: metricKG (quantidade)
# Saída: data/cafe/series/mdic_export.json
# ============================================================

import requests
import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import urllib3
from requests.exceptions import SSLError
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =========================
# CONFIGURAÇÕES
# =========================
BASE_URL_HTTPS = "https://api-comexstat.mdic.gov.br"
BASE_URL_HTTP  = "http://api-comexstat.mdic.gov.br"
ENDPOINT_PATH  = "/general"

OUT_PATH = Path("data/cafe/series/mdic_export.json")

NCM_CAFE_VERDE = {"090111", "090112"}

# Todas as UFs do Brasil (exportação brasileira)
UF_BRASIL = [
    11,12,13,14,15,16,17,
    21,22,23,24,25,26,27,28,29,
    31,32,33,35,
    41,42,43,
    50,51,52,53
]

START_PERIOD = "1996-01"
END_PERIOD = datetime.utcnow().strftime("%Y-%m")

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json"
}

# =========================
# FUNÇÕES AUXILIARES
# =========================
def _post_general(body: dict) -> list:
    # 1) tentativa padrão: HTTPS com verificação de certificado
    try:
        r = requests.post(
            BASE_URL_HTTPS + ENDPOINT_PATH,
            headers=HEADERS,
            json=body,
            timeout=120,
            verify=True,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except SSLError:
        pass  # cai para fallback abaixo

    # 2) fallback: HTTPS sem verificação (evita SSL no GitHub runner)
    try:
        r = requests.post(
            BASE_URL_HTTPS + ENDPOINT_PATH,
            headers=HEADERS,
            json=body,
            timeout=120,
            verify=False,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception:
        pass

    # 3) último fallback: HTTP (se o host aceitar)
    r = requests.post(
        BASE_URL_HTTP + ENDPOINT_PATH,
        headers=HEADERS,
        json=body,
        timeout=120,
    )
    r.raise_for_status()
    return r.json().get("data", [])


# =========================
# MAIN
# =========================
def main():
    print(">> MDIC Export | Café Verde | Iniciando coleta")

    body = {
        "flow": "export",
        "monthDetail": True,
        "period": {
            "from": START_PERIOD,
            "to": END_PERIOD
        },
        "filters": [
            {
                "filter": "state",
                "values": UF_BRASIL
            }
        ],
        "details": ["ncm"],
        "metrics": ["metricKG"]
    }

    rows = _post_general(body)

    if not rows:
        raise RuntimeError("MDIC retornou zero linhas.")

    # -------------------------
    # Agregação mensal (NCM 090111 + 090112)
    # -------------------------
    agg = defaultdict(float)

    for r in rows:
        ncm = str(r.get("ncm", "")).strip()
        if ncm not in NCM_CAFE_VERDE:
            continue

        year = r["year"]
        month = r["monthNumber"]
        kg = float(r.get("metricKG", 0) or 0)

        key = f"{year}-{month:02d}"
        agg[key] += kg

    # -------------------------
    # Construção da série final
    # -------------------------
    series = []
    for ym in sorted(agg.keys()):
        dt = datetime.strptime(ym + "-01", "%Y-%m-%d")
        kg = agg[ym]
        bags_60kg = kg / 60.0 if kg else 0.0

        series.append({
            "date": dt.strftime("%Y-%m-%d"),
            "kg": round(kg, 2),
            "bags_60kg": round(bags_60kg, 2)
        })

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source": "MDIC / COMEXSTAT",
                "product": "Café Verde",
                "ncm": sorted(NCM_CAFE_VERDE),
                "frequency": "monthly",
                "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "series": series
            },
            f,
            ensure_ascii=False,
            indent=2
        )

    print(f">> Série salva em {OUT_PATH} | {len(series)} pontos")


if __name__ == "__main__":
    main()
