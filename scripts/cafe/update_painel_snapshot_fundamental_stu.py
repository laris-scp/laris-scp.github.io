# scripts/cafe/update_painel_snapshot_fundamental_stu.py
# =============================================================================
# PAINEL SNAPSHOT — FUNDAMENTAL_STU
# - Lê: data/cafe/series/fundamental_stu.json
# - Atualiza: data/cafe/painel_snapshot.json (somente id="fundamental_stu")
# - Regras:
#   percentil = (hist < último).mean()
#   nível: thresholds 0.2/0.4/0.6/0.8 -> (-1, -0.5, 0, 0.5, 1)
#   tendência/momento: últimos 3 pontos
#   score = (nivel + tendencia + momento) * mult_bloco
#   bloco 2 => mult_bloco = -1
#   score_ponderado = score * peso (peso lido do snapshot existente)
# =============================================================================

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

EPS = 1e-12

SERIES_PATH = os.path.join("data", "cafe", "series", "fundamental_stu.json")
SNAPSHOT_PATH = os.path.join("data", "cafe", "painel_snapshot.json")

VAR_ID = "fundamental_stu"
VAR_NAME = "FUNDAMENTAL S&D (STU Global)"
FREQUENCIA = "Anual"

REGRA_DE_SINAL = (
    "Score baseado no STU World (nível/tendência/momento) com inversão do bloco 2 "
    "(STU baixo = bullish). Tabela mostra decomposição por países (peso produção 5y × Δprodução YoY)."
)
FONTE = "USDA/FAS PSD API — Coffee, Green (World STU + produção dos principais países)"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Arquivo não encontrado: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def nivel_from_percentil(p: float):
    if np.isnan(p):
        return ("NEUTRO", 0.0)
    if p < 0.20:
        return ("MUITO BAIXO", -1.0)
    if p < 0.40:
        return ("BAIXO", -0.5)
    if p < 0.60:
        return ("NEUTRO", 0.0)
    if p < 0.80:
        return ("ALTO", 0.5)
    return ("MUITO ALTO", 1.0)


def tendencia_momento(vals: List[float]):
    v1, v2, v3 = float(vals[-3]), float(vals[-2]), float(vals[-1])
    d1 = v2 - v1
    d2 = v3 - v2

    # tendência
    if (d1 > EPS) and (d2 > EPS):
        tendencia, val_tend = "ALTA", 1.0
    elif (d1 < -EPS) and (d2 < -EPS):
        tendencia, val_tend = "QUEDA", -1.0
    elif (abs(d1) <= EPS) and (abs(d2) <= EPS):
        tendencia, val_tend = "LATERAL", 0.0
    else:
        tendencia, val_tend = "INDEFINIDA", 0.0

    # momento
    momento, val_mom = "NEUTRO", 0.0
    if tendencia == "ALTA":
        if d2 > d1 + EPS:
            momento, val_mom = "ALTA ACELERANDO", 1.0
        elif d2 < d1 - EPS:
            momento, val_mom = "ALTA DESACELERANDO", 0.5
    elif tendencia == "QUEDA":
        if d2 < d1 - EPS:
            momento, val_mom = "QUEDA ACELERANDO", -1.0
        elif d2 > d1 + EPS:
            momento, val_mom = "QUEDA DESACELERANDO", -0.5

    return tendencia, val_tend, momento, val_mom


def main() -> None:
    series = load_json(SERIES_PATH)
    snap = load_json(SNAPSHOT_PATH)

    pts = series.get("data", [])
    if not isinstance(pts, list) or not pts:
        raise RuntimeError("fundamental_stu.json sem campo 'data' válido.")

    # ordena por date (string ISO)
    pts = sorted(pts, key=lambda x: str(x.get("date", "")))

    # extrair valores válidos
    vals = []
    for p in pts:
        v = p.get("close", None)
        try:
            if v is None:
                continue
            vals.append(float(v))
        except Exception:
            continue

    if len(vals) < 5:
        raise RuntimeError(f"Série curta demais para percentil/tendência/momento (len={len(vals)}).")

    ultimo_valor = float(vals[-1])

    arr = np.array(vals, dtype=float)
    percentil = float((arr < ultimo_valor).mean())

    nivel, valor_nivel = nivel_from_percentil(percentil)
    tendencia, valor_tendencia, momento, valor_momento = tendencia_momento(vals)

    # localizar item no snapshot
    items = snap.get("items", None)
    if not isinstance(items, list):
        raise RuntimeError("painel_snapshot.json esperado com chave 'items' (lista).")

    idx = None
    for i, it in enumerate(items):
        if str(it.get("id", "")).strip() == VAR_ID:
            idx = i
            break
    if idx is None:
        raise RuntimeError(f"Item id='{VAR_ID}' não encontrado em painel_snapshot.json.")

    item = items[idx]

    bloco = int(item.get("bloco", 1))
    mult_bloco = 1.0 if bloco == 1 else (-1.0 if bloco == 2 else 1.0)

    # peso continua vindo do snapshot (como você pediu)
    try:
        peso = float(item.get("peso", 1.0))
    except Exception:
        peso = 1.0

    score = (float(valor_nivel) + float(valor_tendencia) + float(valor_momento)) * float(mult_bloco)
    score_pond = float(score) * float(peso)

    # atualizar somente este item
    item.update({
        "id": VAR_ID,
        "bloco": bloco,
        "variavel": VAR_NAME,
        "ultimo_valor": ultimo_valor,
        "percentil": percentil,
        "nivel": nivel,
        "valor_nivel": float(valor_nivel),
        "tendencia": tendencia,
        "valor_tendencia": float(valor_tendencia),
        "momento": momento,
        "valor_momento": float(valor_momento),
        "score": float(score),
        "peso": float(peso),
        "score_ponderado": float(score_pond),
        "frequencia": FREQUENCIA,
        "ultima_atualizacao": now_str(),
        "regra_de_sinal": REGRA_DE_SINAL,
        "fonte": FONTE,
    })

    items[idx] = item
    snap["items"] = items

    write_json(SNAPSHOT_PATH, snap)

    print("OK: painel_snapshot.json atualizado para fundamental_stu.")
    print(f"Bloco={bloco} | mult_bloco={mult_bloco} | peso={peso}")
    print(f"Último={ultimo_valor} | percentil={percentil}")
    print(f"Nível={nivel}({valor_nivel}) | Tend={tendencia}({valor_tendencia}) | Mom={momento}({valor_momento})")
    print(f"Score={score} | Score ponderado={score_pond}")


if __name__ == "__main__":
    main()
