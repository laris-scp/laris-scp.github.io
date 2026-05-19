import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

SERIES_PATH = Path("data/soja/series/mdic_export.json")
SNAPSHOT_PATH = Path("data/soja/painel_snapshot.json")

VAR_ID = "mdic_export"
VAR_NAME = "EXPORTAÇÃO (MDIC) – SOJA EM GRÃO"
FREQUENCIA = "Mensal"
BLOCO = 2  # fundamental, mult_bloco = -1
FONTE = "MDIC/ComexStat – Exportação Brasil (NCM 12019000)"

SERIES_LAST_DATE_FIELD = "ultima_data_serie"

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

# Janela "ativa" para o cálculo do Momento (alta sazonal de exportação)
# Fora dessa janela, Momento força NEUTRO (evita ruído de meses de baixa estação)
MOMENTO_ACTIVE_MONTHS = {1, 2, 3, 4, 5, 6, 7}

DEFAULT_PESO = 2.0


def _now_utc_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _percentile_last(values: pd.Series) -> float:
    arr = pd.to_numeric(values, errors="coerce").dropna().values
    if len(arr) == 0:
        return float("nan")
    return float((arr < arr[-1]).mean())


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
        raise RuntimeError("mdic_export.json sem lista de dados (series/data/points).")

    df = pd.DataFrame(pts).copy()
    if "date" not in df.columns or "kg" not in df.columns or "toneladas" not in df.columns:
        raise RuntimeError("mdic_export.json precisa conter colunas: date, kg, toneladas.")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["kg"] = pd.to_numeric(df["kg"], errors="coerce")
    df["toneladas"] = pd.to_numeric(df["toneladas"], errors="coerce")
    df = df.dropna(subset=["date", "toneladas"]).sort_values("date").reset_index(drop=True)

    last_date = df.iloc[-1]["date"]
    series_last_date = last_date.strftime("%Y-%m-%d")
    y = int(last_date.year)
    m = int(last_date.month)

    if not SNAPSHOT_PATH.exists():
        raise RuntimeError(f"Snapshot não encontrado: {SNAPSHOT_PATH}")

    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    row = _ensure_row(snapshot)

    # Early exit: se a data da série não mudou, sai
    prev_last = row.get(SERIES_LAST_DATE_FIELD)
    if prev_last is not None and str(prev_last) == str(series_last_date):
        print("OK: mdic_export soja snapshot já está atualizado. (sem mudança na série)")
        return

    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month

    # Referência: últimos 5 anos, EXCLUINDO ano corrente (sem vazamento)
    ref_years = [yy for yy in range(y - WINDOW_YEARS, y) if yy in df["year"].unique()]

    # -------------------------
    # NÍVEL (Opção C): z-score do acumulado YTD jan-até-m vs. histórico do mesmo acumulado
    # -------------------------
    # Acumulado do ano corrente até o mês m
    acc_y = float(df[(df["year"] == y) & (df["month"] <= m)]["toneladas"].sum())

    # Acumulado por ano nos anos de referência, até o mesmo mês m
    ref_acc_by_year = (
        df[df["year"].isin(ref_years) & (df["month"] <= m)]
        .groupby("year")["toneladas"].sum()
    )
    z = _robust_z(acc_y, ref_acc_by_year)
    nivel_texto, nivel_score = _bucket_nivel(z)

    # -------------------------
    # TENDÊNCIA: acumulado YTD vs mediana histórica do mesmo acumulado
    # -------------------------
    hist_acc_med = float(ref_acc_by_year.median()) if len(ref_acc_by_year) else np.nan
    ratio_trend = float(acc_y / hist_acc_med) if (hist_acc_med and not np.isnan(hist_acc_med)) else 1.0

    tendencia_texto, tendencia_score = _bucket_trend(ratio_trend)

    # -------------------------
    # MOMENTO: YoY da média móvel 3M
    # Janela ativa jan-jul; fora dela, força NEUTRO
    # -------------------------
    momento_texto, momento_score, ratio_mom = "NEUTRO", 0.0, 1.0
    momento_ativo = m in MOMENTO_ACTIVE_MONTHS

    if momento_ativo and len(df) >= 15:
        last3 = df.tail(3)["toneladas"].mean()

        # Pega os 3 meses equivalentes do ano anterior
        months_3 = []
        mm = m
        for _ in range(3):
            months_3.append(mm)
            mm -= 1
            if mm == 0:
                mm = 12
        prev3 = df[(df["year"] == y - 1) & (df["month"].isin(months_3))]["toneladas"].mean()
        if prev3 and not np.isnan(prev3) and prev3 != 0:
            ratio_mom = float(last3 / prev3)
            momento_texto, momento_score = _bucket_momento(ratio_mom)

    # -------------------------
    # Percentil (histórico completo, para referência informacional)
    # -------------------------
    percentil = _percentile_last(df["toneladas"])

    # -------------------------
    # Score final (bloco 2 -> mult_bloco = -1)
    # -------------------------
    mult_bloco = 1.0 if BLOCO == 1 else -1.0
    score_raw = float(nivel_score + tendencia_score + momento_score)
    score = score_raw * mult_bloco

    # Normaliza -0.0 -> 0.0
    if score == 0.0:
        score = 0.0

    peso = float(row.get("peso", DEFAULT_PESO))
    score_ponderado = float(score * peso)
    if score_ponderado == 0.0:
        score_ponderado = 0.0

    regra_de_sinal = (
        "Nível: compara o acumulado YTD (jan até o mês atual) com a sazonalidade do mesmo "
        "acumulado nos últimos 5 anos. "
        "Tendência: compara o acumulado do ano com a mediana histórica do mesmo período (5y). "
        "Momento: mede se a exportação está acelerando vs. o ano passado (YoY da média móvel 3M). "
        "Fora da janela de alta safra (ago-dez), o Momento fica NEUTRO para evitar ruído. "
        "Exportação acima do padrão indica demanda externa forte e tende a fazer o preço subir."
    )

    row.update({
        "bloco": BLOCO,
        "variavel": VAR_NAME,
        "ultimo_valor": round(float(df.iloc[-1]["toneladas"]), 2),
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
        "momento_fora_janela": bool(not momento_ativo),
        "ytd_acumulado_t": round(acc_y, 2),
        "ytd_media_5y_t": round(hist_acc_med, 2) if not np.isnan(hist_acc_med) else None,
    })

    snapshot["updated_at"] = _now_utc_str()

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json (soja) atualizado para mdic_export.")
    print(f"Último (toneladas): {row['ultimo_valor']} | mês: {m} | momento ativo? {momento_ativo}")
    print(f"YTD acumulado: {acc_y:,.0f} t | YTD mediana 5y: {hist_acc_med:,.0f} t")
    print(f"z (nível): {z:.2f} | ratio_trend: {ratio_trend:.3f} | ratio_mom: {ratio_mom:.3f}")
    print(f"Nível: {nivel_texto}({nivel_score}) | Tend: {tendencia_texto}({tendencia_score}) | Mom: {momento_texto}({momento_score})")
    print(f"Score: {score} | Peso: {peso} | Score ponderado: {score_ponderado}")


if __name__ == "__main__":
    main()
