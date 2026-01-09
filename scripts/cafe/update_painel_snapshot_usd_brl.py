import json
from datetime import datetime
from pathlib import Path
import pandas as pd

SERIES_PATH = Path("data/cafe/series/usd_brl.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

MM_LONG = 252
MM_SHORT = 50

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
    t = t.strip().upper()
    if t == "QUEDA":
        return -1.0
    if t == "ALTA":
        return 1.0
    return 0.0

def momento_to_value(m: str) -> float:
    m = " ".join(m.strip().upper().replace("-", " ").split())
    if m == "QUEDA ACELERANDO":
        return -1.0
    if m == "QUEDA DESACELERANDO":
        return -0.5
    if m == "ALTA DESACELERANDO":
        return 0.5
    if m == "ALTA ACELERANDO":
        return 1.0
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

FONTE_TXT = "BCB SGS (série 1) – USD/BRL"

def main():
    # --- Ler série já automatizada ---
    series_payload = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    pts = series_payload["series"]

    df = pd.DataFrame(pts)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna().sort_values("date").reset_index(drop=True)

    # Garantir colunas
    for c in ["close", "mm50", "mm252"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().reset_index(drop=True)

    if len(df) < MM_LONG + 60:
        raise RuntimeError(f"Poucos dados após MM: {len(df)}")

    # Últimos valores
    ult = float(df.iloc[-1]["close"])
    last_mm252 = float(df.iloc[-1]["mm252"])
    last_mm50 = float(df.iloc[-1]["mm50"])

    # Nível: percentil do dist vs MM252
    df["dist_mm252"] = df["close"] - df["mm252"]
    percentil = percentile_rank(df["dist_mm252"], ult - last_mm252)
    nivel_txt, valor_nivel = pct_to_level_and_value(percentil)

    # Tendência: close vs MM50/MM252
    if ult > last_mm50 and ult > last_mm252:
        tendencia = "ALTA"
    elif ult < last_mm50 and ult < last_mm252:
        tendencia = "QUEDA"
    else:
        tendencia = "LATERAL"
    valor_tendencia = tendencia_to_value(tendencia)

    # Momento: 3 pontos mensais da MM50
    df["mes"] = df["date"].dt.to_period("M")
    mm50_monthly = df.groupby("mes").last().reset_index().sort_values("mes")

    momento = "NEUTRO"
    if len(mm50_monthly) >= 3:
        m2 = float(mm50_monthly.iloc[-3]["mm50"])
        m1 = float(mm50_monthly.iloc[-2]["mm50"])
        m0 = float(mm50_monthly.iloc[-1]["mm50"])

        d1 = m1 - m2
        d2 = m0 - m1

        if tendencia == "ALTA":
            momento = "ALTA ACELERANDO" if abs(d2) > abs(d1) else "ALTA DESACELERANDO"
        elif tendencia == "QUEDA":
            momento = "QUEDA ACELERANDO" if abs(d2) > abs(d1) else "QUEDA DESACELERANDO"
        else:
            momento = "NEUTRO"

    valor_momento = momento_to_value(momento)

    # Score (sem peso)
    score = float(valor_nivel + valor_tendencia + valor_momento)

        # --- Carregar snapshot e atualizar apenas o id=usd_brl ---
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    if not (isinstance(snapshot, dict) and "rows" in snapshot and isinstance(snapshot["rows"], list)):
        raise RuntimeError("Formato inesperado em painel_snapshot.json: esperado dict com chave 'rows' (lista).")

    rows = snapshot["rows"]

    found = False
    for item in rows:
        if item.get("id") == "usd_brl":
            peso = float(item.get("peso", 1.0))  # preserva o peso atual
            item.update({
                "ultimo_valor": ult,
                "percentil": float(round(percentil, 4)),
                "nivel": nivel_txt,
                "valor_nivel": float(valor_nivel),
                "tendencia": tendencia,
                "valor_tendencia": float(valor_tendencia),
                "momento": momento,
                "valor_momento": float(valor_momento),
                "score": float(score),
                "score_ponderado": float(score * peso),
                "frequencia": "Diária",
                "ultima_atualizacao": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "regra_de_sinal": RULE_TXT,
                "fonte": FONTE_TXT,
            })
            found = True
            break

    if not found:
        raise RuntimeError("Não encontrei id='usd_brl' em painel_snapshot.json (rows).")

    # Atualiza timestamp geral do snapshot
    snapshot["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snapshot["rows"] = rows

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )


    print("OK: painel_snapshot.json atualizado (usd_brl).")
    print("Ult:", ult, "| Nivel:", nivel_txt, valor_nivel, "| Tend:", tendencia, valor_tendencia, "| Mom:", momento, valor_momento)

if __name__ == "__main__":
    main()
