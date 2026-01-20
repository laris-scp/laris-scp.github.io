import json
import calendar
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd

OUT_PATH = Path("data/cafe/series/mdic_export.json")

BASE_URL = "https://api-comexstat.mdic.gov.br"
GENERAL_ENDPOINT = f"{BASE_URL}/general"
UPDATED_ENDPOINT = f"{BASE_URL}/general/dates/updated"
FILTER_VALUES_ENDPOINT = f"{BASE_URL}/general/filters"

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


def _to_eom(year: int, month: int) -> str:
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def _safe_get_updated_to() -> str:
    """
    Retorna 'YYYY-MM' do último mês completo disponível na API.
    O endpoint /general/dates/updated costuma retornar algo como { "updated": "2025-12" } ou semelhante.
    """
    r = requests.get(UPDATED_ENDPOINT, timeout=60)
    r.raise_for_status()
    data = r.json()

    # tenta chaves comuns
    for k in ["updated", "date", "last", "value"]:
        if isinstance(data, dict) and k in data and isinstance(data[k], str):
            v = data[k].strip()
            if len(v) >= 7:
                return v[:7]

    # fallback: tenta achar um "YYYY-MM" em qualquer valor string
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, str) and len(v) >= 7 and v[4] == "-":
                return v[:7]

    raise RuntimeError(f"Não consegui interpretar /general/dates/updated: {data}")


def _post_general(body: dict) -> list:
    """
    Faz POST /general e retorna lista de linhas (dicts).
    A API pode retornar lista direta ou envelope com chave.
    """
    r = requests.post(GENERAL_ENDPOINT, params={"language": "pt"}, json=body, timeout=120)
    if r.status_code == 400:
        # deixa o chamador tratar (fallback de filtro)
        raise ValueError(r.text)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, list):
        return data

    # envelopes comuns
    for k in ["data", "result", "results", "items"]:
        if isinstance(data, dict) and isinstance(data.get(k), list):
            return data[k]

    # se vier dict único, embrulha
    if isinstance(data, dict):
        return [data]

    raise RuntimeError(f"Formato inesperado no retorno do /general: {type(data)}")


def _try_cuci_filter_values_to_ids(codes: list[str]) -> list:
    """
    Busca /general/filters/cuci e tenta mapear os códigos (071a/071c) para IDs numéricos,
    caso a API exija IDs no body.
    """
    url = f"{FILTER_VALUES_ENDPOINT}/cuci"
    r = requests.get(url, params={"language": "pt"}, timeout=120)
    r.raise_for_status()
    data = r.json()

    # data pode ser lista ou envelope
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

    # Heurística: procurar o code em campos tipo "code", "co", "id", "text", "label", "descricao"
    found_ids = []
    code_l = [c.lower().strip() for c in codes]

    for c in code_l:
        matched = None
        for it in values:
            if not isinstance(it, dict):
                continue
            # campos comuns
            txt = " ".join([str(it.get(k, "")) for k in ["code", "co", "text", "label", "descricao", "description", "name"]]).lower()
            if c in txt:
                matched = it
                break

        if not matched:
            raise RuntimeError(f"Não encontrei '{c}' em /general/filters/cuci. Exemplo item: {values[0] if values else None}")

        # tenta extrair id numérico
        for kid in ["id", "value", "co", "code"]:
            if kid in matched:
                try:
                    vid = int(str(matched[kid]).strip())
                    found_ids.append(vid)
                    break
                except Exception:
                    continue

        if len(found_ids) < code_l.index(c) + 1:
            raise RuntimeError(f"Encontrei '{c}', mas não consegui extrair um ID numérico do item: {matched}")

    return found_ids


def main():
    to_ym = _safe_get_updated_to()

    # 1) tenta consulta usando os próprios códigos como values
    body_codes = {
        "flow": FLOW,
        "monthDetail": MONTH_DETAIL,
        "period": {"from": PERIOD_FROM, "to": to_ym},
        "filters": [{"filter": "cuci", "values": CUCI_CODES}],
        "details": [],
        "metrics": METRICS,
    }

    try:
        rows = _post_general(body_codes)
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
