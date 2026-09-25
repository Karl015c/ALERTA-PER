import os
import csv
import json
import math
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

COMPANIES_FILE = "empresas.csv"   # columnas: Ticker,Empresa
STATE_FILE = "estado.json"
DIAS_PAUSA = 15
CRECIMIENTO_MINIMO = 3.0

MAX_WORKERS = 5

# ── Tickers para los que queremos ver el desglose completo en el log
# de GitHub Actions (precio, EPS GAAP, EPS proxy, factor aplicado).
# Añade o quita tickers aquí para diagnosticar cualquier caso raro.
DEBUG_TICKERS = {"AENA.MC", "LSEG.L", "AMZN"}

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

PER_MIN_VALIDO = 2
PER_MAX_VALIDO = 60

FACTOR_MIN = -2.0
FACTOR_MAX = 3.0

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


def get_adjusted_eps_proxy(t, ticker=None):
    debug = ticker in DEBUG_TICKERS

    eps_ttm = None
    fuente_eps = None
    try:
        info = t.info or {}
        eps_ttm = info.get("trailingEps")
        if eps_ttm:
            fuente_eps = "trailingEps (info)"
    except Exception:
        pass

    if not eps_ttm or eps_ttm <= 0:
        try:
            q = t.quarterly_income_stmt
            if q is not None and not q.empty:
                for key in ["Diluted EPS", "Basic EPS"]:
                    if key in q.index:
                        eps_ttm = float(q.loc[key].iloc[:4].sum())
                        fuente_eps = f"suma 4T de '{key}'"
                        break
        except Exception:
            pass

    if debug:
        print(f"🔍 [{ticker}] EPS TTM base = {eps_ttm} (fuente: {fuente_eps})")

    if not eps_ttm or eps_ttm <= 0:
        if debug:
            print(f"🔍 [{ticker}] Sin EPS válido, se descarta aquí.")
        return None, None, None

    try:
        q = t.quarterly_income_stmt
        if q is None or q.empty or "Net Income" not in q.index:
            if debug:
                print(f"🔍 [{ticker}] Sin 'Net Income' en quarterly_income_stmt, se usa EPS sin ajustar.")
            return eps_ttm, eps_ttm, False

        q = q.iloc[:, :4]
        net_income_row = q.loc["Net Income"]
        other_row = find_row(q, OTHER_INCOME_CANDIDATES)

        if debug:
            print(f"🔍 [{ticker}] Columnas (trimestres) usadas: {list(q.columns)}")
            print(f"🔍 [{ticker}] Net Income por trimestre: {net_income_row.to_dict()}")
            if other_row is not None:
                print(f"🔍 [{ticker}] Other Income/Expense por trimestre: {other_row.to_dict()}")
            else:
                print(f"🔍 [{ticker}] No se encontró fila de 'Other Income/Expense' (factor = 1 para todos).")

        net_income_ttm_bruto = 0.0
        other_ttm_bruto = 0.0
        quarters_usados = 0

        for col in q.columns:
            net_income_q = net_income_row.get(col)
            other_q = other_row.get(col) if other_row is not None else 0

            # Si Yahoo no tiene dato de "Other Income" para este trimestre
            # concreto (nan), lo tratamos como 0 (sin extraordinario ese
            # trimestre) en vez de dejar que contamine toda la suma TTM
            # con nan (nan + cualquier_cosa = nan, siempre).
            if other_q is None or (isinstance(other_q, float) and math.isnan(other_q)):
                other_q = 0

            if net_income_q is None or (isinstance(net_income_q, float) and math.isnan(net_income_q)):
                continue

            factor_q = (net_income_q - other_q) / net_income_q if net_income_q != 0 else None
            if factor_q is None or math.isnan(factor_q) or factor_q < FACTOR_MIN or factor_q > FACTOR_MAX:
                if debug:
                    print(f"🔍 [{ticker}] Trimestre {col} descartado (factor={factor_q}).")
                continue

            net_income_ttm_bruto += net_income_q
            other_ttm_bruto += other_q
            quarters_usados += 1

        if debug:
            print(f"🔍 [{ticker}] Trimestres usados: {quarters_usados} | "
                  f"Net Income TTM bruto: {net_income_ttm_bruto} | "
                  f"Other Income TTM bruto: {other_ttm_bruto}")

        if quarters_usados >= 3 and net_income_ttm_bruto != 0:
            factor_ponderado = (net_income_ttm_bruto - other_ttm_bruto) / net_income_ttm_bruto

            # Salvaguarda extra: si por lo que sea el resultado es nan
            # o infinito, NO lo aceptamos como si fuera un valor válido
            # (antes esto colaba silenciosamente como si fuera el techo
            # de FACTOR_MAX = 3.0, que fue exactamente el bug de Aena).
            if math.isnan(factor_ponderado) or math.isinf(factor_ponderado):
                if debug:
                    print(f"🔍 [{ticker}] Factor final inválido ({factor_ponderado}), "
                          f"se usa EPS sin ajustar en su lugar.")
                return eps_ttm, eps_ttm, False

            factor_sin_topar = factor_ponderado
            factor_ponderado = max(FACTOR_MIN, min(FACTOR_MAX, factor_ponderado))
            eps_proxy_ttm = eps_ttm * factor_ponderado

            diferencia_relativa = abs(eps_proxy_ttm - eps_ttm) / eps_ttm
            ajuste_relevante = diferencia_relativa >= DIFERENCIA_MINIMA_AJUSTE

            if debug:
                print(f"🔍 [{ticker}] Factor sin topar: {factor_sin_topar:.3f} | "
                      f"Factor aplicado (topado): {factor_ponderado:.3f}")
                print(f"🔍 [{ticker}] EPS proxy final: {eps_proxy_ttm:.4f} "
                      f"(EPS GAAP era {eps_ttm:.4f})")

            return eps_proxy_ttm, eps_ttm, ajuste_relevante
        else:
            if debug:
                print(f"🔍 [{ticker}] Menos de 3 trimestres válidos o Net Income TTM = 0, "
                      f"se usa EPS sin ajustar.")
    except Exception as e:
        if debug:
            print(f"🔍 [{ticker}] Excepción durante el cálculo del proxy: {e}")

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


def abreviar_nombre(name):
    palabras_a_abreviar = {
        "Applied": "Apl.",
        "Materials": "Mat.",
        "International": "Intl.",
        "Corporation": "Corp.",
        "Company": "Co.",
        "Holdings": "Hold.",
        "Electric": "Elec.",
        "Technologies": "Tech.",
        "Solutions": "Sol.",
    }
    palabras = name.split()
    abreviado = [palabras_a_abreviar.get(p, p) for p in palabras]
    return " ".join(abreviado)


# ─── Análisis por empresa ───

def analyze_ticker(ticker, name):
    try:
        t = yf.Ticker(ticker)
        price = get_price(t)
        if price is None or price <= 0:
            print(f"⚠️ {ticker}: No se pudo obtener precio.")
            return None

        if ticker in DEBUG_TICKERS:
            print(f"🔍 [{ticker}] Precio usado: {price}")

        eps_proxy, eps_gaap, ajuste_relevante = get_adjusted_eps_proxy(t, ticker=ticker)
        if not eps_proxy or eps_proxy <= 0:
            print(f"⚠️ {ticker}: No se pudo obtener EPS válido.")
            return None

        eps_cagr, rev_cagr = get_cagr_3y(t)

        per = price / eps_proxy
        if ticker in DEBUG_TICKERS:
            print(f"🔍 [{ticker}] PER final calculado: {per:.2f}x")

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
                motivo = state.get(ticker + "_motivo", "")
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
    por_encima_umbral = []
    pausadas_nuevas = []
    sin_datos = []

    umbral_maximo = THRESHOLDS[0][0]

    for ticker, name in empresas_a_analizar.items():
        r = resultados.get(ticker)
        if not r:
            sin_datos.append(f"• ❓ {name.upper()} ({ticker})")
            continue

        eps_cagr, rev_cagr = r["eps_cagr"], r["rev_cagr"]
        eps_txt = f"{eps_cagr:.0f}%" if eps_cagr is not None else "N/D"
        rev_txt = f"{rev_cagr:.0f}%" if rev_cagr is not None else "N/D"

        aviso = "" if r["ajuste_relevante"] else " *"

        matched = tier_for_per(r["per"])

        if not matched:
            por_encima_umbral.append({
                "linea": f"• {ticker}{aviso} — Ventas: {rev_txt}",
                "rev_cagr": rev_cagr if rev_cagr is not None else float("-inf"),
            })
            continue

        th, emoji = matched

        crecimiento_debil = (
            eps_cagr is not None and rev_cagr is not None
            and eps_cagr < CRECIMIENTO_MINIMO and rev_cagr < CRECIMIENTO_MINIMO
        )

        if crecimiento_debil:
            fecha_hasta = hoy + timedelta(days=DIAS_PAUSA)
            state[ticker] = fecha_hasta.strftime("%Y-%m-%d")
            motivo = f"(PER {r['per']:.1f}x, {eps_txt} EPS, {rev_txt} ventas)"
            state[ticker + "_motivo"] = motivo
            pausadas_nuevas.append(
                f"• 😴 {name.upper()}: crecimiento débil {motivo}. Pausa {DIAS_PAUSA} días."
            )
            continue

        tiers[th].append({
            "linea": f"• {emoji} {name.upper()}{aviso}\n"
                     f"   PER: {r['per']:.1f}x | EPS: {eps_txt} | Ventas: {rev_txt}",
            "rev_cagr": rev_cagr if rev_cagr is not None else float("-inf"),
        })

    save_state(state)

    for th, _ in THRESHOLDS:
        tiers[th].sort(key=lambda x: x["rev_cagr"], reverse=True)
    por_encima_umbral.sort(key=lambda x: x["rev_cagr"], reverse=True)

    bloques = []
    for th, emoji in THRESHOLDS:
        if tiers[th]:
            lineas = [item["linea"] for item in tiers[th]]
            bloques.append(f"\n📌 PER {th}x o menos {emoji}:\n" + "\n\n".join(lineas))

    if por_encima_umbral:
        lineas = [item["linea"] for item in por_encima_umbral]
        bloques.append(f"\n📈 PER +{umbral_maximo}x:\n" + "\n".join(lineas))

    if pausadas_nuevas:
        bloques.append("\n🆕 Nuevas en pausa:\n" + "\n".join(pausadas_nuevas))

    if pausadas_activas:
        bloques.append("\n⏸️ En pausa:\n" + "\n".join(pausadas_activas))

    if sin_datos:
        bloques.append("\n⚠️ Sin datos:\n" + "\n".join(sin_datos))

    if not bloques:
        return None

    cabecera = "📊 Alerta PER — " + datetime.now().strftime("%d/%m/%Y")
    pie = "\n\n* = ajuste de extraordinarios sin efecto relevante."
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
