import json
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

SERIES_PATH = Path("data/cafe/series/clima_soil.json")
SNAPSHOT_PATH = Path("data/cafe/painel_snapshot.json")

# Conforme seu código original: EPS ~ 1e-12 (praticamente zero)
EPS = 1e-12
AREA = [-15, -55, -25, -40]

def map_percentil_to_nivel(p: float):
    if pd.isna(p):
        return ("NEUTRO", 0.0)
    if p < 0.20: return ("MUITO BAIXO", -1.0)
    if p < 0.40: return ("BAIXO", -0.5)
    if p < 0.60: return ("NEUTRO", 0.0)
    if p < 0.80: return ("ALTO", 0.5)
    return ("MUITO ALTO", 1.0)

def tendencia_3p(d1: float, d2: float):
    if (d1 > EPS) and (d2 > EPS):
        return ("ALTA", 1.0)
    if (d1 < -EPS) and (d2 < -EPS):
        return ("QUEDA", -1.0)
    if (abs(d1) <= EPS) and (abs(d2) <= EPS):
        return ("LATERAL", 0.0)
    return ("INDEFINIDA", 0.0)

def momento_from_trend(tendencia: str, d1: float, d2: float):
    # Mesmo comportamento do seu Colab: acelera/desacelera via comparação de d2 vs d1
    if tendencia == "ALTA":
        if d2 > d1 + EPS:
            return ("ALTA ACELERANDO", 1.0)
        if d2 < d1 - EPS:
            return ("ALTA DESACELERANDO", 0.5)
        return ("NEUTRO", 0.0)

    if tendencia == "QUEDA":
        if d2 < d1 - EPS:
            return ("QUEDA ACELERANDO", -1.0)
        if d2 > d1 + EPS:
            return ("QUEDA DESACELERANDO", -0.5)
        return ("NEUTRO", 0.0)

    return ("NEUTRO", 0.0)

def main():
    # 1) Ler série
    s = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
    df = pd.DataFrame(s.get("series", []))

    if df.empty or "date" not in df.columns or "close" not in df.columns:
        raise RuntimeError("clima_soil.json inválido: 'series' vazia ou faltam campos date/close.")

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

    if len(df) < 3:
        raise RuntimeError("Histórico insuficiente para clima_soil (mínimo 3 pontos).")

    # 2) Variáveis conforme Colab
    # swvl3 (umidade): close
    vals_wet = df["close"].astype(float).values
    # dryness = -swvl3 (seca): sinal econômico (seca bullish)
    vals = (-df["close"].astype(float)).values

    ultimo_valor_raw = float(vals_wet[-1])
    ultimo_valor_sig = float(vals[-1])
    ultimo_date = df["date"].iloc[-1].strftime("%Y-%m-%d")

    # percentil conforme seu Colab: mean(arr < ultimo) (observação: era "<", não "<=")
    arr = np.array(vals, dtype=float)
    percentil = float((arr < ultimo_valor_sig).mean())

    nivel_cat, val_nivel = map_percentil_to_nivel(percentil)

    v1, v2, v3 = float(vals[-3]), float(vals[-2]), float(vals[-1])
    d1 = v2 - v1
    d2 = v3 - v2

    tendencia, val_tend = tendencia_3p(d1, d2)
    momento, val_mom = momento_from_trend(tendencia, d1, d2)

    # 3) Ler snapshot e achar linha clima_soil
    snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    if "rows" not in snap or not isinstance(snap["rows"], list):
        raise RuntimeError("painel_snapshot.json inválido: não encontrei 'rows' como lista.")

    row = None
    for r in snap["rows"]:
        if r.get("id") == "clima_soil":
            row = r
            break
    if row is None:
        raise RuntimeError("Não encontrei id='clima_soil' em painel_snapshot.json.")

    bloco = int(row.get("bloco", 1))
    # conforme seu padrão geral: bloco 2 inverte sinal
    mult_bloco = 1.0 if bloco == 1 else (-1.0 if bloco == 2 else 1.0)

    peso = float(row.get("peso", 1.0))

    score = (float(val_nivel) + float(val_tend) + float(val_mom)) * float(mult_bloco)
    score_ponderado = float(score) * float(peso)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rule_txt = (
        "Score climático baseado na umidade do solo (ERA5-Land, camada 28–100cm). "
        "Menor umidade (seca) indica maior risco produtivo e tende a ser bullish para o preço. "
        "Nível via percentil histórico; tendência e momento via últimos 3 meses."
    )

    fonte_txt = (
        "ERA5-Land (ECMWF/Copernicus) — swvl3 (Volumetric soil water layer 3), "
        f"média regional Brasil cafeeiro (bbox {AREA}). Último mês={ultimo_date}."
    )

    # 4) Atualiza somente os campos do clima
    row.update({
        "ultimo_valor": ultimo_valor_raw,
        "percentil": round(percentil, 4),
        "nivel": nivel_cat,
        "valor_nivel": float(val_nivel),
        "tendencia": tendencia,
        "valor_tendencia": float(val_tend),
        "momento": momento,
        "valor_momento": float(val_mom),
        "score": float(score),
        "score_ponderado": float(score_ponderado),
        "frequencia": "Mensal",
        "ultima_atualizacao": now_str,
        "regra_de_sinal": rule_txt,
        "fonte": fonte_txt,
    })

    # opcional: atualiza metadado do snapshot
    snap["updated_at"] = now_str

    SNAPSHOT_PATH.write_text(
        json.dumps(snap, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )

    print("OK: painel_snapshot.json atualizado (clima_soil).")
    print("DEBUG:", "ultimo=", ultimo_valor_raw, "| percentil(seca)=", round(percentil, 4),
          "| tendencia(seca)=", tendencia, "| momento=", momento)
    print("DEBUG:", "score=", score, "| peso=", peso, "| score_ponderado=", score_ponderado)

if __name__ == "__main__":
    main()
