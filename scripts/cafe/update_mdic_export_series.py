import json
import calendar
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd
import certifi
from requests.exceptions import SSLError
import urllib3
# evita log poluído quando cair no verify=False (opcional)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


OUT_PATH = Path("data/cafe/series/mdic_export.json")

BASE_URL_HTTPS = "https://api-comexstat.mdic.gov.br"
BASE_URL_HTTP  = "http://api-comexstat.mdic.gov.br"

BASE_URL = BASE_URL_HTTPS  # usamos HTTPS como padrão

HISTORICAL_ENDPOINT = f"{BASE_URL}/historical-data"
UPDATED_ENDPOINT = f"{BASE_URL}/general/dates/updated"
FILTER_VALUES_ENDPOINT = f"{BASE_URL}/historical-data/filters"

# Config
FLOW = "export"
MONTH_DETAIL = True

# CUCI (071a + 071c)
CUCI_CODES = ["071a", "071c"]

# Métrica: KG líquido
METRICS = ["metricKG"]

KG_PER_BAG = 60.0

# Período (ComexStat geral tem dados a partir de 1997)
PERIOD_FROM = "1997-01"

def _request_json(method: str, url: str, *, params=None, json_body=None, timeout=60):
    """
    Faz request e retorna .json().
    Estratégia:
      1) verify=True (padrão)
      2) verify=certifi.where()
      3) se ainda falhar por SSLError: verify=False (contorno)
    """
    # 1) padrão
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

    # 2) forçar bundle do certifi
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

    # 3) contorno final
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

def _to_eom(year: int, month: int) -> str:
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def _safe_get_updated_to() -> str:
    """
    Retorna 'YYYY-MM' do último mês completo disponível na API.
    Endpoint documentado: /general/dates/updated
    """
    data = _request_json("GET", UPDATED_ENDPOINT, timeout=60)

    # a doc mostra que vem assim:
    # { "data": { "updated": "2022-11-24", "year": "2022", "monthNumber": "12" }, "success": true, ... }
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        inner = data["data"]
        # preferir year + monthNumber quando existir
        y = inner.get("year")
        m = inner.get("monthNumber")
        if isinstance(y, str) and isinstance(m, str) and len(y) == 4:
            return f"{y}-{int(m):02d}"

        up = inner.get("updated")
        if isinstance(up, str) and len(up) >= 7:
            return up[:7]

    # fallback genérico
    for k in ["updated", "date", "last", "value"]:
        if isinstance(data, dict) and k in data and isinstance(data[k], str):
            v = data[k].strip()
            if len(v) >= 7:
                return v[:7]

    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str) and len(v) >= 7 and v[4] == "-":
                return v[:7]

    raise RuntimeError(f"Não consegui interpretar /general/dates/updated: {data}")

def _post_historical(body: dict) -> list:
    """
    POST /historical-data
    Retorna lista de linhas com dados mensais de exportação/importação.
    """
    data = _request_json(
        "POST",
        HISTORICAL_ENDPOINT,
        params={"language": "pt"},
        json_body=body,
        timeout=180,
    )

    # resposta padrão vem em data[]
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]

    raise RuntimeError(f"Formato inesperado em /historical-data: {data}")

def _try_cuci_filter_values_to_ids(codes: list[str]) -> list:
    """
    Busca /general/filters/cuci e tenta mapear os códigos (071a/071c) para IDs numéricos,
    caso a API exija IDs no body.
    """
    url = f"{FILTER_VALUES_ENDPOINT}/cuci"
    data = _request_json("GET", url, params={"language": "pt"}, timeout=120)

    values = None
    if isinstance(data, list):
        values = data
    elif isinstance(data, dict):
        for k in ["data", "result", "results", "items", "values"]:
            if isinstance(data.get(k), list):
                values = data[k]
                break

    if not values:
        raise RuntimeError(f"Não consegui ler valores de /general/filters/cuci: {data}")

    found_ids = []
    code_l = [c.lower().strip() for c in codes]

    for c in code_l:
        matched = None
        for it in values:
            if not isinstance(it, dict):
                continue
            txt = " ".join(
                [str(it.get(k, "")) for k in ["code", "co", "text", "label", "descricao", "description", "name"]]
            ).lower()
            if c in txt:
                matched = it
                break

        if not matched:
            raise RuntimeError(
                f"Não encontrei '{c}' em /general/filters/cuci. Exemplo item: {values[0] if values else None}"
            )

        extracted = False
        for kid in ["id", "value", "co", "code"]:
            if kid in matched:
                try:
                    vid = int(str(matched[kid]).strip())
                    found_ids.append(vid)
                    extracted = True
                    break
                except Exception:
                    continue

        if not extracted:
            raise RuntimeError(f"Encontrei '{c}', mas não consegui extrair um ID numérico do item: {matched}")

    return found_ids

def main():
    to_ym = _safe_get_updated_to()

    # 1) tenta consulta usando os próprios códigos como values
    body_codes = {
        "type": "export",
        "filters": {
            "cuci": cuci_ids
        },
        "from": from_ym,
        "to": to_ym,
        "frequency": "monthly"
    }


    try:
        rows = _post_historical(body_codes)
        used_filter_mode = "cuci_codes"
    except ValueError:
        # 2) fallback: mapear CUCI para IDs e tentar de novo
        ids = _try_cuci_filter_values_to_ids(CUCI_CODES)
        body_ids = {
            "flow": FLOW,
            "monthDetail": MONTH_DETAIL,
            "period": {"from": PERIOD_FROM, "to": to_ym},
            "filters": [{"filter": "cuci", "values": ids}],
            "details": [],
            "metrics": METRICS,
        }
        rows = _post_general(body_ids)
        used_filter_mode = "cuci_ids"

    if not rows:
        raise RuntimeError("Consulta retornou 0 linhas. Verifique filtros/período.")

    df = pd.DataFrame(rows)

    # Tentativa de identificar colunas de ano/mês e métrica KG
    # A API pode devolver algo como: year, month, metricKG
    # Ou: coAno, coMes, metricKG, etc.
    def pick_col(candidates):
        for c in candidates:
            if c in df.columns:
                return c
        return None

    col_year = pick_col(["year", "ano", "coAno", "CO_ANO", "co_ano"])
    col_month = pick_col(["month", "mes", "coMes", "CO_MES", "co_mes"])
    col_kg = pick_col(["metricKG", "kgLiq", "vlKgLiquido", "vl_kg_liquido", "vl_kg", "metric_kg"])

    if col_year is None or col_month is None or col_kg is None:
        raise RuntimeError(
            f"Não encontrei colunas esperadas no retorno do /general.\n"
            f"Colunas disponíveis: {list(df.columns)}\n"
            f"Exemplo de linha: {rows[0]}"
        )

    df[col_year] = pd.to_numeric(df[col_year], errors="coerce")
    df[col_month] = pd.to_numeric(df[col_month], errors="coerce")
    df[col_kg] = pd.to_numeric(df[col_kg], errors="coerce")

    df = df.dropna(subset=[col_year, col_month, col_kg]).copy()
    df[col_year] = df[col_year].astype(int)
    df[col_month] = df[col_month].astype(int)

    # agrega por ano/mes (seguro, caso a API venha com duplicatas)
    df = (
        df.groupby([col_year, col_month], as_index=False)[col_kg]
        .sum(min_count=1)
        .rename(columns={col_year: "year", col_month: "month", col_kg: "kg_total"})
    )

    # monta datas no fim do mês
    df["date"] = df.apply(lambda r: _to_eom(int(r["year"]), int(r["month"])), axis=1)

    # converte para sacas
    df["bags_60kg"] = df["kg_total"] / KG_PER_BAG

    df = df.sort_values(["year", "month"]).reset_index(drop=True)

    # prepara JSON no padrão do projeto (series[])
    out = {
        "id": "mdic_export",
        "name": "Exportação (MDIC)",
        "unit": "bags_60kg",
        "meta": {
            "source": "ComexStat (MDIC) API",
            "endpoint": "/general",
            "flow": FLOW,
            "metric": "metricKG",
            "cuci": CUCI_CODES,
            "filter_mode": used_filter_mode,
            "kg_per_bag": KG_PER_BAG,
            "period_from": PERIOD_FROM,
            "period_to": to_ym,
            "updated_at_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "series": [
            {
                "date": row["date"],
                # para o site, vamos usar close como a série principal (sacas)
                "close": round(float(row["bags_60kg"]), 6),
                # para tabela/auditoria, guardamos kg também
                "kg_total": round(float(row["kg_total"]), 3),
            }
            for _, row in df.iterrows()
        ],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    print("OK: série MDIC export gerada:", str(OUT_PATH))
    print("Pontos:", len(out["series"]), "| Última data:", out["series"][-1]["date"])
    print("Filtro usado:", used_filter_mode)


if __name__ == "__main__":
    main()
