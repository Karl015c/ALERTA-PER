import os
import csv
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

COMPANIES_FILE = "empresas.csv"   # columnas: Ticker,Empresa
STATE_FILE = "estado.json"
DIAS_PAUSA = 90
CRECIMIENTO_MINIMO = 3.0

# Peticiones en paralelo. 8 es un valor conservador y fiable.
# Si con 100-200 empresas Yahoo empieza a dar errores, BAJA este número;
# si va sobrado y quieres más velocidad, puedes subirlo poco a poco.
MAX_WORKERS = 8

THRESHOLDS = [
    (27, "🟢"),
    (25, "🟡"),
    (21, "🟠"),
    (19, "🔴"),
]

OTHER_INCOME_CANDIDATES = [
    "Other Income Expense",
    "Total Other Income Expense Net",
    "Other Non Operating Income Expenses",
    "Net Non Operating Interest Income Expense",
]

PER_MIN_VALIDO = 3
PER_MAX_VALIDO = 100
FACTOR_MIN = -2.0
FACTOR_MAX = 3.0


# ─── Lectura de la lista de empresas ───

def load_companies():
    companies = {}
    with open(COMPANIES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = (row.get("Ticker") or "").strip()
            name = (row.get("Empresa") or "").strip()
            if ticker and name:
                companies[ticker] = name
    return companies


# ─── Estado persistente (empresas en pausa) ───

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── Datos financieros ───

def get_price(ticker):
    t = yf.Ticker(ticker)
    hist = t.history(period="5d")
    return float(hist["Close"].iloc[-1])


def find_row(df, candidates):
    for name in candidates:
        if name in df.index:
            return df.loc[name]
    for idx in df.index:
        low = str(idx).lower()
        if "other" in low and ("income" in low or "expense" in low):
            return df.loc[idx]
    return None


def get_adjusted_eps_proxy(ticker):
    """
    EPS TTM 'limpio' partiendo del EPS diluido ya reportado por Yahoo para
    ese ticker exacto (correcto en su propia unidad/divisa/ratio ADS), y
    aplicándole la proporción de beneficio que no viene de
    'Other Income/Expense, net'. Evita mezclar unidades de fuentes
    distintas (el bug de Alibaba/Novo Nordisk).
    """
    t = yf.Ticker(ticker)
    q = t.quarterly_income_stmt
    if q is None or q.empty or "Diluted EPS" not in q.index:
        return None, None

    q = q.iloc[:, :4]
    eps_row = q.loc["Diluted EPS"]
    net_income_row = q.loc["Net Income"] if "Net Income" in q.index else None
    other_row = find_row(q, OTHER_INCOME_CANDIDATES)

    if net_income_row is None:
        return None, None

    eps_gaap_ttm = 0.0
    eps_proxy_ttm = 0.0
    quarters_usados = 0

    for col in q.columns:
        eps_q = eps_row.get(col)
        net_income_q = net_income_row.get(col)
        other_q = other_row.get(col) if other_row is not None else 0

        if eps_q is None or net_income_q is None or net_income_q == 0:
            continue

        factor = (net_income_q - other_q) / net_income_q
        if factor < FACTOR_MIN or factor > FACTOR_MAX:
            continue

        eps_gaap_ttm += eps_q
        eps_proxy_ttm += eps_q * factor
        quarters_usados += 1

    if quarters_usados < 3:
        return None, None

    return eps_proxy_ttm, eps_gaap_ttm


def get_cagr_3y(ticker):
    try:
        t = yf.Ticker(ticker)
        fin = t.financials
        eps_cagr = rev_cagr = None

        if "Diluted EPS" in fin.index:
            serie = fin.loc["Diluted EPS"].dropna()
            if len(serie) >= 4:
                end, start = serie.iloc[0], serie.iloc[3]
                if start > 0:
                    eps_cagr = ((end / start) ** (1 / 3) - 1) * 100

        if "Total Revenue" in fin.index:
            serie = fin.loc["Total Revenue"].dropna()
            if len(serie) >= 4:
                end, start = serie.iloc[0], serie.iloc[3]
                if start > 0:
                    rev_cagr = ((end / start) ** (1 / 3) - 1) * 100

        return eps_cagr, rev_cagr
    except Exception:
        return None, None


def tier_for_per(per):
    matched = None
    for th, emoji in THRESHOLDS:
        if per <= th:
            matched = (th, emoji)
    return matched


# ─── Análisis por empresa (se ejecuta en paralelo) ───

def analyze_ticker(ticker, name):
    try:
        price = get_price(ticker)
        eps_proxy, _ = get_adjusted_eps_proxy(ticker)
        eps_cagr, rev_cagr = get_cagr_3y(ticker)
    except Exception:
        return None

    if not eps_proxy or eps_proxy <= 0:
        return None

    per = price / eps_proxy
    if per < PER_MIN_VALIDO or per > PER_MAX_VALIDO:
        return None

    return {
        "ticker": ticker,
        "name": name,
        "per": per,
        "eps_cagr": eps_cagr,
        "rev_cagr": rev_cagr,
    }


# ─── Construcción del mensaje ───

def build_message():
    companies = load_companies()
    state = load_state()
    hoy = datetime.now().date()

    resultados = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze_ticker, t, n): t for t, n in companies.items()}
        for fut in as_completed(futures):
            ticker = futures[fut]
            r = fut.result()
            if r:
                resultados[ticker] = r

    tiers = {th: [] for th, _ in THRESHOLDS}
    pausadas_activas = []
    pausadas_nuevas = []

    for ticker, name in companies.items():
        r = resultados.get(ticker)
        if not r:
            continue

        matched = tier_for_per(r["per"])
        if not matched:
            if ticker in state:
                del state[ticker]
            continue

        th, emoji = matched
        eps_cagr, rev_cagr = r["eps_cagr"], r["rev_cagr"]

        if ticker in state:
            fecha_hasta = datetime.strptime(state[ticker], "%Y-%m-%d").date()
            if hoy <= fecha_hasta:
                dias_restantes = (fecha_hasta - hoy).days
                pausadas_activas.append(
                    f"😴 {name.upper()}: en pausa (crecimiento débil), "
                    f"quedan {dias_restantes} días."
                )
                continue
            else:
                del state[ticker]

        crecimiento_debil = (
            eps_cagr is not None and rev_cagr is not None
            and eps_cagr < CRECIMIENTO_MINIMO and rev_cagr < CRECIMIENTO_MINIMO
        )

        if crecimiento_debil:
            fecha_hasta = hoy + timedelta(days=DIAS_PAUSA)
            state[ticker] = fecha_hasta.strftime("%Y-%m-%d")
            pausadas_nuevas.append(
                f"😴 {name.upper()}: PER {r['per']:.1f}x pero crecimiento débil "
                f"({eps_cagr:.0f}% CAGR EPS, {rev_cagr:.0f}% CAGR ventas, ambos <{CRECIMIENTO_MINIMO:.0f}%). "
                f"No la revisamos hasta {fecha_hasta.strftime('%d/%m/%Y')}."
            )
            continue

        eps_txt = f"{eps_cagr:.0f}%" if eps_cagr is not None else "N/D"
        rev_txt = f"{rev_cagr:.0f}%" if rev_cagr is not None else "N/D"
        tiers[th].append(
            f"{emoji} {name.upper()} // PER {r['per']:.1f}x (proxy) // "
            f"{eps_txt} CAGR EPS // {rev_txt} CAGR VENTAS"
        )

    save_state(state)

    bloques = []
    for th, emoji in THRESHOLDS:
        if tiers[th]:
            bloques.append(f"\nPER a {th} o menos {emoji}:\n" + "\n".join(tiers[th]))

    if pausadas_nuevas:
        bloques.append("\n🆕 Nuevas en pausa:\n" + "\n".join(pausadas_nuevas))
    if pausadas_activas:
        bloques.append("\n⏸️ En pausa:\n" + "\n".join(pausadas_activas))

    if not bloques:
        return None

    cabecera = "📊 Alerta PER (proxy automático) — " + datetime.now().strftime("%d/%m/%Y")
    pie = "\n\n⚠️ PER calculado excluyendo 'Other Income/Expense, net'. Revisar manualmente antes de decidir."
    return cabecera + "\n" + "\n".join(bloques) + pie


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})


if __name__ == "__main__":
    mensaje = build_message()
    if mensaje:
        send_telegram(mensaje)
        print(mensaje)
    else:
        print("Ninguna empresa está hoy por debajo de los umbrales.")
