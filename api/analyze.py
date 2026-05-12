"""
Buffett Screener — Vercel Serverless Function
Receives a ticker, fetches financial data from Financial Modeling Prep,
applies Warren Buffett's 6 criteria, and returns scores + raw values.
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler

FMP_BASE = "https://financialmodelingprep.com/stable"
API_KEY = os.environ.get("FMP_API_KEY", "")


def fmp_get(endpoint: str, params: dict = None) -> list | dict:
    """Call an FMP endpoint and return the parsed JSON."""
    params = params or {}
    params["apikey"] = API_KEY
    url = f"{FMP_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "buffett-screener/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def safe(value, default=0.0):
    """Coerce to float, treating None/missing as default."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def score_linear(actual: float, target: float, *, higher_is_better: bool = True) -> int:
    """
    Map a metric to a 0-100 score.
    higher_is_better=True : >= target ->100, == 0 -> 0 (linear)
    higher_is_better=False: <= target ->100, == 2*target -> 0 (linear)
    """
    if higher_is_better:
        if actual <= 0:
            return 0
        return max(0, min(100, round((actual / target) * 100))) if target else 0
    else:
        if actual <= 0:
            return 100
        if actual >= 2 * target:
            return 0
        return max(0, min(100, round((2 * target - actual) / target * 100)))


def analyze(ticker: str) -> dict:
    """Run the 6 Buffett criteria for a single ticker."""
    ticker = ticker.upper().strip()

    # ----- 1. Pull data -----
    profile_data = fmp_get("profile", {"symbol": ticker})
    if not profile_data:
        return {"error": f"Ticker '{ticker}' not found on FMP."}
    profile = profile_data[0]

    income = fmp_get("income-statement", {"symbol": ticker, "limit": 5})
    balance = fmp_get("balance-sheet-statement", {"symbol": ticker, "limit": 5})
    cashflow = fmp_get("cash-flow-statement", {"symbol": ticker, "limit": 5})
    ratios = fmp_get("ratios", {"symbol": ticker, "limit": 5})
    key_metrics = fmp_get("key-metrics", {"symbol": ticker, "limit": 5})

    if not (income and balance and cashflow):
        return {"error": f"Insufficient financial data for '{ticker}'."}

    # ----- 2. Criterion I: 5Y avg ROE > 15% -----
    roe_values = []
    for i in range(min(5, len(ratios))):
        roe = safe(ratios[i].get("returnOnEquity"))
        if roe != 0:
            roe_values.append(roe * 100)  # FMP returns decimal, convert to %
    avg_roe = sum(roe_values) / len(roe_values) if roe_values else 0
    score_roe = score_linear(avg_roe, 15, higher_is_better=True)

    # ----- 2. Criterion II: Long-term Debt < Net Income * 3 -----
    latest_balance = balance[0]
    long_term_debt = safe(latest_balance.get("longTermDebt"))
    # Sum of last 4 quarters of net income, approximated by latest annual
    net_income = safe(income[0].get("netIncome"))
    debt_ratio = long_term_debt / net_income if net_income > 0 else 99
    score_debt = score_linear(debt_ratio, 3, higher_is_better=False)

    # ----- 3. Criterion III: 5Y EPS CAGR > 10% -----
    if len(income) >= 5:
        eps_now = safe(income[0].get("eps"))
        eps_then = safe(income[4].get("eps"))
        if eps_then > 0 and eps_now > 0:
            eps_cagr = ((eps_now / eps_then) ** (1/4) - 1) * 100
        else:
            eps_cagr = 0
    else:
        eps_cagr = 0
    score_growth = score_linear(eps_cagr, 10, higher_is_better=True)

    # ----- 4. Criterion IV: Cash Conversion Cycle < 120 days -----
    # CCC = DIO + DSO - DPO
    revenue = safe(income[0].get("revenue"), 1)
    cogs = safe(income[0].get("costOfRevenue"), 1)
    inventory = safe(latest_balance.get("inventory"))
    receivables = safe(latest_balance.get("netReceivables"))
    payables = safe(latest_balance.get("accountPayables"))
    dio = (inventory / cogs) * 365 if cogs > 0 else 0
    dso = (receivables / revenue) * 365 if revenue > 0 else 0
    dpo = (payables / cogs) * 365 if cogs > 0 else 0
    ccc = dio + dso - dpo
    score_ccc = score_linear(ccc, 120, higher_is_better=False)

    # ----- 5. Criterion V: Retained-earnings efficiency -----
    # ΔRetained × 0.5 < ΔQuick Assets (over 5y)
    if len(balance) >= 5:
        re_now = safe(balance[0].get("retainedEarnings"))
        re_then = safe(balance[4].get("retainedEarnings"))
        qa_now = safe(balance[0].get("cashAndShortTermInvestments")) + safe(balance[0].get("netReceivables"))
        qa_then = safe(balance[4].get("cashAndShortTermInvestments")) + safe(balance[4].get("netReceivables"))
        delta_re = re_now - re_then
        delta_qa = qa_now - qa_then
        if delta_re > 0:
            efficiency = delta_qa / (delta_re * 0.5)
        else:
            efficiency = 1
        # score: >= 1.0 -> 100, 0 -> 0
        score_retained = max(0, min(100, round(efficiency * 70)))
    else:
        score_retained = 50
        efficiency = 0

    # ----- 6. Criterion VI: FCF / Revenue > 7% -----
    fcf_values = []
    for i in range(min(5, len(cashflow))):
        fcf = safe(cashflow[i].get("freeCashFlow"))
        rev = safe(income[i].get("revenue"), 1) if i < len(income) else 1
        if rev > 0:
            fcf_values.append((fcf / rev) * 100)
    avg_fcf_margin = sum(fcf_values) / len(fcf_values) if fcf_values else 0
    score_fcf = score_linear(avg_fcf_margin, 7, higher_is_better=True)

    # ----- Assemble result -----
    scores = [score_roe, score_debt, score_growth, score_ccc, score_retained, score_fcf]
    avg_score = round(sum(scores) / 6)

    return {
        "ticker": ticker,
        "company": profile.get("companyName", ticker),
        "exchange": profile.get("exchangeShortName", profile.get("exchange", "")),
        "price": profile.get("price"),
        "industry": profile.get("industry"),
        "scores": scores,
        "averageScore": avg_score,
        "raw": {
            "roe": round(avg_roe, 1),
            "debtRatio": round(debt_ratio, 2),
            "epsCagr": round(eps_cagr, 1),
            "ccc": round(ccc),
            "retainedEfficiency": round(efficiency, 2),
            "fcfMargin": round(avg_fcf_margin, 1),
        },
        "rawLabels": [
            f"ROE {avg_roe:.1f}%",
            f"{debt_ratio:.1f}×",
            f"{eps_cagr:.1f}% CAGR",
            f"CCC {ccc:.0f}d",
            f"Ratio {efficiency:.2f}",
            f"FCF {avg_fcf_margin:.1f}%",
        ],
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Parse the ?ticker=AAPL query
        path = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(path.query)
        ticker = (params.get("ticker") or [""])[0]

        # CORS headers (so GitHub Pages site can call this endpoint)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Cache-Control", "public, max-age=86400")  # cache 24h
        self.end_headers()

        if not ticker:
            self.wfile.write(json.dumps({"error": "Missing ?ticker= parameter."}).encode("utf-8"))
            return

        if not API_KEY:
            self.wfile.write(json.dumps({"error": "Server not configured: FMP_API_KEY missing."}).encode("utf-8"))
            return

        try:
            result = analyze(ticker)
            self.wfile.write(json.dumps(result).encode("utf-8"))
        except urllib.error.HTTPError as e:
            self.wfile.write(json.dumps({"error": f"FMP API error {e.code}: {e.reason}"}).encode("utf-8"))
        except Exception as e:
            self.wfile.write(json.dumps({"error": f"Server error: {type(e).__name__}: {e}"}).encode("utf-8"))

    def do_OPTIONS(self):
        # CORS preflight
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
