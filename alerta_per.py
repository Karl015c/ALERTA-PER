import os
import csv
import json
import time
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

# 5 trabajadores paralelos evita que Yahoo Finance bloquee las peticiones europeas
MAX_WORKERS = 5

THRESHOLDS = [
    (30, "🔵"),
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

REVENUE_CANDIDATES = [
    "Total Revenue",
    "Operating Revenue",
    "Total Operating Income",
    "Net Interest Income",
    "Net Income"
]

PER_MIN_VALIDO = 3
PER_MAX_VALIDO = 100
FACTOR_MIN = -2.0
FACTOR_MAX = 3.0


# ─── Lectura de la lista de empresas ───

def load_companies():
    companies = {}
    if not os.path.exists(COMPANIES_FILE):
        print(f"❌ Error: No se encuentra el archivo {COMPANIES_FILE}")
        return companies
        
    for enc in ["utf-8-sig", "latin-1", "utf-8"]:
        try:
            with open(COMPANIES_FILE, newline="", encoding=enc) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row_clean = {k.strip() if k else "": v for k, v in row.items()}
                    ticker = (row_clean.get("Ticker") or "").strip()
                    name = (row_clean.get("Empresa") or "").strip()
                    if ticker and name:
                        companies[ticker] = name
            if companies:
                break
        except Exception:
            continue
    return companies


# ─── Estado persistente ───

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── Datos financieros con contingencia anti-bloqueo ───

def get_price(t):
    try:
        if hasattr(t, "fast_info") and "lastPrice" in t.fast_info and t.fast_info["lastPrice"]:
            return float(t.fast_info["lastPrice"])
    except Exception:
        pass
    
    try:
        hist = t.history(period="5d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


def find_row(df, candidates):
    for name in candidates:
        if name in df.index:
            return df.loc[name]
    return None


def get_adjusted_eps_proxy(t):
    eps_ttm = None
    try:
        info = t.info or {}
        eps_ttm = info.get("trailingEps")
    except Exception:
        pass
    
    # Contingencia sumando trimestres si info no responde
    if not eps_ttm or eps_ttm <= 0:
        try:
            q = t.quarterly_income_stmt
            if q is not None and not q.empty:
                for key in ["Diluted EPS", "Basic EPS"]:
                    if key in q.index:
                        eps_ttm = float(q.loc[key].iloc[:4].sum())
                        break
        except Exception:
            pass

    if not eps_ttm or eps_ttm <= 0:
        return None, None

    try:
        q = t.quarterly_income_stmt
        if q is None or q.empty or "Net Income" not in q.index:
            return eps_ttm, eps_ttm

        q = q.iloc[:, :4]
        net_income_row = q.loc["Net Income"]
        other_row = find_row(q, OTHER_INCOME_CANDIDATES)

        factor_acumulado = 0.0
        quarters_usados = 0

        for col in q.columns:
            net_income_q = net_income_row.get(col)
            other_q = other_row.get(col) if other_row is not None else 0

            if net_income_q is None or net_income_q == 0:
                continue

            factor = (net_income_q - other_q) / net_income_q
            if FACTOR_MIN <= factor <= FACTOR_MAX:
                factor_acumulado += factor
                quarters_usados += 1

        if quarters_usados >= 3:
            factor_medio = factor_acumulado / quarters_usados
            eps_proxy_ttm = eps_ttm * factor_medio
            return eps_proxy_ttm, eps_ttm
    except Exception:
        pass

    return eps_ttm, eps_ttm


def get_cagr_3y(t):
    try:
        fin = t.financials
        eps_cagr = rev_cagr = None

        if fin is not None and not fin.empty:
            eps_row = None
            for key in ["Diluted EPS", "Basic EPS"]:
                if key in fin.index:
                    eps_row = fin.loc[key]
                    break

            if eps_row is not None:
                serie = eps_row.dropna()
                if len(serie) >= 4:
                    end, start = serie.iloc[0], serie.iloc[3]
                    if start > 0 and end > 0:
                        eps_cagr = ((end / start) ** (1 / 3) - 1) * 100

            rev_row = find_row(fin, REVENUE_CANDIDATES)
            if rev_row is not None:
                serie = rev_row.dropna()
                if len(serie) >= 4:
                    end, start = serie.iloc[0], serie.iloc[3]
                    if start > 0 and end > 0:
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


# ─── Análisis por empresa ───

def analyze_ticker(ticker, name):
    try:
        t = yf.Ticker(ticker)
        price = get_price(t)
        if price is None or price <= 0:
            return None

        eps_proxy, _ = get_adjusted_eps_proxy(t)
        if not eps_proxy or eps_proxy <= 0:
            return None

        eps_cagr, rev_cagr = get_cagr_3y(t)

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
    except Exception:
        return None


# ─── Construcción del mensaje ───

def build_message():
    companies = load_companies()
    if not companies:
        print("No se cargó ninguna empresa de empresas.csv")
        return None

    state = load_state()
    hoy = datetime.now().date()

    empresas_a_analizar = {}
    pausadas_activas = []

    # 1. Separar empresas en pausa activa para NO hacer peticiones en Yahoo
    for ticker, name in companies.items():
        if ticker in state:
            fecha_hasta = datetime.strptime(state[ticker], "%Y-%m-%d").date()
            if hoy <= fecha_hasta:
                dias_restantes = (fecha_hasta - hoy).days
                pausadas_activas.append(
                    f"• 😴 {name.upper()} ({ticker}): quedan {dias_restantes} días de pausa."
                )
                continue
            else:
                del state[ticker]
        
        empresas_a_analizar[ticker] = name

    # 2. Analizar empresas activas
    resultados = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze_ticker, t, n): t for t, n in empresas_a_analizar.items()}
        for fut in as_completed(futures):
            ticker = futures[fut]
            r = fut.result()
            if r:
                resultados[ticker] = r

    tiers = {th: [] for th, _ in THRESHOLDS}
    pausadas_nuevas = []

    for ticker, name in empresas_a_analizar.items():
        r = resultados.get(ticker)
        if not r:
            continue

        matched = tier_for_per(r["per"])
        if not matched:
            continue

        th, emoji = matched
        eps_cagr, rev_cagr = r["eps_cagr"], r["rev_cagr"]

        crecimiento_debil = (
            eps_cagr is not None and rev_cagr is not None
            and eps_cagr < CRECIMIENTO_MINIMO and rev_cagr < CRECIMIENTO_MINIMO
        )

        if crecimiento_debil:
            fecha_hasta = hoy + timedelta(days=DIAS_PAUSA)
            state[ticker] = fecha_hasta.strftime("%Y-%m-%d")
            pausadas_nuevas.append(
                f"• 😴 {name.upper()}: PER {r['per']:.1f}x pero crecimiento débil "
                f"({eps_cagr:.0f}% CAGR EPS, {rev_cagr:.0f}% CAGR ventas)."
            )
            continue

        eps_txt = f"{eps_cagr:.0f}%" if eps_cagr is not None else "N/D"
        rev_txt = f"{rev_cagr:.0f}%" if rev_cagr is not None else "N/D"
        
        tiers[th].append(
            f"• {emoji} {name.upper()}\n"
            f"   PER: {r['per']:.1f}x | EPS CAGR: {eps_txt} | Ventas CAGR: {rev_txt}"
        )

    save_state(state)

    bloques = []
    for th, emoji in THRESHOLDS:
        if tiers[th]:
            bloques.append(f"\n📌 PER a {th}x o menos {emoji}:\n" + "\n\n".join(tiers[th]))

    if pausadas_nuevas:
        bloques.append("\n🆕 Nuevas en pausa (silenciadas por 90 días):\n" + "\n".join(pausadas_nuevas))

    if pausadas_activas:
        bloques.append("\n⏸️ En pausa (silenciadas por 90 días):\n" + "\n".join(pausadas_activas))

    if not bloques:
        return None

    cabecera = "📊 Alerta PER (proxy automático) — " + datetime.now().strftime("%d/%m/%Y")
    pie = "\n\n⚠️ PER calculado excluyendo 'Other Income/Expense, net'. Revisar manualmente antes de decidir."
    return cabecera + "\n" + "\n".join(bloques) + pie


def send_telegram(text):
    # Telegram permite hasta 4096 caracteres por mensaje. Si se excede, se divide en varios envíos.
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    if len(text) <= 4000:
        response = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        if not response.ok:
            print(f"❌ Error enviando a Telegram ({response.status_code}): {response.text}")
        else:
            print("✅ Mensaje enviado con éxito a Telegram.")
    else:
        # Dividir por bloques en caso de listas muy largas
        partes = text.split("\n\n📌 ")
        for i, parte in enumerate(partes):
            contenido = parte if i == 0 else "📌 " + parte
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": contenido})
            time.sleep(1)


if __name__ == "__main__":
    mensaje = build_message()
    if mensaje:
        send_telegram(mensaje)
    else:
        print("Ninguna empresa está hoy por debajo de los umbrales.")
        send_telegram("ℹ️ Bot activo: Ninguna empresa cumple hoy con los criterios de PER configurados.")
