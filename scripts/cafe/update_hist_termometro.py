# scripts/cafe/update_hist_termometro.py
# =============================================================================
# HIST TERMÔMETRO (Geral) — Append-only a partir do painel_snapshot.json
#
# Formato OFICIAL (compatível com o site):
# {
#   "commodity": "cafe",
#   "series": [ { "date":"YYYY-MM-DD", "value": <float> }, ... ],
#   "meta": {... opcional ...}
# }
#
# Regras:
# - Se inputs falharem: ERRO (workflow vermelho)
# - Se não houver dado novo (date já existe): OK (verde) e não altera arquivo
# - Append-only
# =============================================================================

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Tuple

SNAPSHOT_PATH = os.path.join("data", "cafe", "painel_snapshot.json")
HIST_PATH = os.path.join("data", "cafe", "hist_termometro.json")


def die(msg: str) -> None:
    raise RuntimeError(msg)


def load_json(path: str) -> Any:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def parse_snapshot(snapshot: Any) -> Tuple[str, float]:
    """
    Retorna (date_yyyy_mm_dd, termometro_geral)
    """
    if not isinstance(snapshot, dict):
        die("painel_snapshot.json em formato inesperado (esperado dict na raiz).")

    updated_at = snapshot.get("updated_at")
    thermos = snapshot.get("thermometros")

    if not updated_at or not isinstance(updated_at, str):
        die("painel_snapshot.json sem 'updated_at' (string).")
    if not isinstance(thermos, dict):
        die("painel_snapshot.json sem 'thermometros' (dict).")

    geral = thermos.get("geral")
    if geral is None:
        die("painel_snapshot.json sem 'thermometros.geral'.")

    date = updated_at.strip().split(" ")[0]  # YYYY-MM-DD
    if len(date) != 10:
        die(f"updated_at inválido para extrair data (esperado YYYY-MM-DD ...): '{updated_at}'")

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except Exception:
        die(f"Data inválida extraída de updated_at: '{date}'")

    try:
        value = float(geral)
    except Exception:
        die(f"thermometros.geral não é numérico: '{geral}'")

    return date, value


def normalize_existing_hist(hist: Any) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """
    Aceita:
    - Formato oficial: dict com 'commodity' + 'series'
    - Formato antigo gerado por engano: dict com 'meta' + 'data'
    - Formato legado: lista na raiz
    Retorna: (commodity, meta, series_points)
    """
    commodity = "cafe"
    meta: Dict[str, Any] = {}
    series: List[Dict[str, Any]] = []

    if hist is None:
        return commodity, meta, series

    if isinstance(hist, dict):
        if isinstance(hist.get("commodity"), str):
            commodity = hist["commodity"]

        if isinstance(hist.get("meta"), dict):
            meta = hist["meta"]

        # Formato oficial
        if isinstance(hist.get("series"), list):
            series = hist["series"]
            return commodity, meta, series

        # Formato errado (meta/data)
        if isinstance(hist.get("data"), list):
            # converter data -> series
            series = [{"date": p.get("date"), "value": p.get("value")} for p in hist["data"] if isinstance(p, dict)]
            return commodity, meta, series

        die("hist_termometro.json: não encontrei 'series' nem 'data' em formato válido.")

    if isinstance(hist, list):
        # legado: lista de pontos
        series = hist
        return commodity, meta, series

    die("hist_termometro.json em formato inesperado.")
    return commodity, meta, series


def clean_points(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in points:
        if not isinstance(p, dict):
            continue
        d = p.get("date")
        v = p.get("value")
        if not isinstance(d, str) or len(d) != 10:
            continue
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except Exception:
            continue
        try:
            fv = float(v)
        except Exception:
            continue
        out.append({"date": d, "value": fv})

    out.sort(key=lambda x: x["date"])
    return out


def main() -> None:
    snapshot = load_json(SNAPSHOT_PATH)
    if snapshot is None:
        die(f"Arquivo não encontrado: {SNAPSHOT_PATH}")

    date, value = parse_snapshot(snapshot)

    hist = load_json(HIST_PATH)
    commodity, meta, series_raw = normalize_existing_hist(hist)
    series = clean_points(series_raw)

    existing_dates = {p["date"] for p in series}
    if date in existing_dates:
        print(f"OK: hist_termometro.json já contém date={date}. Sem mudanças.")
        return

    series.append({"date": date, "value": value})
    series.sort(key=lambda x: x["date"])

    # Atualiza meta (mantendo o que já existe)
    meta_out = dict(meta) if isinstance(meta, dict) else {}
    meta_out.setdefault("title", "Histórico do Termômetro Geral")
    meta_out.setdefault("source", "painel_snapshot.json (thermometros.geral)")
    meta_out.setdefault("frequency", "Diária")
    meta_out["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # --- saída FINAL no formato que o site consome ---
    # commodity + series (não "data")
    out = {
        "commodity": commodity,
        "series": series,
        "meta": meta_out,
    }


    write_json(HIST_PATH, out)

    print("OK: hist_termometro.json atualizado (append-only) no formato oficial (series).")
    print(f"Adicionado: date={date} | value={value}")
    print(f"Total pontos: {len(series)}")


if __name__ == "__main__":
    main()
