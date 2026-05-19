import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

SERIES_PATH = Path("data/soja/series/crush_spread.json")
SNAPSHOT_PATH = Path("data/soja/painel_snapshot.json")

VAR_ID = "crush_spread"
VAR_NAME = "CRUSH SPREAD (CBOT)"
FREQUENCIA = "Diária"
BLOCO = 1  # preço - sem inversão de sinal (mult_bloco = +1)
FONTE = "CBOT via Yahoo Finance (ZS=F, ZM=F, ZL=F)"

SERIES_LAST_DATE_FIELD = "ultima_data_serie"
DEFAULT_PESO = 2.0

# Janela para percentil (nível)
PERCENTIL_WINDOW_DAYS = 252 * 10  # 10 anos úteis (~2520 pregões)

# Janelas das médias móveis (em dias úteis)
MM_SHORT = 20    # ~1 mês
MM_LONG = 60     # ~3 meses
MM_LAG = 20      # comparação MM20 hoje vs MM20 de 20d atrás

# Faixas de tendência
TREND_LOW = 0.90
TREND_HIGH = 1.10

# Faixas de momento
MOM_QACEL = 0.90
MOM_QDESA = 0.95
MOM_NEUT_HI = 1.05
MOM_ADESA = 1.10

REGRA_DE_SINAL = (
    "Crush spread é o lucro bruto do esmagador (margem de processamento da soja em óleo e farelo). "
    "Fórmula CME: (Farelo × 0.022) + (Óleo × 0.11) − Soja, em $/bushel. "
    "Crush alto/subindo indica esmagador com margem gorda, comprando mais soja, demanda forte — tende a fazer o preço subir. "
    "Crush baixo/caindo indica esmagador recuando, demanda fraca — tende a fazer o preço cair. "
    "Nível: percentil do valor atual no histórico de 10 anos. "
    "Tendência: relação entre média móvel de 20 dias e de 60 dias. "
    "Momento: aceleração da MM20 (hoje vs. 20 dias atrás)."
)


def _now_utc_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _percentil_in_window(s: pd.Series, window_n: int) -> float:
    """Percentil do último valor dentro da janela mais recente."""
    s = pd.to_numeric(s, errors="coerce").dropna()
    if len(s) == 0:
        return float("nan")
    last = float(s.iloc[-1])
    sample = s.iloc[-window_n:] if len(s) > window_n else s
    return float((sample < last).mean())


def _bucket_nivel_by_percentil(p: float):
    if np.isnan(p):
        return "NEUTRO", 0.0
    if p < 0.20:
        return "MUITO BAIXO", -1.0
    if p < 0.40:
        return "BAIXO", -0.5
    if p < 0.60:
        return "NEUTRO", 0.0
    if p < 0.80:
        return "ALTO", 0.5
    return "MUITO ALTO", 1.0


def _bucket_trend(ratio: float):
    if np.isnan(ratio):
        return "LATERAL", 0.0
    if ratio < TREND_LOW:
        return "QUEDA", -1.0
    if ratio <= TREND_HIGH:
        return "LATERAL", 0.0
    return "ALTA", 1.0


def _bucket_momento(ratio: float):
    if np.isnan(ratio):
        return "NEUTRO", 0.0
    if ratio < MOM_QACEL:
        return "QUEDA ACELERANDO", -1.0
    if ratio < MOM_QDESA:
        return "QUEDA DESACELERANDO", -0.5
    if ratio <= MOM_NEUT_HI:
        return "NEUTRO", 0.0
    if ratio <= MOM_ADESA:
        return "ALTA DESACELERANDO", 0.5
    return "ALTA ACELERANDO", 1.0


def _ensure_row(snapshot: dict) -> dict:
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        snapshot["rows"] = []
        rows = snapshot["rows"]

    for r in rows:
        if r.get("id") == VAR_ID:
            return r

    new_row = {
        "id": VAR_ID,
        "bloco": BLOCO,
        "variavel": VAR_NAME,
        "ultimo_valor": None,
        "percentil": None,
        "nivel": None,
        "valor_nivel": None,
        "tendencia": None,
        "valor_tendencia": None,
        "momento": None,
        "valor_momento": None,
        "score": None,
        "peso": DEFAULT_PESO,
        "score_ponderado": None,
        "frequencia": FREQUENCIA,
        "ultima_atualizacao": None,
        "regra_de_sinal": "",
        "fonte": FONTE,
        SERIES_LAST_DATE_FIELD: None,
    }
    rows.append(new_row)
    return new_row


def main():
    if not SERIES_PATH.exists():
        raise RuntimeError(f"Série não encontrada: {SERIES_PATH}")

    series = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    pts = series.get("data", [])
    if not isinstance(pts, list) or len(pts) == 0:
        raise RuntimeError("crush_spread.json sem campo 'data' válido.")

    df = pd.DataFrame(pts).copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

    if len(df) < MM_LONG + MM_LAG:
        raise RuntimeError(f"Série curta demais ({len(df)} pts). Mínimo: {MM_LONG + MM_LAG}.")

    last_date = df.iloc[-1]["date"]
    series_last_date = last_date.strftime("%Y-%m-%d")

    if not SNAPSHOT_PATH.exists():
        raise RuntimeError(f"Snapshot não encontrado: {SNAPSHOT_PATH}")

    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    row = _ensure_row(snapshot)

    # Early exit
    prev_last = row.get(SERIES_LAST_DATE_FIELD)
    if prev_last is not None and str(prev_last) == str(series_last_date):
        print("OK: crush_spread snapshot já está atualizado.")
        return

    s = df["close"]

    # -------------------------
    # NÍVEL: percentil em janela de 10 anos
    # -------------------------
    last_val = float(s.iloc[-1])
    percentil = _percentil_in_window(s, PERCENTIL_WINDOW_DAYS)
    nivel_texto, nivel_score = _bucket_nivel_by_percentil(percentil)

    # -------------------------
    # TENDÊNCIA: ratio MM20 / MM60
    # -------------------------
    mm_short = float(s.tail(MM_SHORT).mean())
    mm_long = float(s.tail(MM_LONG).mean())
    ratio_trend = mm_short / mm_long if mm_long != 0 else 1.0
    tendencia_texto, tendencia_score = _bucket_trend(ratio_trend)

    # -------------------------
    # MOMENTO: MM20 hoje vs MM20 de 20 dias atrás
    # -------------------------
    mm_short_lag = float(s.iloc[-(MM_LAG + MM_SHORT):-MM_LAG].mean())
    ratio_mom = mm_short / mm_short_lag if mm_short_lag != 0 else 1.0
    momento_texto, momento_score = _bucket_momento(ratio_mom)

    # -------------------------
    # Score final (bloco 1, sem inversão)
    # -------------------------
    mult_bloco = 1.0 if BLOCO == 1 else -1.0
    score = (nivel_score + tendencia_score + momento_score) * mult_bloco
    if score == 0.0:
        score = 0.0

    peso = float(row.get("peso", DEFAULT_PESO))
    score_ponderado = score * peso
    if score_ponderado == 0.0:
        score_ponderado = 0.0

    row.update({
        "bloco": BLOCO,
        "variavel": VAR_NAME,
        "ultimo_valor": round(last_val, 4),
        "percentil": round(float(percentil), 4) if not np.isnan(percentil) else None,
        "nivel": nivel_texto,
        "valor_nivel": float(nivel_score),
        "tendencia": tendencia_texto,
        "valor_tendencia": float(tendencia_score),
        "momento": momento_texto,
        "valor_momento": float(momento_score),
        "score": round(score, 4),
        "peso": peso,
        "score_ponderado": round(score_ponderado, 4),
        "frequencia": FREQUENCIA,
        "ultima_atualizacao": str(series_last_date),
        "regra_de_sinal": REGRA_DE_SINAL,
        "fonte": FONTE,
        SERIES_LAST_DATE_FIELD: str(series_last_date),
        "mm20": round(mm_short, 4),
        "mm60": round(mm_long, 4),
        "ratio_trend": round(float(ratio_trend), 4),
        "ratio_momento": round(float(ratio_mom), 4),
    })

    snapshot["updated_at"] = _now_utc_str()

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print(f"OK: painel_snapshot.json (soja) atualizado para crush_spread.")
    print(f"Último: ${last_val:.3f}/bu | percentil 10y: {percentil:.2%}")
    print(f"MM20: ${mm_short:.3f} | MM60: ${mm_long:.3f} | ratio_trend: {ratio_trend:.3f}")
    print(f"MM20 lag: ${mm_short_lag:.3f} | ratio_momento: {ratio_mom:.3f}")
    print(f"Nível: {nivel_texto}({nivel_score}) | Tend: {tendencia_texto}({tendencia_score}) | Mom: {momento_texto}({momento_score})")
    print(f"Score: {score} | Peso: {peso} | Score ponderado: {score_ponderado}")


if __name__ == "__main__":
    main()
