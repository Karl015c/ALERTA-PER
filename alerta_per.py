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
DIAS_PAUSA = 15                    # bajado de 90 a 15 (con L-X-V, 90 días de calendario
                                    # equivalen a ~38 ejecuciones sin revisar la empresa)
CRECIMIENTO_MINIMO = 3.0

MAX_WORKERS = 5

THRESHOLDS = [
    (34, "🔵"),
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
    "Net Income",
]

# Bajado de 150 a 60: un PER proxy por encima de esto es casi siempre
# ruido de datos (EPS TTM anormalmente bajo), no una valoración real.
PER_MIN_VALIDO = 2
PER_MAX_VALIDO = 60

FACTOR_MIN = -2.0
FACTOR_MAX = 3.0

# Si el PER proxy y el PER GAAP puro difieren menos de este % relativo,
# el ajuste de "Other Income/Expense" no ha corregido nada relevante,
# así que avisamos de que puede haber otro tipo de extraordinario
# (ej. cargos por compensación en acciones, reestructuración) que este
# proxy no detecta.
DIFERENCIA_MINIMA_AJUSTE = 0.03  # 3%


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


# ─── Datos financieros ───

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
    """
    Devuelve (eps_proxy_ttm, eps_gaap_ttm, ajuste_relevante).

    ajuste_relevante = True si el proxy corrigió el EPS en más de
    DIFERENCIA_MINIMA_AJUSTE respecto al GAAP puro. Si es False, el PER
    proxy mostrado es casi idéntico al GAAP, así que probablemente hay
    otro tipo de extraordinario (SBC, reestructuración) que este proxy
    concreto no detecta y hace falta revisión manual.
    """
    eps_ttm = None
    try:
        info = t.info or {}
        eps_ttm = info.get("trailingEps")
    except Exception:
        pass

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
        return None, None, None

    try:
        q = t.quarterly_income_stmt
        if q is None or q.empty or "Net Income" not in q.index:
            return eps_ttm, eps_ttm, False

        q = q.iloc[:, :4]
        net_income_row = q.loc["Net Income"]
        other_row = find_row(q, OTHER_INCOME_CANDIDATES)

        # ── Factor PONDERADO por el peso real de cada trimestre en el
        # beneficio TTM total, en vez de promediar 4 factores por igual.
        # Esto evita que un trimestre con beneficio casi nulo (pero un
        # "Other Income" grande en términos absolutos) distorsione el
        # promedio de forma desproporcionada — el bug de Novo Nordisk.
        net_income_ttm_bruto = 0.0
        other_ttm_bruto = 0.0
        quarters_usados = 0

        for col in q.columns:
            net_income_q = net_income_row.get(col)
            other_q = other_row.get(col) if other_row is not None else 0

            if net_income_q is None:
                continue

            # descarta trimestres con beneficio casi nulo (factor indefinido/inestable)
            factor_q = (net_income_q - other_q) / net_income_q if net_income_q != 0 else None
            if factor_q is None or factor_q < FACTOR_MIN or factor_q > FACTOR_MAX:
                continue

            net_income_ttm_bruto += net_income_q
            other_ttm_bruto += other_q
            quarters_usados += 1

        if quarters_usados >= 3 and net_income_ttm_bruto != 0:
            factor_ponderado = (net_income_ttm_bruto - other_ttm_bruto) / net_income_ttm_bruto
            factor_ponderado = max(FACTOR_MIN, min(FACTOR_MAX, factor_ponderado))
            eps_proxy_ttm = eps_ttm * factor_ponderado

            diferencia_relativa = abs(eps_proxy_ttm - eps_ttm) / eps_ttm
            ajuste_relevante = diferencia_relativa >= DIFERENCIA_MINIMA_AJUSTE

            return eps_proxy_ttm, eps_ttm, ajuste_relevante
    except Exception:
        pass

    return eps_ttm, eps_ttm, False


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
            print(f"⚠️ {ticker}: No se pudo obtener precio.")
            return None

        eps_proxy, eps_gaap, ajuste_relevante = get_adjusted_eps_proxy(t)
        if not eps_proxy or eps_proxy <= 0:
            print(f"⚠️ {ticker}: No se pudo obtener EPS válido.")
            return None

        eps_cagr, rev_cagr = get_cagr_3y(t)

        per = price / eps_proxy
        if per < PER_MIN_VALIDO or per > PER_MAX_VALIDO:
            print(f"⚠️ {ticker}: PER fuera de rango ({per:.1f}x), descartado.")
            return None

        return {
            "ticker": ticker,
            "name": name,
            "per": per,
            "per_gaap": price / eps_gaap if eps_gaap else None,
            "eps_cagr": eps_cagr,
            "rev_cagr": rev_cagr,
            "ajuste_relevante": ajuste_relevante,
        }
    except Exception as e:
        print(f"❌ Error analizando {ticker}: {e}")
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

    for ticker, name in companies.items():
        if ticker in state:
            fecha_hasta = datetime.strptime(state[ticker], "%Y-%m-%d").date()
            if hoy <= fecha_hasta:
                dias_restantes = (fecha_hasta - hoy).days
                motivo = state[ticker + "_motivo"] if (ticker + "_motivo") in state else ""
                linea = f"• 😴 {name.upper()} ({ticker}): quedan {dias_restantes} días."
                if motivo:
                    linea += f" {motivo}"
                pausadas_activas.append(linea)
                continue
            else:
                del state[ticker]
                state.pop(ticker + "_motivo", None)

        empresas_a_analizar[ticker] = name

    resultados = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze_ticker, t, n): t for t, n in empresas_a_analizar.items()}
        for fut in as_completed(futures):
            ticker = futures[fut]
            r = fut.result()
            if r:
                resultados[ticker] = r

    tiers = {th: [] for th, _ in THRESHOLDS}
    por_encima_30 = []
    pausadas_nuevas = []
    sin_datos = []

    for ticker, name in empresas_a_analizar.items():
        r = resultados.get(ticker)
        if not r:
            sin_datos.append(f"• ❓ {name.upper()} ({ticker})")
            continue

        eps_cagr, rev_cagr = r["eps_cagr"], r["rev_cagr"]
        eps_txt = f"{eps_cagr:.0f}%" if eps_cagr is not None else "N/D"
        rev_txt = f"{rev_cagr:.0f}%" if rev_cagr is not None else "N/D"

        # Aviso si el ajuste de "Other Income/Expense" no corrigió nada
        # relevante: puede haber otro tipo de extraordinario que este
        # proxy no detecta (SBC, reestructuración, etc.)
        aviso = "" if r["ajuste_relevante"] else " ⚠️ revisar a mano"

        matched = tier_for_per(r["per"])

        if not matched:
            por_encima_30.append(
                f"• ⚪ {name.upper()}{aviso}\n"
                f"   PER: {r['per']:.1f}x | EPS CAGR: {eps_txt} | Ventas CAGR: {rev_txt}"
            )
            continue

        th, emoji = matched

        crecimiento_debil = (
            eps_cagr is not None and rev_cagr is not None
            and eps_cagr < CRECIMIENTO_MINIMO and rev_cagr < CRECIMIENTO_MINIMO
        )

        if crecimiento_debil:
            fecha_hasta = hoy + timedelta(days=DIAS_PAUSA)
            state[ticker] = fecha_hasta.strftime("%Y-%m-%d")
            motivo = f"(PER {r['per']:.1f}x, {eps_txt} CAGR EPS, {rev_txt} CAGR ventas)"
            state[ticker + "_motivo"] = motivo
            pausadas_nuevas.append(
                f"• 😴 {name.upper()}: crecimiento débil {motivo}. Pausa {DIAS_PAUSA} días."
            )
            continue

        tiers[th].append(
            f"• {emoji} {name.upper()}{aviso}\n"
            f"   PER: {r['per']:.1f}x | EPS CAGR: {eps_txt} | Ventas CAGR: {rev_txt}"
        )

    save_state(state)

    bloques = []
    for th, emoji in THRESHOLDS:
        if tiers[th]:
            bloques.append(f"\n📌 PER a {th}x o menos {emoji}:\n" + "\n\n".join(tiers[th]))

    if por_encima_30:
        bloques.append(f"\n📈 PER por encima de {THRESHOLDS[0][0]}x:\n" + "\n\n".join(por_encima_30))

    if pausadas_nuevas:
        bloques.append("\n🆕 Nuevas en pausa:\n" + "\n".join(pausadas_nuevas))

    if pausadas_activas:
        bloques.append("\n⏸️ En pausa:\n" + "\n".join(pausadas_activas))

    if sin_datos:
        bloques.append("\n⚠️ Sin datos / No analizadas:\n" + "\n".join(sin_datos))

    if not bloques:
        return None

    cabecera = "📊 Alerta PER (proxy automático) — " + datetime.now().strftime("%d/%m/%Y")
    pie = (
        "\n\n⚠️ PER calculado excluyendo 'Other Income/Expense, net'. "
        "Las marcadas '⚠️ revisar a mano' no mostraron un ajuste relevante: "
        "puede haber otro tipo de extraordinario (SBC, reestructuración, etc.) "
        "que este proxy no detecta."
    )
    return cabecera + "\n" + "\n".join(bloques) + pie


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    if len(text) <= 4000:
        response = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        if not response.ok:
            print(f"❌ Error enviando a Telegram ({response.status_code}): {response.text}")
        else:
            print("✅ Mensaje enviado con éxito a Telegram.")
    else:
        partes = text.split("\n\n")
        msg_actual = ""
        for parte in partes:
            if len(msg_actual) + len(parte) + 2 < 4000:
                msg_actual += parte + "\n\n"
            else:
                requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg_actual})
                time.sleep(1)
                msg_actual = parte + "\n\n"
        if msg_actual.strip():
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg_actual})


if __name__ == "__main__":
    mensaje = build_message()
    if mensaje:
        send_telegram(mensaje)
    else:
        print("Ninguna empresa está hoy para mostrar.")
