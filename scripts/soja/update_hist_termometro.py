#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # repo root
SNAPSHOT_PATH = ROOT / "data" / "soja" / "painel_snapshot.json"
HIST_PATH = ROOT / "data" / "soja" / "hist_termometro.json"

def die(msg: str):
    raise RuntimeError(msg)

def load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

def save_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def parse_date_yyyy_mm_dd(s: str) -> str:
    """
    Aceita:
      - "YYYY-MM-DD ..."
      - "YYYY-MM-DD"
      - "DD/MM/YYYY"
    Retorna sempre "YYYY-MM-DD".
    """
    s = (s or "").strip()
    if not s:
        die("Data vazia ao tentar gerar histórico do termômetro.")

    # snapshot updated_at: "YYYY-MM-DD HH:MM:SS"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]

    # planilha: "DD/MM/YYYY"
    if len(s) == 10 and s[2] == "/" and s[5] == "/":
        dt = datetime.strptime(s, "%d/%m/%Y")
        return dt.strftime("%Y-%m-%d")

    # fallback
    try:
        dt = datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        die(f"Formato de data não reconhecido: {s}")

def ensure_official_format(obj):
    """
    Formato oficial esperado pelo site:
    {
      "meta": {...},
      "series": [{"date":"YYYY-MM-DD","value":float}, ...]
    }
    Aceita também legado com chave 'data'.
    """
    if isinstance(obj, dict):
        if "meta" in obj and "series" in obj and isinstance(obj["series"], list):
            return obj, True
        if "meta" in obj and "data" in obj and isinstance(obj["data"], list):
            # legado -> converte para series
            return {"meta": obj.get("meta", {}), "series": obj["data"]}, False

    # se vier como lista de pontos
    if isinstance(obj, list):
        series = []
        for p in obj:
            if isinstance(p, dict) and "date" in p and "value" in p:
                series.append({"date": str(p["date"]), "value": float(p["value"])})
        return {"meta": {}, "series": series}, False

    # formato desconhecido -> inicializa vazio no formato oficial
    return {"meta": {}, "series": []}, False


def main():
    snap = load_json(SNAPSHOT_PATH)
    if not isinstance(snap, dict):
        die("painel_snapshot.json inválido ou ausente.")

    if "thermometers" not in snap or not isinstance(snap["thermometers"], dict):
        die("painel_snapshot.json esperado com chave 'thermometers' (dict).")

    if "geral" not in snap["thermometers"]:
        die("painel_snapshot.json esperado com thermometers.geral.")

    updated_at = snap.get("updated_at")
    if not updated_at:
        die("painel_snapshot.json sem 'updated_at'.")

    date = parse_date_yyyy_mm_dd(updated_at)
    value = float(snap["thermometers"]["geral"])

    # Carrega histórico
    hist_raw = load_json(HIST_PATH)
    hist_obj, is_official = ensure_official_format(hist_raw) if hist_raw is not None else ({"meta": {}, "series": []}, True)

    meta_in = hist_obj.get("meta") if isinstance(hist_obj, dict) else {}
    series = hist_obj.get("series") if isinstance(hist_obj, dict) else None
    if not isinstance(series, list):
        die("hist_termometro.json esperado com chave 'series' (lista).")


    # Normaliza e filtra pontos válidos
    cleaned = []
    for p in series:
        if not isinstance(p, dict):
            continue
        d = p.get("date")
        v = p.get("value")
        if not d:
            continue
        try:
            d = parse_date_yyyy_mm_dd(d)
        except Exception:
            continue
        try:
            v = float(v)
        except Exception:
            continue
        cleaned.append({"date": d, "value": v})

    # UPSERT do dia:
    # - se já existe a data, atualiza o value
    # - se não existe, adiciona
    by_date = {p["date"]: p["value"] for p in cleaned}
    existed = date in by_date
    by_date[date] = value

    series_out = [{"date": d, "value": by_date[d]} for d in sorted(by_date.keys())]

    if existed:
        print(f"Atualizado ponto existente: date={date} | value={value}")
    else:
        print(f"Adicionado ponto novo: date={date} | value={value}")

    # Meta final
    meta_out = dict(meta_in) if isinstance(meta_in, dict) else {}
    meta_out.setdefault("id", "hist_termometro")
    meta_out.setdefault("title", "Histórico do Termômetro Geral")
    meta_out["source"] = "painel_snapshot.json (thermometers.geral)"
    meta_out.setdefault("frequency", "Diária")
    meta_out.setdefault("value_name", "TERMOMETRO GERAL")
    meta_out["updated_at"] = updated_at

    out = {"meta": meta_out, "series": series_out}
    save_json(HIST_PATH, out)

    print("OK: hist_termometro.json atualizado (upsert por data).")
    print(f"Pontos: {len(series_out)} | Último: {series_out[-1]}")

if __name__ == "__main__":
    main()
