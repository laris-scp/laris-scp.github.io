import json
from datetime import datetime
from pathlib import Path
import pandas as pd

SERIES_PATH = Path("data/cafe/series/preco_arabica.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

MM_LONG = 252

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
    else:
        return "MUITO ALTO", 1.0

def tendencia_to_value(t: str) -> float:
    t = t.upper()
    if t == "QUEDA":
        return -1.0
    if t == "ALTA":
        return 1.0
    return 0.0

def momento_to_value(m: str) -> float:
    m = " ".join(m.upper().replace("-", " ").split())
    if m == "QUEDA ACELERANDO":
        return -1.0
    if m == "QUEDA DESACELERANDO":
        return -0.5
    if m == "ALTA DESACELERANDO":
        return 0.5
    if m == "ALTA ACELERANDO":
        return 1.0
    return 0.0

RULE_TXT = (
    "Nível indica se o preço está baixo ou alto versus o histórico recente; "
    "Tendência mostra a direção atual comparando o preço com médias de curto e longo prazo; "
    "Momento indica se essa tendência está ganhando ou perdendo força ao observar a evolução mensal da média; "
    "Score combina Nível, Tendência e Momento (com peso e ajuste por bloco) em um único indicador."
)

FONTE_TXT = "Yahoo Finance (KC=F) | diário | Close | MMs 50/252 | janela nível 10a"

def main():
    series_payload = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    df = pd.DataFrame(series_payload["series"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    for c in ["close", "mm50", "mm252"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().reset_index(drop=True)

    ult = float(df.iloc[-1]["close"])
    last_mm50 = float(df.iloc[-1]["mm50"])
    last_mm252 = float(df.iloc[-1]["mm252"])

    # NÍVEL
    df["dist_rel"] = (df["close"] - df["mm252"]) / df["mm252"].abs()
    percentil = percentile_last(df["dist_rel"])
    nivel_txt, valor_nivel = pct_to_level_and_value(percentil)

    # TENDÊNCIA
    if ult > last_mm50 and ult > last_mm252:
        tendencia = "ALTA"
    elif ult < last_mm50 and ult < last_mm252:
        tendencia = "QUEDA"
    else:
        tendencia = "LATERAL"
    valor_tendencia = tendencia_to_value(tendencia)

    # MOMENTO
    df["mes"] = df["date"].dt.to_period("M")
    mm50_monthly = df.groupby("mes").last().reset_index()

    momento = "NEUTRO"
    if len(mm50_monthly) >= 3:
        m2 = float(mm50_monthly.iloc[-3]["mm50"])
        m1 = float(mm50_monthly.iloc[-2]["mm50"])
        m0 = float(mm50_monthly.iloc[-1]["mm50"])
        d1, d2 = m1 - m2, m0 - m1

        if tendencia == "ALTA":
            momento = "ALTA ACELERANDO" if abs(d2) > abs(d1) else "ALTA DESACELERANDO"
        elif tendencia == "QUEDA":
            momento = "QUEDA ACELERANDO" if abs(d2) > abs(d1) else "QUEDA DESACELERANDO"

    valor_momento = momento_to_value(momento)
    score = float(valor_nivel + valor_tendencia + valor_momento)

    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    rows = snapshot["rows"]

    for item in rows:
        if item.get("id") == "preco_arabica":
            peso = float(item.get("peso", 1.0))
            bloco = str(item.get("bloco", "")).strip()
            mult_bloco = -1.0 if bloco in ["2", "-1", "BLOCO 2", "B2"] else 1.0

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
                "score_ponderado": score * peso * mult_bloco,
                "frequencia": "Diária",
                "ultima_atualizacao": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "regra_de_sinal": RULE_TXT,
                "fonte": FONTE_TXT,
            })
            break
    else:
        raise RuntimeError("Não encontrei id='preco_arabica' em painel_snapshot.json")

    snapshot["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("OK: painel_snapshot.json atualizado (preco_arabica).")

if __name__ == "__main__":
    main()
