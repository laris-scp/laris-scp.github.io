import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

SERIES_PATH = Path("data/soja/series/esr.json")
SNAPSHOT_PATH = Path("data/soja/painel_snapshot.json")

VAR_ID = "esr_export"
VAR_NAME = "EXPORTAÇÃO (USDA ESR) – SOJA EUA"
FREQUENCIA = "Semanal"
BLOCO = 1  # demanda direta — exportação forte = altista, mult_bloco = +1
FONTE = "USDA FAS – Export Sales Reporting (ESR) · Soybeans · destino WORLD"

SERIES_LAST_DATE_FIELD = "ultima_data_serie"

# Métrica principal: total comprometido acumulado da safra (pace).
METRIC = "total_commitment"

# Thresholds (padrão do painel)
Z_MUITO = 1.5
Z_ALTO = 0.5

TREND_LOW = 0.90
TREND_HIGH = 1.10

MOM_QACEL = 0.85
MOM_QDESA = 0.95
MOM_NEUT1 = 1.05
MOM_ADESA = 1.15

WINDOW_YEARS = 5

DEFAULT_PESO = 2.0


def _now_utc_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _robust_z(x: float, ref: pd.Series) -> float:
    """Z-score robusto usando mediana e MAD (resistente a outliers)."""
    ref = pd.to_numeric(ref, errors="coerce").dropna()
    if len(ref) < 3:
        return 0.0
    med = float(ref.median())
    mad = float((ref - med).abs().median())
    if mad <= 0 or np.isnan(mad):
        return 0.0
    denom = 1.4826 * mad
    return float((x - med) / denom) if denom != 0 else 0.0


def _bucket_nivel(z: float):
    if z <= -Z_MUITO:
        return "MUITO BAIXO", -1.0
    if z <= -Z_ALTO:
        return "BAIXO", -0.5
    if z < Z_ALTO:
        return "NEUTRO", 0.0
    if z < Z_MUITO:
        return "ALTO", 0.5
    return "MUITO ALTO", 1.0


def _bucket_trend(ratio: float):
    if ratio < TREND_LOW:
        return "QUEDA", -1.0
    if ratio <= TREND_HIGH:
        return "LATERAL", 0.0
    return "ALTA", 1.0


def _bucket_momento(ratio: float):
    if ratio < MOM_QACEL:
        return "QUEDA ACELERANDO", -1.0
    if ratio < MOM_QDESA:
        return "QUEDA DESACELERANDO", -0.5
    if ratio <= MOM_NEUT1:
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
    pts = series.get("series") or series.get("data") or series.get("points") or []
    if not isinstance(pts, list) or len(pts) == 0:
        raise RuntimeError("esr.json sem lista de dados (series/data/points).")

    df = pd.DataFrame(pts).copy()
    needed = {"date", "market_year", "week_index", METRIC}
    if not needed.issubset(df.columns):
        raise RuntimeError(f"esr.json precisa conter colunas: {sorted(needed)}.")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["market_year"] = pd.to_numeric(df["market_year"], errors="coerce")
    df["week_index"] = pd.to_numeric(df["week_index"], errors="coerce")
    df[METRIC] = pd.to_numeric(df[METRIC], errors="coerce")
    df = df.dropna(subset=["date", "market_year", "week_index", METRIC])
    df = df.sort_values("date").reset_index(drop=True)
    df["market_year"] = df["market_year"].astype(int)
    df["week_index"] = df["week_index"].astype(int)

    last = df.iloc[-1]
    last_date = last["date"]
    series_last_date = last_date.strftime("%Y-%m-%d")
    cur_my = int(last["market_year"])
    cur_week = int(last["week_index"])

    if not SNAPSHOT_PATH.exists():
        raise RuntimeError(f"Snapshot não encontrado: {SNAPSHOT_PATH}")

    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    row = _ensure_row(snapshot)

    # Early exit: se a data da série não mudou, sai
    prev_last = row.get(SERIES_LAST_DATE_FIELD)
    if prev_last is not None and str(prev_last) == str(series_last_date):
        print("OK: esr_export soja snapshot já está atualizado. (sem mudança na série)")
        return

    # Valor corrente: total_commitment da última semana da safra atual
    cur_value = float(last[METRIC])

    # Referência: mesmas safras anteriores (5y), comparadas na MESMA semana da safra.
    ref_mys = [my for my in range(cur_my - WINDOW_YEARS, cur_my) if my in df["market_year"].unique()]

    # Para cada safra de referência, pega o total_commitment na semana mais próxima de cur_week.
    ref_values = []
    for my in ref_mys:
        sub = df[df["market_year"] == my]
        if sub.empty:
            continue
        idx = (sub["week_index"] - cur_week).abs().idxmin()
        ref_values.append(float(sub.loc[idx, METRIC]))
    ref_series = pd.Series(ref_values, dtype="float64")

    # -------------------------
    # NÍVEL: z-score do commitment atual vs. mesma semana da safra nos últimos 5 anos
    # -------------------------
    z = _robust_z(cur_value, ref_series)
    nivel_texto, nivel_score = _bucket_nivel(z)

    # -------------------------
    # TENDÊNCIA: commitment atual vs. mediana histórica da mesma semana (5y)
    # -------------------------
    hist_med = float(ref_series.median()) if len(ref_series) else np.nan
    ratio_trend = float(cur_value / hist_med) if (hist_med and not np.isnan(hist_med) and hist_med != 0) else 1.0
    tendencia_texto, tendencia_score = _bucket_trend(ratio_trend)

    # -------------------------
    # MOMENTO: ritmo das últimas 4 semanas vs. mesmas 4 semanas da safra anterior
    # (mede se as vendas estão acelerando ou desacelerando vs. o ano passado)
    # -------------------------
    momento_texto, momento_score, ratio_mom = "NEUTRO", 0.0, 1.0
    cur_my_df = df[df["market_year"] == cur_my].sort_values("week_index")
    prev_my_df = df[df["market_year"] == cur_my - 1].sort_values("week_index")

    if len(cur_my_df) >= 5 and not prev_my_df.empty:
        last4 = cur_my_df.tail(4)
        gain_cur = float(last4[METRIC].iloc[-1] - last4[METRIC].iloc[0])

        weeks_target = set(last4["week_index"].tolist())
        prev_sub = prev_my_df[prev_my_df["week_index"].isin(weeks_target)].sort_values("week_index")
        if len(prev_sub) >= 2:
            gain_prev = float(prev_sub[METRIC].iloc[-1] - prev_sub[METRIC].iloc[0])
            if gain_prev != 0:
                ratio_mom = float(gain_cur / gain_prev)
                momento_texto, momento_score = _bucket_momento(ratio_mom)

    # -------------------------
    # Percentil informacional: posição do commitment atual entre as safras de ref (mesma semana)
    # -------------------------
    if len(ref_series) > 0:
        percentil = float((ref_series < cur_value).mean())
    else:
        percentil = float("nan")

    # -------------------------
    # Score final (bloco 1 -> mult_bloco = +1, sem inversão)
    # -------------------------
    mult_bloco = 1.0 if BLOCO == 1 else -1.0
    score_raw = float(nivel_score + tendencia_score + momento_score)
    score = score_raw * mult_bloco
    if score == 0.0:
        score = 0.0

    peso = float(row.get("peso", DEFAULT_PESO))
    score_ponderado = float(score * peso)
    if score_ponderado == 0.0:
        score_ponderado = 0.0

    regra_de_sinal = (
        "Mede o pace da safra dos EUA: total comprometido acumulado (vendas + embarques) "
        "da soja americana, somando todos os destinos (WORLD). "
        "Nível: compara o total comprometido da safra atual com o mesmo ponto da safra "
        "(mesma semana, safra set-ago) nos últimos 5 anos. "
        "Tendência: compara com a mediana histórica do mesmo ponto da safra (5y). "
        "Momento: compara o ganho das últimas 4 semanas com o ganho nas mesmas semanas "
        "da safra anterior (vendas acelerando ou desacelerando). "
        "Exportação dos EUA acima do padrão indica demanda externa forte e tende a fazer o preço CBOT subir."
    )

    row.update({
        "bloco": BLOCO,
        "variavel": VAR_NAME,
        "ultimo_valor": round(cur_value, 2),
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
        "regra_de_sinal": regra_de_sinal,
        "fonte": FONTE,
        SERIES_LAST_DATE_FIELD: str(series_last_date),
        "market_year": cur_my,
        "semana_safra": cur_week,
        "commitment_atual_t": round(cur_value, 2),
        "commitment_mediana_5y_t": round(hist_med, 2) if not np.isnan(hist_med) else None,
    })

    snapshot["updated_at"] = _now_utc_str()

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json (soja) atualizado para esr_export.")
    print(f"Safra MY {cur_my} | semana {cur_week} | commitment: {cur_value:,.0f} t")
    print(f"Mediana 5y (mesma semana): {hist_med:,.0f} t" if not np.isnan(hist_med) else "Mediana 5y: n/d")
    print(f"z (nível): {z:.2f} | ratio_trend: {ratio_trend:.3f} | ratio_mom: {ratio_mom:.3f}")
    print(f"Nível: {nivel_texto}({nivel_score}) | Tend: {tendencia_texto}({tendencia_score}) | Mom: {momento_texto}({momento_score})")
    print(f"Score: {score} | Peso: {peso} | Score ponderado: {score_ponderado}")


if __name__ == "__main__":
    main()
