# scripts/cafe/update_hist_termometro.py
# =============================================================================
# HIST TERMÔMETRO (Geral) — Append-only a partir do painel_snapshot.json
#
# SAÍDA (formato que o site deve consumir):
# {
#   "commodity": "cafe",
#   "series": [ { "date":"YYYY-MM-DD", "value": <float> }, ... ],
#   "meta": {...}
# }
#
# Compatibilidade de entrada (hist_termometro.json existente):
# - { "commodity": "...", "series": [...] , "meta": {...} }  (oficial)
# - { "meta": {...}, "data": [...] }                         (legado/antigo)
# - [ { "date":..., "value":... }, ... ]                     (lista raiz)
#
# Regras:
# - Se snapshot inválido / chaves ausentes: ERRO (workflow vermelho)
# - Se não houver dado novo:
#     - se arquivo já estiver no formato oficial -> não muda nada
#     - se arquivo estiver no formato legado -> REGRAVA no formato oficial
# - Append-only por date
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
    """Retorna (date_yyyy_mm_dd, termometro_geral)."""
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


def clean_points(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normaliza e filtra pontos válidos: {'date':'YYYY-MM-DD','value':float}."""
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


def normalize_existing_hist(hist: Any) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]], bool]:
    """
    Retorna:
      (commodity, meta, series_points, is_official)

    is_official=True somente quando o JSON existente já está no formato:
      {"commodity": "...", "series": [...], "meta": {...}}
    """
    commodity = "cafe"
    meta: Dict[str, Any] = {}
    series: List[Dict[str, Any]] = []
    is_official = False

    if hist is None:
        return commodity, meta, series, is_official

    # Formato dict
    if isinstance(hist, dict):
        if isinstance(hist.get("commodity"), str):
            commodity = hist["commodity"]

        if isinstance(hist.get("meta"), dict):
            meta = hist["meta"]

        # Formato oficial
        if isinstance(hist.get("series"), list):
            series = hist["series"]
            is_official = True
            return commodity, meta, series, is_official

        # Formato legado (meta + data)
        if isinstance(hist.get("data"), list):
            series = hist["data"]
            is_official = False
            return commodity, meta, series, is_official

        die("hist_termometro.json: não encontrei 'series' nem 'data' em formato válido.")

    # Formato lista na raiz (legado)
    if isinstance(hist, list):
        series = hist
        is_official = False
        return commodity, meta, series, is_official

    die("hist_termometro.json em formato inesperado (nem dict nem list).")
    return commodity, meta, series, is_official


def main() -> None:
    snapshot = load_json(SNAPSHOT_PATH)
    if snapshot is None:
        die(f"Arquivo não encontrado: {SNAPSHOT_PATH}")

    date, value = parse_snapshot(snapshot)

    hist = load_json(HIST_PATH)
    commodity, meta_in, series_raw, is_official = normalize_existing_hist(hist)
    series = clean_points(series_raw)

    existing_dates = {p["date"] for p in series}

    # Se não há dado novo:
    # - se já é oficial -> não mexe
    # - se não é oficial -> regrava no oficial (migração)
    if date in existing_dates and is_official:
        print(f"OK: hist_termometro.json já contém date={date} e já está no formato oficial. Sem mudanças.")
        return

    if date in existing_dates and not is_official:
        print(f"OK: date={date} já existe, mas o arquivo NÃO está no formato oficial. Vou regravar no formato oficial.")

    # Se é data nova, append
    if date not in existing_dates:
        series.append({"date": date, "value": value})
        series.sort(key=lambda x: x["date"])
        print(f"Adicionado ponto novo: date={date} | value={value}")

    # Meta final (preserva o que existir e garante campos úteis)
    meta_out = dict(meta_in) if isinstance(meta_in, dict) else {}
    meta_out.setdefault("id", "hist_termometro")
    meta_out.setdefault("title", "Histórico do Termômetro Geral")
    meta_out.setdefault("source", "painel_snapshot.json (thermometros.geral)")
    meta_out.setdefault("frequency", "Diária")
    meta_out.setdefault("value_name", "TERMOMETRO GERAL")
    meta_out["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    out = {
        "commodity": commodity,
        "series": series,
        "meta": meta_out,
    }

    write_json(HIST_PATH, out)

    print("OK: hist_termometro.json gravado no formato oficial (commodity + series).")
    print(f"Total pontos: {len(series)}")


if __name__ == "__main__":
    main()
