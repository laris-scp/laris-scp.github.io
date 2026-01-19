import json
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

SERIES_PATH = Path("data/cafe/series/gscpi_fretes.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

# Mantido (não usamos mais na tendência, mas deixei para não mexer no restante do arquivo)
EPS_TREND = 0.05  # conforme seu Colab

# ---- NOVA REGRA (tendencia_3) - parâmetros que você escolheu ----
MM_SLOPE = 6           # MM6
SLOPE_WIN = 12         # janela de slope (12 meses)
SLOPE_STD_WIN = 36     # std de referência (36 meses)
SLOPE_MULT = 0.10      # threshold (0.10 * std)

def percentile_rank_leq(values, value) -> float:
    """
    Percentil conforme seu Colab: (s <= value).sum / len(s)
    """
    s = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if len(s) == 0:
        return float("nan")
    return float((s <= value).sum() / len(s))

def map_percentil_to_nivel(p):
    if pd.isna(p): return ("NEUTRO", 0.0)
    if p < 0.20: return ("MUITO BAIXO", -1.0)
    if p < 0.40: return ("BAIXO", -0.5)
    if p < 0.60: return ("NEUTRO", 0.0)
    if p < 0.80: return ("ALTO", 0.5)
    return ("MUITO ALTO", 1.0)

def map_tendencia_to_val(t):
    if t == "ALTA": return 1.0
    if t == "QUEDA": return -1.0
    return 0.0

def map_momento_to_val(m):
    if m == "QUEDA ACELERANDO": return -1.0
    if m == "QUEDA DESACELERANDO": return -0.5
    if m == "ALTA ACELERANDO": return 1.0
    if m == "ALTA DESACELERANDO": return 0.5
    return 0.0

# -----------------------------
# Helpers NOVOS (tendencia_3)
# -----------------------------
def _slope_ols(y: np.ndarray) -> float:
    n = len(y)
    if n < 2:
        return float("nan")
    x = np.arange(n, dtype=float)
    y = y.astype(float)
    x_mean = x.mean()
    y_mean = y.mean()
    denom = ((x - x_mean) ** 2).sum()
    if denom == 0:
        return float("nan")
    return float(((x - x_mean) * (y - y_mean)).sum() / denom)

def rolling_slope(series: pd.Series, window: int) -> pd.Series:
    # min_periods sempre <= window (evita ValueError)
    minp = max(3, window // 2)
    minp = min(minp, window)
    return series.rolling(window, min_periods=minp).apply(
        lambda x: _slope_ols(np.asarray(x, dtype=float)),
        raw=False
    )

def main():
    # 1) Carrega série
    series_json = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    df = pd.DataFrame(series_json["series"])

    if df.empty or "date" not in df.columns or "close" not in df.columns:
        raise RuntimeError("Série GSCPI inválida: faltam colunas 'date' e/ou 'close'.")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    ultimo_date = df.iloc[-1]["date"].strftime("%Y-%m-%d")

    if len(df) < 3:
        raise RuntimeError("Histórico insuficiente para GSCPI (mínimo 3 pontos).")

    v0 = float(df.iloc[-1]["close"])

    # 2) Nível (percentil no histórico completo) - mantido
    percentil = percentile_rank_leq(df["close"], v0)
    nivel_cat, val_nivel = map_percentil_to_nivel(percentil)

    # =========================================================
    # 3) TENDÊNCIA (NOVA: MM6 + slope em 12m + threshold dinâmico)
    # =========================================================
    s = df["close"].astype(float)

    mm6 = s.rolling(MM_SLOPE, min_periods=MM_SLOPE).mean()
    slope = rolling_slope(mm6, SLOPE_WIN)

    std = s.rolling(SLOPE_STD_WIN, min_periods=min(18, SLOPE_STD_WIN)).std()
    thr = SLOPE_MULT * std

    # último slope/threshold válidos (podem ser NaN se histórico curto)
    sl0 = float(slope.iloc[-1]) if pd.notna(slope.iloc[-1]) else float("nan")
    th0 = float(thr.iloc[-1]) if pd.notna(thr.iloc[-1]) else float("nan")

    tendencia = "INDEFINIDA"
    if pd.notna(sl0) and pd.notna(th0):
        if abs(sl0) <= th0:
            tendencia = "LATERAL"
        elif sl0 > th0:
            tendencia = "ALTA"
        elif sl0 < -th0:
            tendencia = "QUEDA"

    # 4) MOMENTO (mantém o formato, mas agora baseado no slope ganhando/perdendo força)
    #    Ideia: se a tendência é ALTA e o slope aumentou, está acelerando; se caiu, desacelerando.
    momento = "NEUTRO"
    sl_prev = float(slope.iloc[-2]) if len(slope) >= 2 and pd.notna(slope.iloc[-2]) else float("nan")

    if tendencia == "ALTA" and pd.notna(sl_prev) and pd.notna(sl0):
        momento = "ALTA ACELERANDO" if sl0 > sl_prev else "ALTA DESACELERANDO"
    elif tendencia == "QUEDA" and pd.notna(sl_prev) and pd.notna(sl0):
        # para queda, mais negativo = acelerando a queda
        momento = "QUEDA ACELERANDO" if sl0 < sl_prev else "QUEDA DESACELERANDO"

    val_tend = map_tendencia_to_val(tendencia)
    val_mom = map_momento_to_val(momento)

    # 5) Score ajustado por bloco e ponderado por peso (mantido)
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    row = next(r for r in snapshot["rows"] if r["id"] == "gscpi")

    bloco = int(row.get("bloco", 1))
    mult_bloco = 1.0 if bloco == 1 else (-1.0 if bloco == 2 else 1.0)

    score = (val_nivel + val_tend + val_mom) * mult_bloco

    peso = float(row.get("peso", 1.0))
    score_ponderado = float(score) * float(peso)

    # Texto MAIS SIMPLES (pedido)
    rule_txt = (
        "Nível: compara o GSCPI atual com todo o histórico e diz se ele está baixo, normal ou alto. "
        f"Tendência: olha a direção do GSCPI nos últimos meses usando uma média móvel e mede se ele vem subindo, caindo ou ficando de lado "
        f"(regra mais estável: MM{MM_SLOPE} + tendência de {SLOPE_WIN} meses, com filtro de ruído). "
        "Momento: indica se essa tendência está ganhando força ou perdendo força."
    )

    row.update({
        "ultimo_valor": v0,
        "percentil": round(percentil, 4) if not pd.isna(percentil) else None,
        "nivel": nivel_cat,
        "valor_nivel": float(val_nivel),
        "tendencia": tendencia,
        "valor_tendencia": float(val_tend),
        "momento": momento,
        "valor_momento": float(val_mom),
        "score": float(score),
        "score_ponderado": float(score_ponderado),
        "frequencia": "Mensal",
        "ultima_atualizacao": ultimo_date,
        "regra_de_sinal": rule_txt,
        "fonte": "NY Fed – Global Supply Chain Pressure Index (GSCPI)",
    })

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json atualizado (gscpi).")
    print("Último:", v0, "| tendência:", tendencia, "| momento:", momento)
    print("DEBUG slope:", sl0, "| thr:", th0, "| slope_prev:", sl_prev)
    print("Score:", score, "| Peso:", peso, "| Score ponderado:", score_ponderado)

if __name__ == "__main__":
    main()
