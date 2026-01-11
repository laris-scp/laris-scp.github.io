#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

def _to_float(x: Any, default: float | None = None) -> float | None:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _score_pond(row: Dict[str, Any]) -> float | None:
    sp = _to_float(row.get("score_ponderado"), None)
    if sp is not None:
        return sp
    score = _to_float(row.get("score"), None)
    peso  = _to_float(row.get("peso"), 1.0)
    if score is None or peso is None:
        return None
    return score * peso

def _aggregate(rows: List[Dict[str, Any]], bloco: int | None) -> Tuple[float | None, float, float]:
    soma_sp = 0.0
    soma_peso = 0.0

    for r in rows:
        if bloco is not None and r.get("bloco") != bloco:
            continue

        peso = _to_float(r.get("peso"), 1.0)
        sp   = _score_pond(r)

        if peso is None or sp is None:
            continue

        soma_sp += float(sp)
        soma_peso += float(peso)

    if soma_peso <= 0:
        return (None, soma_sp, soma_peso)

    return (soma_sp / soma_peso, soma_sp, soma_peso)

def main() -> None:
    if not SNAPSHOT_PATH.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {SNAPSHOT_PATH}")

    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("painel_snapshot.json esperado com chave 'rows' (lista).")

    t1, s1, p1 = _aggregate(rows, bloco=1)
    t2, s2, p2 = _aggregate(rows, bloco=2)
    tg, sg, pg = _aggregate(rows, bloco=None)

    def r2(x: float | None) -> float | None:
        return None if x is None else round(float(x), 2)

    snapshot["thermometers"] = {
        "bloco_1": r2(t1),
        "bloco_2": r2(t2),
        "geral":   r2(tg),
    }
    snapshot["updated_at"] = _now_str()

    SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print("OK: painel_snapshot.json termometros recalculados.")
    print(f"DEBUG bloco_1: soma_sp={s1:.4f} soma_peso={p1:.4f} t={t1}")
    print(f"DEBUG bloco_2: soma_sp={s2:.4f} soma_peso={p2:.4f} t={t2}")
    print(f"DEBUG geral  : soma_sp={sg:.4f} soma_peso={pg:.4f} t={tg}")

if __name__ == "__main__":
    main()
