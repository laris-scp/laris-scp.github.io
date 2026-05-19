import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================
# CONFIG
# =========================
LOOKBACK_YEARS = 10  # usado só no "bootstrap" (quando não existe JSON anterior)
MM_LONG = 252
MM_SHORT = 50

OUT_PATH = Path("data/soja/series/usd_brl.json")

# Metadados (mantém o "contrato" do JSON)
META = {
    "id": "usd_brl",
    "name": "USD/BRL",
    "unit": "R$",
    "frequency": "Diária",
}

BCB_SERIE = 1  # SGS 1

# Timeouts (connect, read)
HTTP_TIMEOUT = (10, 90)

# Retry/backoff para instabilidade do BCB / rede do runner
RETRY_TOTAL = 5
RETRY_BACKOFF = 1.2  # 1.2s, 2.4s, 4.8s...

# Janela mínima de histórico para recalcular MM sem buscar "10 anos"
RECALC_BUFFER_DAYS = 420

# Para checar se há dado novo (requisição pequena)
PROBE_DAYS = 14


def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        status=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _bcb_url(start: str, end: str) -> str:
    return (
        f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{BCB_SERIE}/dados"
        f"?formato=json&dataInicial={start}&dataFinal={end}"
    )


def fetch_bcb_sgs(start: str, end: str, session: requests.Session) -> pd.DataFrame:
    url = _bcb_url(start, end)
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    r = session.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()

    df = pd.DataFrame(r.json())
    if df.empty:
        return df

    df["data"] = pd.to_datetime(df["data"], dayfirst=True, errors="coerce")
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce")
    df = df.dropna().sort_values("data").reset_index(drop=True)
    return df


def fetch_bcb_chunked(start_dt: datetime, end_dt: datetime, session: requests.Session) -> pd.DataFrame:
    """
    Opção B: quebra a coleta em blocos para reduzir payload e risco de timeout.
    Blocos de ~180 dias.
    """
    if end_dt < start_dt:
        return pd.DataFrame(columns=["data", "valor"])

    frames = []
    cur = start_dt
    while cur <= end_dt:
        nxt = min(cur + timedelta(days=180), end_dt)
        start = cur.strftime("%d/%m/%Y")
        end = nxt.strftime("%d/%m/%Y")
        part = fetch_bcb_sgs(start, end, session)
        if not part.empty:
            frames.append(part)
        cur = nxt + timedelta(days=1)

    if not frames:
        return pd.DataFrame(columns=["data", "valor"])

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["data"]).sort_values("data").reset_index(drop=True)
    return df


def _load_existing() -> tuple[pd.DataFrame, str | None]:
    if not OUT_PATH.exists():
        return pd.DataFrame(columns=["date", "close"]), None

    payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    pts = payload.get("series", [])
    if not pts:
        return pd.DataFrame(columns=["date", "close"]), None

    last_date_str = pts[-1].get("date")
    df = pd.DataFrame(pts)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return df[["date", "close"]], last_date_str


def main():
    session = _make_session()

    # --- 1) Estado atual ---
    df_existing, last_date_existing_str = _load_existing()

    # --- 2) Define end_dt como D-1 (não inclui o dia corrente) ---
    # D-1 em UTC evita pegar dados "no meio do dia"
    end_dt = datetime.utcnow() - timedelta(days=1)

    # --- 3) Probe pequeno: existe dado novo? ---
    probe_start_dt = end_dt - timedelta(days=PROBE_DAYS)

    df_probe = fetch_bcb_chunked(probe_start_dt, end_dt, session)
    if df_probe.empty:
        raise RuntimeError("BCB retornou vazio no probe (janela curta).")

    last_date_bcb = df_probe.iloc[-1]["data"].date().isoformat()

    if last_date_existing_str is not None and last_date_bcb <= last_date_existing_str:
        print(f"Sem dados novos. Última data no JSON: {last_date_existing_str} | Última data no BCB: {last_date_bcb}")
        return

    # --- 4) Coleta principal ---
    if last_date_existing_str is None:
        # Bootstrap
        start_dt = end_dt - timedelta(days=LOOKBACK_YEARS * 365 - 5)
        df_bcb = fetch_bcb_chunked(start_dt, end_dt, session)
        if df_bcb.empty:
            raise RuntimeError("BCB retornou vazio no bootstrap.")
        df_all = df_bcb.rename(columns={"data": "date", "valor": "close"})
    else:
        # Incremental (recalcula cauda)
        last_dt = datetime.fromisoformat(last_date_existing_str)
        start_dt = last_dt - timedelta(days=RECALC_BUFFER_DAYS)

        df_tail = fetch_bcb_chunked(start_dt, end_dt, session)
        if df_tail.empty:
            raise RuntimeError("BCB retornou vazio na atualização incremental.")
        df_tail = df_tail.rename(columns={"data": "date", "valor": "close"})

        df_prefix = df_existing[df_existing["date"] < pd.to_datetime(start_dt)].copy()

        df_all = pd.concat([df_prefix, df_tail[["date", "close"]]], ignore_index=True)
        df_all["date"] = pd.to_datetime(df_all["date"], errors="coerce")
        df_all["close"] = pd.to_numeric(df_all["close"], errors="coerce")
        df_all = df_all.dropna().drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    # --- 4.1) Garantia de histórico mínimo para calcular MM252 sem quebrar ---
    # Após rolling(MM_LONG) e dropna(), perdemos (MM_LONG-1) linhas.
    # Para ter pelo menos (MM_LONG + 60) linhas "válidas" no fim, precisamos:
    # len_bruto >= (MM_LONG - 1) + (MM_LONG + 60)
    min_raw = (MM_LONG - 1) + (MM_LONG + 60)

    if len(df_all) < min_raw:
        # Fallback seguro: refaz bootstrap (10 anos) para recompor histórico suficiente
        print(
            f"AVISO: histórico insuficiente para MM{MM_LONG}. "
            f"len(df_all)={len(df_all)} < {min_raw}. Fazendo bootstrap de {LOOKBACK_YEARS} anos."
        )
        start_dt = end_dt - timedelta(days=LOOKBACK_YEARS * 365 - 5)
        df_bcb = fetch_bcb_chunked(start_dt, end_dt, session)
        if df_bcb.empty:
            raise RuntimeError("BCB retornou vazio no bootstrap (fallback por histórico insuficiente).")
        df_all = df_bcb.rename(columns={"data": "date", "valor": "close"})
        df_all["date"] = pd.to_datetime(df_all["date"], errors="coerce")
        df_all["close"] = pd.to_numeric(df_all["close"], errors="coerce")
        df_all = df_all.dropna().drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)

    # --- 5) Médias móveis ---
    df_all["mm252"] = df_all["close"].rolling(MM_LONG).mean()
    df_all["mm50"] = df_all["close"].rolling(MM_SHORT).mean()

    # Mantém seu padrão: remove NaNs
    df_all = df_all.dropna().reset_index(drop=True)

    if len(df_all) < MM_LONG + 60:
        raise RuntimeError(f"Poucos dados após MM: {len(df_all)}")

    series = []
    for _, row in df_all.iterrows():
        series.append(
            {
                "date": row["date"].date().isoformat(),
                "close": float(row["close"]),
                "mm50": float(row["mm50"]),
                "mm252": float(row["mm252"]),
            }
        )

    payload = {**META, "series": series}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    print("OK: data/soja/series/usd_brl.json gerado.")
    print("Última data:", series[-1]["date"])
    print("Último close:", series[-1]["close"])


if __name__ == "__main__":
    main()
