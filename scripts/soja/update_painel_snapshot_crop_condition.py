# scripts/soja/update_painel_snapshot_crop_condition.py
# =============================================================================
# PAINEL SNAPSHOT - CROP CONDITION (SOJA)
# - Lê: data/soja/series/crop_condition.json
# - Atualiza: data/soja/painel_snapshot.json (somente id="crop_condition")
# - Regras:
#   Nível: faixas fixas sobre G+E atual
#   Tendência: ge_atual - média G+E de 5y para a mesma semana ISO
#   Momento: ge_atual - ge da semana anterior
#   score = (nivel + tendencia + momento) * mult_bloco
#   bloco 2 => mult_bloco = -1
#   score_ponderado = score * peso (peso lido do snapshot existente; default 3.0)
#
# Janela ativa (Opção A híbrida):
#   - Se última semana da série > 21 dias atrás: FORA DE JANELA
#     -> Nível mantém o último valor classificado
#     -> Tendência e Momento zeram (signal = 0, label = "FORA DE JANELA")
# =============================================================================

from __future__ import annotations

import json
import os
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

SERIES_PATH = os.path.join("data", "soja", "series", "crop_condition.json")
SNAPSHOT_PATH = os.path.join("data", "soja", "painel_snapshot.json")

VAR_ID = "crop_condition"
VAR_NAME = "CROP CONDITION (Soja EUA · G+E)"
FREQUENCIA = "Semanal"

DEFAULT_BLOCO = 2
DEFAULT_PESO = 3.0

OUT_OF_WINDOW_DAYS = 21
WINDOW_5Y_TOLERANCE_DAYS = 7   # margem para matching de "mesma semana" no histórico

REGRA_DE_SINAL = (
    "Crop Condition (Good + Excellent) mede a saúde da lavoura americana de soja. "
    "Lavoura ruim/deteriorando tende a ser positivo para o preço (menor oferta esperada); "
    "lavoura boa/melhorando tende a ser negativo. "
    "Nível: condição absoluta atual. Tendência: comparação com média histórica de 5 anos para a mesma semana. "
    "Momento: variação versus a semana anterior. Fora da janela de safra (out-mai), Tendência e Momento ficam neutros."
)
FONTE = "USDA NASS Quick Stats — Soybeans Condition (Good + Excellent)"


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
        # mantém formato compacto, igual aos outros snapshots
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


# -------------------------
# CLASSIFICADORES
# -------------------------

def nivel_from_ge(ge_atual: float) -> Tuple[str, float]:
    if ge_atual < 50:
        return ("MUITO BAIXO", -1.0)
    if ge_atual < 55:
        return ("BAIXO", -0.5)
    if ge_atual < 65:
        return ("NEUTRO", 0.0)
    if ge_atual < 70:
        return ("ALTO", 0.5)
    return ("MUITO ALTO", 1.0)


def tendencia_from_diff_5y(diff: float) -> Tuple[str, float]:
    if diff < -5:
        return ("QUEDA FORTE", -1.0)
    if diff < -2:
        return ("QUEDA", -0.5)
    if diff <= 2:
        return ("LATERAL", 0.0)
    if diff <= 5:
        return ("ALTA", 0.5)
    return ("ALTA FORTE", 1.0)


def momento_from_diff_wow(diff: float) -> Tuple[str, float]:
    if diff < -3:
        return ("QUEDA ACELERANDO", -1.0)
    if diff < -1:
        return ("QUEDA DESACELERANDO", -0.5)
    if diff <= 1:
        return ("NEUTRO", 0.0)
    if diff <= 3:
        return ("ALTA DESACELERANDO", 0.5)
    return ("ALTA ACELERANDO", 1.0)


# -------------------------
# HELPERS DE DATA
# -------------------------

def parse_iso_date(s: str) -> Optional[date]:
    try:
        y, m, d = map(int, str(s).split("-"))
        return date(y, m, d)
    except Exception:
        return None


def compute_media_5y_same_week(points: List[Dict[str, Any]], target_date: date) -> Optional[float]:
    """
    Para a data target_date, busca pontos dos últimos 5 anos (mesmo mês/dia ±7 dias)
    e retorna a média de G+E (close). Retorna None se houver menos de 2 anos com dado.
    """
    target_doy = target_date.timetuple().tm_yday
    target_year = target_date.year

    matched_values: List[float] = []
    for offset in range(1, 6):
        year_n = target_year - offset
        best: Optional[Tuple[int, float]] = None  # (distância em dias, valor)
        for p in points:
            d = parse_iso_date(p.get("date", ""))
            if d is None or d.year != year_n:
                continue
            doy_n = d.timetuple().tm_yday
            dist = abs(doy_n - target_doy)
            if dist <= WINDOW_5Y_TOLERANCE_DAYS:
                close = p.get("close")
                if close is None:
                    continue
                if best is None or dist < best[0]:
                    best = (dist, float(close))
        if best is not None:
            matched_values.append(best[1])

    if len(matched_values) < 2:
        return None
    return sum(matched_values) / len(matched_values)


def is_out_of_window(last_series_date: date, today: Optional[date] = None) -> bool:
    if today is None:
        today = date.today()
    return (today - last_series_date).days > OUT_OF_WINDOW_DAYS


# -------------------------
# MAIN
# -------------------------

def main() -> None:
    series = load_json(SERIES_PATH)
    snap = load_json(SNAPSHOT_PATH)

    pts = series.get("data", [])
    if not isinstance(pts, list) or not pts:
        raise RuntimeError("crop_condition.json sem campo 'data' válido.")

    pts_sorted = sorted(pts, key=lambda x: str(x.get("date", "")))

    last_point = pts_sorted[-1]
    last_date_str = str(last_point.get("date"))
    last_date = parse_iso_date(last_date_str)
    if last_date is None:
        raise RuntimeError(f"Data inválida no último ponto: {last_date_str}")

    ge_atual = float(last_point.get("close"))
    ge_anterior: Optional[float] = None
    if len(pts_sorted) >= 2:
        prev = pts_sorted[-2]
        # exigir que o ponto anterior seja da mesma safra (até 14 dias antes)
        prev_date = parse_iso_date(str(prev.get("date", "")))
        if prev_date is not None and 0 < (last_date - prev_date).days <= 14:
            ge_anterior = float(prev.get("close"))

    ge_5y = compute_media_5y_same_week(pts_sorted, last_date)

    # Decisão: dentro ou fora da janela
    out_of_window = is_out_of_window(last_date)

    # Nível: sempre classificado (mesmo fora da janela, congela no último valor)
    nivel, valor_nivel = nivel_from_ge(ge_atual)

    if out_of_window:
        tendencia, valor_tendencia = ("FORA DE JANELA", 0.0)
        momento, valor_momento = ("FORA DE JANELA", 0.0)
    else:
        # Tendência: precisa da média 5y; se não houver, marca LATERAL
        if ge_5y is None:
            tendencia, valor_tendencia = ("LATERAL", 0.0)
        else:
            tendencia, valor_tendencia = tendencia_from_diff_5y(ge_atual - ge_5y)

        # Momento: precisa da semana anterior; se não houver, marca NEUTRO
        if ge_anterior is None:
            momento, valor_momento = ("NEUTRO", 0.0)
        else:
            momento, valor_momento = momento_from_diff_wow(ge_atual - ge_anterior)

    # Localizar/criar a row no snapshot
    rows = snap.get("rows", None)
    if not isinstance(rows, list):
        raise RuntimeError("painel_snapshot.json esperado com chave 'rows' (lista).")

    idx = None
    for i, it in enumerate(rows):
        if str(it.get("id", "")).strip() == VAR_ID:
            idx = i
            break

    if idx is None:
        item: Dict[str, Any] = {
            "id": VAR_ID,
            "bloco": DEFAULT_BLOCO,
            "variavel": VAR_NAME,
            "peso": DEFAULT_PESO,
        }
        rows.append(item)
        idx = len(rows) - 1
    else:
        item = rows[idx]

    bloco = int(item.get("bloco", DEFAULT_BLOCO))
    mult_bloco = 1.0 if bloco == 1 else (-1.0 if bloco == 2 else 1.0)

    try:
        peso = float(item.get("peso", DEFAULT_PESO))
    except Exception:
        peso = DEFAULT_PESO

    score = (float(valor_nivel) + float(valor_tendencia) + float(valor_momento)) * float(mult_bloco)
    score_pond = float(score) * float(peso)

    item.update({
        "id": VAR_ID,
        "bloco": bloco,
        "variavel": VAR_NAME,
        "peso": float(peso),
        "ultimo_valor": float(ge_atual),
        "media_5y": float(ge_5y) if ge_5y is not None else None,
        "nivel": nivel,
        "valor_nivel": float(valor_nivel),
        "tendencia": tendencia,
        "valor_tendencia": float(valor_tendencia),
        "momento": momento,
        "valor_momento": float(valor_momento),
        "score": float(score),
        "score_ponderado": float(score_pond),
        "frequencia": FREQUENCIA,
        "ultima_atualizacao": last_date_str,
        "ultima_data_serie": last_date_str,
        "fora_de_janela": bool(out_of_window),
        "regra_de_sinal": REGRA_DE_SINAL,
        "fonte": FONTE,
    })

    rows[idx] = item
    snap["rows"] = rows
    snap["updated_at"] = now_str()

    write_json(SNAPSHOT_PATH, snap)

    print(f"OK: {SNAPSHOT_PATH} atualizado para crop_condition.")
    print(f"Bloco={bloco} | mult_bloco={mult_bloco} | peso={peso}")
    print(f"G+E atual={ge_atual:.1f} | 5y={ge_5y if ge_5y is None else f'{ge_5y:.1f}'} | anterior={ge_anterior}")
    print(f"Fora de janela? {out_of_window} (última data: {last_date_str})")
    print(f"Nível={nivel}({valor_nivel}) | Tend={tendencia}({valor_tendencia}) | Mom={momento}({valor_momento})")
    print(f"Score={score} | Score ponderado={score_pond}")


if __name__ == "__main__":
    main()
