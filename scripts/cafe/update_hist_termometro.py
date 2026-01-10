# scripts/cafe/update_hist_termometro.py
# =============================================================================
# HIST TERMÔMETRO (Geral) — Append-only a partir do painel_snapshot.json
#
# Objetivo:
# - Ler data/cafe/painel_snapshot.json
# - Extrair:
#     date  = YYYY-MM-DD (a partir de updated_at)
#     value = thermometros.geral
# - Atualizar data/cafe/hist_termometro.json (append-only, sem duplicar a mesma date)
#
# Regras:
# - Se a API/inputs falharem (snapshot inválido, chaves ausentes): ERRO (workflow vermelho)
# - Se não houver dado novo (date já existe): OK (verde) e não altera o arquivo
# - Não depende de Sheets/Drive
# =============================================================================

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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

    # date = apenas YYYY-MM-DD (primeira parte)
    date = updated_at.strip().split(" ")[0]
    if len(date) != 10:
        die(f"updated_at inválido para extrair data (esperado YYYY-MM-DD ...): '{updated_at}'")

    try:
        # valida a data
        datetime.strptime(date, "%Y-%m-%d")
    except Exception:
        die(f"Data inválida extraída de updated_at: '{date}'")

    try:
        value = float(geral)
    except Exception:
        die(f"thermometros.geral não é numérico: '{geral}'")

    return date, value


def normalize_hist(hist: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Suporta:
    - hist como dict com 'data': [...]
    - hist como lista na raiz: [...]
    Retorna (meta_dict, data_list)
    """
    meta: Dict[str, Any] = {}
    data: List[Dict[str, Any]] = []

    if hist is None:
        return meta, data

    if isinstance(hist, dict):
        meta = hist.get("meta", {}) if isinstance(hist.get("meta", {}), dict) else {}
        data_raw = hist.get("data", [])
        if not isinstance(data_raw, list):
            die("hist_termometro.json: chave 'data' existe mas não é lista.")
        data = data_raw
        return meta, data

    if isinstance(hist, list):
        # formato legado: lista na raiz
        return meta, hist

    die("hist_termometro.json em formato inesperado (nem dict nem list).")
    return meta, data  # pragma: no cover


def extract_points(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filtra e normaliza pontos válidos:
    esperado: {"date": "YYYY-MM-DD", "value": number}
    """
    out: List[Dict[str, Any]] = []
    for p in data:
        if not isinstance(p, dict):
            continue
        d = p.get("date")
        v = p.get("value")
        if not d or not isinstance(d, str):
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
    meta, data_raw = normalize_hist(hist)
    points = extract_points(data_raw)

    existing_dates = {p["date"] for p in points}
    if date in existing_dates:
        print(f"OK: hist_termometro.json já contém date={date}. Sem mudanças.")
        return

    points.append({"date": date, "value": value})
    points.sort(key=lambda x: x["date"])

    # Monta saída no formato oficial (dict com meta + data)
    out = {
        "meta": {
            "id": "hist_termometro",
            "title": "Histórico do Termômetro Geral",
            "source": "painel_snapshot.json (thermometros.geral)",
            "frequency": "Diária",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "data": points,
    }

    # preserva campos extras de meta existentes (se houver)
    if isinstance(meta, dict):
        for k, v in meta.items():
            if k not in out["meta"]:
                out["meta"][k] = v

    write_json(HIST_PATH, out)

    print("OK: hist_termometro.json atualizado (append-only).")
    print(f"Adicionado: date={date} | value={value}")
    print(f"Total pontos: {len(points)}")


if __name__ == "__main__":
    main()
