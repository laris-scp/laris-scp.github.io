import json
from datetime import datetime
from pathlib import Path
import pandas as pd

SERIES_PATH = Path("data/soja/series/usd_brl.json")
SNAPSHOT_PATH = Path("data/soja/painel_snapshot.json")

MM_LONG = 252
MM_SHORT = 50

# Campo novo (extra; não deve quebrar o site)
SERIES_LAST_DATE_FIELD = "ultima_data_serie"


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


def percentile_rank(series: pd.Series, value: float) -> float:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) == 0:
        return float("nan")
    return float((s <= value).sum() / len(s))


RULE_TXT = (
    "Nível indica se o dólar está baixo ou alto versus seu comportamento histórico recente; "
    "Tendência mostra a direção atual comparando o valor com médias de curto e longo prazo; "
    "Momento indica se essa tendência está ganhando ou perdendo força ao observar a evolução mensal da média de curto prazo; "
    "Score combina Nível, Tendência e Momento (com peso) em um único indicador."
)

FONTE_TXT = "Banco Central do Brasil (SGS série 1)."


def main():
    # --- Ler série ---
    series_payload = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    pts = series_payload["series"]
    if not pts:
        raise RuntimeError("usd_brl.json (soja) sem pontos em 'series'.")

    series_last_date = pts[-1].get("date")
    if not series_last_date:
        raise RuntimeError("Não encontrei a última data em usd_brl.json (series[-1].date).")

    # --- Ler snapshot e localizar/criar usd_brl ---
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if not (isinstance(snapshot, dict) and "rows" in snapshot and isinstance(snapshot["rows"], list)):
        raise RuntimeError("Formato inesperado em painel_snapshot.json: esperado dict com chave 'rows' (lista).")

    rows = snapshot["rows"]

    item = None
    for r in rows:
        if r.get("id") == "usd_brl":
            item = r
            break

    # Se a row do usd_brl ainda nao existe (primeira execucao), cria
    if item is None:
        item = {
            "id": "usd_brl",
            "bloco": 1,
            "variavel": "USD/BRL",
            "peso": 4.0,
        }
        rows.append(item)

    # --- Early exit: não atualiza se a série não mudou ---
    prev_series_last_date = item.get(SERIES_LAST_DATE_FIELD)
    if prev_series_last_date is not None and str(prev_series_last_date) == str(series_last_date):

        # corrige legado: ultima_atualizacao com hora (timestamp antigo)
        if str(item.get("ultima_atualizacao")) != str(series_last_date):
            item["ultima_atualizacao"] = str(series_last_date)
            item[SERIES_LAST_DATE_FIELD] = str(series_last_date)

            snapshot["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            SNAPSHOT_PATH.write_text(
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8"
            )

            print(f"Sem dados novos para usd_brl (soja), mas corrigi ultima_atualizacao para {series_last_date}.")
            return

        print(f"Sem dados novos para usd_brl (soja) no snapshot. Última data: {series_last_date}")
        return

    # --- DF ---
    df = pd.DataFrame(pts)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna().sort_values("date").reset_index(drop=True)

    for c in ["close", "mm50", "mm252"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().reset_index(drop=True)

    if len(df) < MM_LONG + 60:
        raise RuntimeError(f"Poucos dados após MM: {len(df)}")

    ult = float(df.iloc[-1]["close"])
    last_mm252 = float(df.iloc[-1]["mm252"])
    last_mm50 = float(df.iloc[-1]["mm50"])

    # Nível
    df["dist_mm252"] = df["close"] - df["mm252"]
    percentil = percentile_rank(df["dist_mm252"], ult - last_mm252)
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
    df_m = df.copy()
    df_m["ym"] = df_m["date"].dt.to_period("M").astype(str)
    mms = df_m.groupby("ym")["mm50"].last().dropna()
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

    # Atualiza preservando peso
    peso = float(item.get("peso", 4.0))
    item.update(
        {
            "id": "usd_brl",
            "bloco": 1,
            "variavel": "USD/BRL",
            "ultimo_valor": ult,
            "percentil": float(round(percentil, 4)),
            "nivel": nivel_txt,
            "valor_nivel": float(valor_nivel),
            "tendencia": tendencia,
            "valor_tendencia": float(valor_tendencia),
            "momento": momento,
            "valor_momento": float(valor_momento),
            "score": float(score),
            "peso": peso,
            "score_ponderado": float(score * peso),
            "frequencia": "Diária",
            # PADRÃO: somente data (YYYY-MM-DD)
            "ultima_atualizacao": str(series_last_date),
            "regra_de_sinal": RULE_TXT,
            "fonte": FONTE_TXT,
            SERIES_LAST_DATE_FIELD: str(series_last_date),
        }
    )

    # Mantém updated_at do snapshot como timestamp (não mexer)
    snapshot["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snapshot["rows"] = rows

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: data/soja/painel_snapshot.json atualizado (usd_brl).")
    print("Últ:", ult, "| Nível:", nivel_txt, valor_nivel, "| Tend:", tendencia, valor_tendencia, "| Mom:", momento, valor_momento)
    print("Última data da série:", series_last_date)


if __name__ == "__main__":
    main()
