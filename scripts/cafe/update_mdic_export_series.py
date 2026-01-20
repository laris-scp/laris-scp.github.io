# ============================================================
# MDIC / COMEXSTAT — Exportação de Café Verde (Brasil)
# Endpoint: POST /general
# Produto: Café Verde (NCM 090111 + 090112)
# Frequência: Mensal
# Métrica: metricKG (quantidade)
# Saída: data/cafe/series/mdic_export.json
# ============================================================

import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import requests
from requests.exceptions import SSLError
import urllib3

# Evita warnings quando cair em verify=False (opcional, mas limpa logs)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================
# CONFIGURAÇÕES
# =========================
BASE_URL_HTTPS = "https://api-comexstat.mdic.gov.br"
BASE_URL_HTTP  = "http://api-comexstat.mdic.gov.br"
ENDPOINT_PATH  = "/general"

OUT_PATH = Path("data/cafe/series/mdic_export.json")

# Café verde (2 NCMs)
NCM_CAFE_VERDE = {"090111", "090112"}

# Todas as UFs do Brasil (exportação brasileira)
UF_BRASIL = [
    11, 12, 13, 14, 15, 16, 17,
    21, 22, 23, 24, 25, 26, 27, 28, 29,
    31, 32, 33, 35,
    41, 42, 43,
    50, 51, 52, 53
]

START_PERIOD = "1996-01"
END_PERIOD = datetime.utcnow().strftime("%Y-%m")

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# =========================
# REQUISIÇÃO ROBUSTA (SSL fallback)
# =========================
def _post_general(body: dict) -> list:
    """
    Tenta:
      1) HTTPS verify=True
      2) HTTPS verify=False (fallback para runner com CA quebrado)
      3) HTTP (último fallback)
    """
    # 1) HTTPS normal
    try:
        r = requests.post(
            BASE_URL_HTTPS + ENDPOINT_PATH,
            headers=HEADERS,
            json=body,
            timeout=120,
            verify=True,
        )
        r.raise_for_status()
        payload = r.json()
        return _normalize_rows(payload)
    except SSLError:
        pass

    # 2) HTTPS sem verificação
    try:
        r = requests.post(
            BASE_URL_HTTPS + ENDPOINT_PATH,
            headers=HEADERS,
            json=body,
            timeout=120,
            verify=False,
        )
        r.raise_for_status()
        payload = r.json()
        return _normalize_rows(payload)
    except Exception:
        pass

    # 3) HTTP
    r = requests.post(
        BASE_URL_HTTP + ENDPOINT_PATH,
        headers=HEADERS,
        json=body,
        timeout=120,
    )
    r.raise_for_status()
    payload = r.json()
    return _normalize_rows(payload)


def _normalize_rows(raw):
    """
    Aceita:
      - lista de dicts
      - lista de strings JSON
      - string JSON (que contém lista/dict)
      - dict (com ou sem "data")
    Retorna: list[dict]
    """
    if raw is None:
        return []

    # Se veio um dict, tenta extrair "data"
    if isinstance(raw, dict):
        raw = raw.get("data", raw)

    # Se veio string, tenta parsear JSON
    if isinstance(raw, str):
        s = raw.strip()
        try:
            raw = json.loads(s)
        except Exception:
            raise RuntimeError(f"Resposta inesperada (string não-JSON): {s[:300]}")

    # Se veio lista
    if isinstance(raw, list):
        if not raw:
            return []

        # Já está no formato certo
        if isinstance(raw[0], dict):
            return raw

        # Lista de strings JSON
        if isinstance(raw[0], str):
            out = []
            for item in raw:
                try:
                    obj = json.loads(item)
                    if isinstance(obj, dict):
                        out.append(obj)
                except Exception:
                    continue
            if out:
                return out
            raise RuntimeError(f"Lista de strings, mas não consegui parsear JSON. Exemplo: {raw[0][:200]}")

    raise RuntimeError(f"Formato inesperado de rows: {type(raw)} | exemplo: {str(raw)[:300]}")


# =========================
# MAIN
# =========================
def main():
    print(">> MDIC Export | Café Verde | Iniciando coleta")

    body = {
        "flow": "export",
        "monthDetail": True,
        "period": {"from": START_PERIOD, "to": END_PERIOD},
        "filters": [
            {"filter": "state", "values": UF_BRASIL},
        ],
        "details": ["ncm"],
        "metrics": ["metricKG"],
    }

    rows = _post_general(body)
    print(f">> Linhas recebidas: {len(rows)} | tipo primeiro item: {type(rows[0]).__name__ if rows else 'EMPTY'}")
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

        year = int(r["year"])
        month = int(r["monthNumber"])
        kg = float(r.get("metricKG", 0) or 0)

        ym = f"{year}-{month:02d}"
        agg[ym] += kg

    if not agg:
        raise RuntimeError("MDIC retornou dados, mas nenhum bateu nos NCMs 090111/090112.")

    # -------------------------
    # Série final
    # -------------------------
    series = []
    for ym in sorted(agg.keys()):
        dt = datetime.strptime(ym + "-01", "%Y-%m-%d")
        kg = agg[ym]
        bags_60kg = kg / 60.0 if kg else 0.0

        series.append({
            "date": dt.strftime("%Y-%m-%d"),
            "kg": round(kg, 2),
            "bags_60kg": round(bags_60kg, 2),
        })

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "MDIC / COMEXSTAT",
        "endpoint": "/general",
        "flow": "export",
        "product": "Café Verde",
        "ncm": sorted(NCM_CAFE_VERDE),
        "frequency": "monthly",
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "series": series,
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f">> Série salva em {OUT_PATH} | {len(series)} pontos")

if __name__ == "__main__":
    main()
