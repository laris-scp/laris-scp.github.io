import json
from datetime import datetime
from pathlib import Path
import pandas as pd

SERIES_PATH = Path("data/soja/series/preco_soja.json")
SNAPSHOT_PATH = Path("data/soja/painel_snapshot.json")

MM_LONG = 252
SERIES_LAST_DATE_FIELD = "ultima_data_serie"


def percentile_last(series: pd.Series) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) == 0:
        return float("nan")
    return float(s.rank(pct=True).iloc[-1])


def pct_to_level_and_value(p: float):
    if p < 0.20:
        return "MUITO BAIXO", -1.0
    elif p < 0.40:
        return "BAIXO", -0.5
    elif p < 0.60:
        return "NEUTRO", 0.0
    elif p < 0.80:
        return "ALTO", 0.5
    return "MUITO ALTO", 1.0


def tendencia_to_value(t: str) -> float:
    if t == "ALTA":
        return 1.0
    if t == "QUEDA":
        return -1.0
    return 0.0


def momento_to_value(m: str) -> float:
    if m == "ALTA ACELERANDO":
        return 1.0
    if m == "ALTA DESACELERANDO":
        return 0.5
    if m == "QUEDA ACELERANDO":
        return -1.0
    if m == "QUEDA DESACELERANDO":
        return -0.5
    return 0.0


RULE_TXT = (
    "Nível indica se o preço da soja está baixo ou alto versus o histórico recente; "
    "Tendência mostra a direção atual comparando o preço com médias de curto e longo prazo; "
    "Momento indica se essa tendência está ganhando ou perdendo força ao observar a evolução mensal da média de curto prazo; "
    "Score combina Nível, Tendência e Momento (com peso) em um único indicador."
)

FONTE_TXT = "Yahoo Finance (ZS=F)."


def main():
    # Ler série
    series_payload = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    pts = series_payload.get("series", [])
    if not pts:
        raise RuntimeError("preco_soja.json sem dados em 'series'.")

    series_last_date = pts[-1].get("date")
    if not series_last_date:
        raise RuntimeError("Não encontrei a última data da série.")

    # Ler snapshot
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("painel_snapshot.json esperado com 'rows' (lista).")

    item = None
    for r in rows:
        if r.get("id") == "preco_soja":
            item = r
            break
    if item is None:
        raise RuntimeError("Não encontrei id='preco_soja' no snapshot.")

    # Early exit se série não mudou
    prev_series_last_date = item.get(SERIES_LAST_DATE_FIELD)
    prev_last_value = item.get("ultimo_valor")

    series_last_value = float(pts[-1].get("close"))

    if (
        prev_series_last_date is not None
        and str(prev_series_last_date) == str(series_last_date)
        and prev_last_value is not None
        and abs(float(prev_last_value) - series_last_value) < 1e-6
    ):
        print(
            f"Sem dados novos para preco_soja. "
            f"Última data: {series_last_date} | Último valor: {series_last_value}"
        )
        return

    # DataFrame
    df = pd.DataFrame(pts)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ["close", "mm50", "mm252"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().sort_values("date").reset_index(drop=True)

    if len(df) < MM_LONG + 60:
        raise RuntimeError(f"Poucos dados após MM: {len(df)}")

    ult = float(df.iloc[-1]["close"])
    last_mm50 = float(df.iloc[-1]["mm50"])
    last_mm252 = float(df.iloc[-1]["mm252"])

    # Nível
    df["dist_rel"] = (df["close"] - df["mm252"]) / df["mm252"].abs()
    percentil = percentile_last(df["dist_rel"])
    nivel_txt, valor_nivel = pct_to_level_and_value(percentil)

    # Tendência
    if ult > last_mm50 and ult > last_mm252:
        tendencia = "ALTA"
    elif ult < last_mm50 and ult < last_mm252:
        tendencia = "QUEDA"
    else:
        tendencia = "NEUTRO"
    valor_tendencia = tendencia_to_value(tendencia)

    # Momento (MM50 mensal)
    df["ym"] = df["date"].dt.to_period("M").astype(str)
    mms = df.groupby("ym")["mm50"].last().dropna()

    if len(mms) < 3:
        momento = "NEUTRO"
    else:
        m0, m1, m2 = float(mms.iloc[-1]), float(mms.iloc[-2]), float(mms.iloc[-3])
        d1 = m1 - m2
        d2 = m0 - m1
        if tendencia == "ALTA":
            momento = "ALTA ACELERANDO" if abs(d2) > abs(d1) else "ALTA DESACELERANDO"
        elif tendencia == "QUEDA":
            momento = "QUEDA ACELERANDO" if abs(d2) > abs(d1) else "QUEDA DESACELERANDO"
        else:
            momento = "NEUTRO"

    valor_momento = momento_to_value(momento)
    score = float(valor_nivel + valor_tendencia + valor_momento)

    peso = float(item.get("peso", 1.0))

    # Atualiza snapshot
    item.update({
        "ultimo_valor": ult,
        "percentil": round(percentil, 4),
        "nivel": nivel_txt,
        "valor_nivel": valor_nivel,
        "tendencia": tendencia,
        "valor_tendencia": valor_tendencia,
        "momento": momento,
        "valor_momento": valor_momento,
        "score": score,
        "score_ponderado": score * peso,
        "frequencia": "Diária",
        "ultima_atualizacao": str(series_last_date),
        "regra_de_sinal": RULE_TXT,
        "fonte": FONTE_TXT,
        SERIES_LAST_DATE_FIELD: str(series_last_date),
    })

    snapshot["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("OK: painel_snapshot.json atualizado (preco_soja).")
    print("Última data da série:", series_last_date)


if __name__ == "__main__":
    main()
