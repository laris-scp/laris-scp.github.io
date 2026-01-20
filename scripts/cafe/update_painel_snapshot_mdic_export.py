import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

SERIES_PATH = Path("data/cafe/series/mdic_export.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

VAR_ID = "mdic_export"
VAR_NAME = "EXPORTAÇÃO (MDIC) – CAFÉ VERDE"
FREQUENCIA = "Mensal"
BLOCO = 1
FONTE = "MDIC/ComexStat – Exportação Brasil (API) | NCM 09011110 + 09011190 | Métrica: KG"

SERIES_LAST_DATE_FIELD = "ultima_data_serie"

# thresholds (padrão do painel)
Z_MUITO = 1.5
Z_ALTO = 0.5

TREND_LOW = 0.90
TREND_HIGH = 1.10

MOM_QACEL = 0.85
MOM_QDESA = 0.95
MOM_NEUT1 = 1.05
MOM_ADESA = 1.15

WINDOW_YEARS = 5


def _now_utc_str():
    # Mantive utcnow para consistência com seus scripts atuais
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _percentile_last(values: pd.Series) -> float:
    arr = pd.to_numeric(values, errors="coerce").dropna().values
    if len(arr) == 0:
        return float("nan")
    return float((arr < arr[-1]).mean())


def _robust_z(x: float, ref: pd.Series) -> float:
    ref = pd.to_numeric(ref, errors="coerce").dropna()
    if len(ref) < 3:
        return 0.0
    med = float(ref.median())
    mad = float((ref - med).abs().median())
    if mad <= 0 or np.isnan(mad):
        return 0.0
    # escala robusta ~ desvio padrão
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

    # cria nova linha no padrão do painel
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
        "peso": 2.0,  # você definiu
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
    pts = series.get("data") or series.get("points") or series.get("series") or []
    if not isinstance(pts, list) or len(pts) == 0:
        raise RuntimeError("mdic_export.json sem lista de dados (data/points).")

    df = pd.DataFrame(pts).copy()
    if "date" not in df.columns or "kg" not in df.columns or "bags_60kg" not in df.columns:
        raise RuntimeError("mdic_export.json precisa conter colunas: date, kg, bags_60kg.")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["kg"] = pd.to_numeric(df["kg"], errors="coerce")
    df["bags_60kg"] = pd.to_numeric(df["bags_60kg"], errors="coerce")
    df = df.dropna(subset=["date", "bags_60kg"]).sort_values("date").reset_index(drop=True)

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
        print("OK: mdic_export snapshot já está atualizado. (sem mudança na série)")
        return

    # -------------------------
    # Referência: últimos 5 anos, EXCLUINDO ano corrente (sem vazamento)
    # -------------------------
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month

    ref_years = [yy for yy in range(y - WINDOW_YEARS, y) if yy in df["year"].unique()]
    ref = df[df["year"].isin(ref_years)].copy()

    # -------------------------
    # NÍVEL: z robusto sazonal (mesmo mês, janela 5y)
    # -------------------------
    v_last = float(df.iloc[-1]["bags_60kg"])
    ref_month = ref[ref["month"] == m]["bags_60kg"]
    z = _robust_z(v_last, ref_month)
    nivel_texto, nivel_score = _bucket_nivel(z)

    # -------------------------
    # TENDÊNCIA: acumulado do ano até mês m vs mediana do acumulado (5y)
    # -------------------------
    acc_y = float(df[(df["year"] == y) & (df["month"] <= m)]["bags_60kg"].sum())

    hist_acc_by_year = (
        ref[ref["month"] <= m]
        .groupby("year")["bags_60kg"].sum()
    )
    hist_acc_med = float(hist_acc_by_year.median()) if len(hist_acc_by_year) else np.nan
    ratio_trend = float(acc_y / hist_acc_med) if (hist_acc_med and not np.isnan(hist_acc_med)) else 1.0

    tendencia_texto, tendencia_score = _bucket_trend(ratio_trend)

    # -------------------------
    # MOMENTO: YoY da média móvel 3M (estável e alinhado à sua regra)
    # -------------------------
    # precisa de pelo menos 15 meses para ter 3m atual e 3m do ano anterior
    momento_texto, momento_score, ratio_mom = "NEUTRO", 0.0, 1.0

    if len(df) >= 15:
        last3 = df.tail(3)["bags_60kg"].mean()
        # pega os 3 meses equivalentes do ano anterior:
        # usando datas: último mês (y,m) -> meses (m-2,m-1,m) do ano y-1
        # aproximamos por seleção por (year == y-1) e month in {m, m-1, m-2} com ajuste de wrap
        months_3 = []
        mm = m
        for _ in range(3):
            months_3.append(mm)
            mm -= 1
            if mm == 0:
                mm = 12
        # se houve wrap, o ano anterior correto para os meses 11/12 é y-1 (ok) e para mês 1 também y-1 (ok)
        prev3 = df[(df["year"] == y - 1) & (df["month"].isin(months_3))]["bags_60kg"].mean()
        if prev3 and not np.isnan(prev3) and prev3 != 0:
            ratio_mom = float(last3 / prev3)
            momento_texto, momento_score = _bucket_momento(ratio_mom)

    # -------------------------
    # Percentil (histórico completo)
    # -------------------------
    percentil = _percentile_last(df["bags_60kg"])

    # -------------------------
    # Score final
    # -------------------------
    score = float(nivel_score + tendencia_score + momento_score)
    peso = float(row.get("peso", 2.0))
    score_ponderado = float(score * peso)

    regra_de_sinal = (
        "Nível: compara a exportação do mês com a sazonalidade do mesmo mês nos últimos 5 anos (robusto). "
        "Tendência: compara o acumulado do ano (até o mês atual) com o padrão dos últimos 5 anos. "
        "Momento: mede se a exportação está acelerando vs o ano passado (YoY da média dos últimos 3 meses). "
        "Exportação acima do padrão tende a ser bullish (maior escoamento externo da oferta)."
    )

    # -------------------------
    # Atualiza linha no snapshot
    # -------------------------
    row.update({
        "bloco": BLOCO,
        "variavel": VAR_NAME,
        "ultimo_valor": round(v_last, 2),
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
    })

    snapshot["updated_at"] = _now_utc_str()

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json atualizado (mdic_export).")
    print("Último:", v_last, "| z:", round(z, 2), "| ratio_trend:", round(ratio_trend, 3), "| ratio_mom:", round(ratio_mom, 3))
    print("Score:", score, "| Peso:", peso, "| Score ponderado:", score_ponderado)


if __name__ == "__main__":
    main()
