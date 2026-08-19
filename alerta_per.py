import os
import yfinance as yf
import requests
from datetime import datetime

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ──────────────────────────────────────────────────────────────
# Lista de empresas a vigilar. Solo hace falta el ticker.
# (formato Yahoo Finance: sufijo de bolsa si no cotiza en EEUU,
# ej. Airbus en París = "AIR.PA")
# ──────────────────────────────────────────────────────────────
COMPANIES = {
    "META":   "Meta",
    "GOOGL":  "Alphabet",
    "BABA":   "Alibaba",
    "NKE":    "Nike",
    "EL":     "Estée Lauder",
    "NVO":    "Novo Nordisk",
    "AIR.PA": "Airbus",
}

THRESHOLDS = [
    (27, "🟢"),
    (25, "🟡"),
    (21, "🟠"),
    (19, "🔴"),
]

# Nombres posibles que usa yfinance para la línea de "otros ingresos/gastos"
# según la empresa (varía un poco entre tickers)
OTHER_INCOME_CANDIDATES = [
    "Other Income Expense",
    "Total Other Income Expense Net",
    "Other Non Operating Income Expenses",
    "Net Non Operating Interest Income Expense",
]


def get_price(ticker):
    t = yf.Ticker(ticker)
    hist = t.history(period="5d")
    return float(hist["Close"].iloc[-1])


def find_row(df, candidates):
    for name in candidates:
        if name in df.index:
            return df.loc[name]
    # búsqueda flexible por si el nombre exacto no coincide
    for idx in df.index:
        low = str(idx).lower()
        if "other" in low and ("income" in low or "expense" in low):
            return df.loc[idx]
    return None


def get_adjusted_eps_proxy(ticker):
    """EPS TTM excluyendo la línea 'Other Income/Expense, net' de los
    últimos 4 trimestres. Devuelve (eps_proxy, eps_gaap_ttm) o (None, None)."""
    t = yf.Ticker(ticker)
    q = t.quarterly_income_stmt
    if q is None or q.empty:
        return None, None

    q = q.iloc[:, :4]  # últimos 4 trimestres

    net_income_row = q.loc["Net Income"] if "Net Income" in q.index else None
    shares_row = (
        q.loc["Diluted Average Shares"]
        if "Diluted Average Shares" in q.index
        else None
    )
    other_row = find_row(q, OTHER_INCOME_CANDIDATES)

    if net_income_row is None or shares_row is None:
        return None, None

    net_income_ttm = net_income_row.sum()
    shares_avg = shares_row.mean()
    other_ttm = other_row.sum() if other_row is not None else 0

    if not shares_avg or shares_avg == 0:
        return None, None

    eps_gaap_ttm = net_income_ttm / shares_avg
    eps_proxy = (net_income_ttm - other_ttm) / shares_avg

    return eps_proxy, eps_gaap_ttm


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


def build_message():
    tiers = {th: [] for th, _ in THRESHOLDS}

    for ticker, name in COMPANIES.items():
        try:
            price = get_price(ticker)
            eps_proxy, eps_gaap = get_adjusted_eps_proxy(ticker)
        except Exception:
            continue

        if not eps_proxy or eps_proxy <= 0:
            continue

        per = price / eps_proxy
        matched = tier_for_per(per)
        if not matched:
            continue

        th, emoji = matched
        eps_cagr, rev_cagr = get_cagr_3y(ticker)
        eps_txt = f"{eps_cagr:.0f}%" if eps_cagr is not None else "N/D"
        rev_txt = f"{rev_cagr:.0f}%" if rev_cagr is not None else "N/D"

        tiers[th].append(
            f"{emoji} {name.upper()} // PER {per:.1f}x (proxy) // "
            f"{eps_txt} CAGR EPS // {rev_txt} CAGR VENTAS"
        )

    bloques = []
    for th, emoji in THRESHOLDS:
        if tiers[th]:
            bloques.append(f"\nPER a {th} o menos {emoji}:\n" + "\n".join(tiers[th]))

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
