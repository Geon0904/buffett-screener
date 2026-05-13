"""
Buffett Screener - Vercel Serverless Function (v1.5)
v1.4 -> v1.5:
- Detects tickers blocked behind FMP's free-tier symbol whitelist.
  When a financial-statement endpoint returns the "Premium Query Parameter"
  message instead of an array, surfaces a structured
  {"errorCode": "PREMIUM_REQUIRED"} so the frontend can render a friendly
  bilingual notice ("this ticker needs the paid plan").
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler

FMP_BASE = "https://financialmodelingprep.com/stable"
API_KEY = os.environ.get("FMP_API_KEY", "")


def to_fmp_ticker(ticker: str) -> str:
    """FMP uses hyphens for share-class suffixes: BRK.B -> BRK-B."""
    return ticker.upper().strip().replace(".", "-")


def fmp_get(endpoint, params=None):
    """
    Call an FMP endpoint and return parsed JSON.
    Returns either a list (success) or a dict with keys like
    'Error Message' / 'message' when the call is blocked.
    """
    params = params or {}
    params["apikey"] = API_KEY
    url = f"{FMP_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "buffett-screener/1.5"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
        # Some FMP endpoints return plain text (not JSON) when blocked.
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"_raw": body}


def is_premium_blocked(resp) -> bool:
    """
    True if the response is FMP's 'this symbol needs a paid plan' notice.
    Free-tier financial statements for some tickers return a string or dict
    containing 'Premium Query Parameter' or 'subscription'.
    """
    if isinstance(resp, dict):
        for key in ("Error Message", "message", "_raw"):
            v = resp.get(key, "")
            if isinstance(v, str) and ("premium" in v.lower() or "subscription" in v.lower()):
                return True
    elif isinstance(resp, str):
        return "premium" in resp.lower() or "subscription" in resp.lower()
    return False


def safe(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def score_linear(actual, target, *, higher_is_better=True):
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


def generate_narratives(raw, scores, sector):
    roe = raw["roe"]
    debt = raw["debtRatio"]
    growth = raw["epsCagr"]
    ccc = raw["ccc"]
    retained = raw["retainedEfficiency"]
    fcf = raw["fcfMargin"]
    avg = sum(scores) / 6

    sector_lower = (sector or "").lower()
    is_financial = any(s in sector_lower for s in ["bank", "insurance", "financial"])

    ko_parts = []
    en_parts = []

    if avg >= 85:
        ko_parts.append("6대 기준 대부분을 통과하는 보기 드문 우량주입니다.")
        en_parts.append("A rare business that clears most of the six pillars.")
    elif avg >= 70:
        ko_parts.append("전반적으로 양호하나 일부 약한 지표가 있습니다.")
        en_parts.append("Solid overall, with a few softer marks.")
    elif avg >= 50:
        ko_parts.append("기준에 부합하는 면과 미달하는 면이 공존합니다.")
        en_parts.append("Mixed picture - passes some tests, fails others.")
    else:
        ko_parts.append("버핏의 정량 기준에서는 미달입니다.")
        en_parts.append("Falls short of Buffett's quantitative thresholds.")

    if roe > 80:
        ko_parts.append(f"ROE {roe:.0f}%는 비정상적으로 높은 수치로, 공격적인 자사주 매입으로 자기자본이 축소된 영향이 큽니다. 본질적 수익성은 양호하지만 ROE 수치는 보정해서 봐야 합니다.")
        en_parts.append(f"The {roe:.0f}% ROE is unusually high - aggressive buybacks have shrunk the equity base, inflating the figure. Underlying profitability is healthy, but treat the ROE number with caution.")
    elif roe > 40:
        ko_parts.append(f"ROE {roe:.0f}%는 압도적인 자본 효율성을 보여줍니다.")
        en_parts.append(f"An ROE of {roe:.0f}% shows commanding capital efficiency.")

    if ccc < -30:
        ko_parts.append(f"현금회전일수 {ccc:.0f}일은 공급망에서 압도적인 협상력을 가졌다는 신호입니다. 협력사가 먼저 돈을 받고 일하는 구조에 가깝습니다.")
        en_parts.append(f"A cash cycle of {ccc:.0f} days signals dominant supply-chain power - the company collects cash before paying suppliers.")
    elif ccc > 150:
        ko_parts.append(f"현금회전일수 {ccc:.0f}일은 운전자본 부담이 크다는 뜻으로, 불황기에 취약할 수 있습니다.")
        en_parts.append(f"A {ccc:.0f}-day cash cycle indicates heavy working-capital strain - vulnerable in downturns.")

    if retained < -0.1:
        ko_parts.append("잉여금 효율이 음수인 것은 회사가 번 돈을 사내에 쌓아두기보다 배당·자사주 매입으로 주주에게 환원하고 있다는 의미입니다. 자체로는 나쁜 신호가 아닙니다.")
        en_parts.append("Negative retained-earnings efficiency means the company is returning profits to shareholders via dividends and buybacks rather than hoarding them - not inherently a bad sign.")

    if fcf < 5 and fcf > 0:
        ko_parts.append(f"FCF 마진 {fcf:.1f}%는 낮은 편으로, 유통·자본집약 산업의 구조적 특징입니다. 운영 효율로 보완되는지 별도로 확인할 필요가 있습니다.")
        en_parts.append(f"The {fcf:.1f}% FCF margin is thin - a structural feature of retail or capital-intensive industries. Worth checking whether operating efficiency compensates.")
    elif fcf > 25:
        ko_parts.append(f"FCF 마진 {fcf:.0f}%는 압도적인 현금 창출력을 보여줍니다.")
        en_parts.append(f"An FCF margin of {fcf:.0f}% reflects exceptional cash generation.")

    if debt > 5 and not is_financial:
        ko_parts.append(f"장기부채가 순이익의 {debt:.1f}배로 부담스러운 수준입니다. 불황 견딜 여력에 의문을 가질 만합니다.")
        en_parts.append(f"Long-term debt at {debt:.1f}x earnings is heavy and raises questions about downturn resilience.")
    elif debt < 0.3 and not is_financial:
        ko_parts.append("부채가 거의 없어 재무 안정성이 탁월합니다.")
        en_parts.append("Near-debtless balance sheet - exceptional financial stability.")

    if growth > 40:
        ko_parts.append(f"EPS가 연 {growth:.0f}%씩 성장 중인데, 이 속도가 지속가능한지는 별개의 질문입니다.")
        en_parts.append(f"EPS is compounding at {growth:.0f}% - whether that pace is sustainable is a separate question.")
    elif growth < 5 and growth > 0:
        ko_parts.append(f"EPS 성장률이 연 {growth:.1f}%로 정체에 가깝습니다. 성장보다 배당·안정성 관점에서 접근하는 편이 어울립니다.")
        en_parts.append(f"EPS growth at {growth:.1f}% is near-flat - better suited to a dividend/stability lens than a growth one.")

    if is_financial:
        ko_parts.append("은행/보험 섹터는 부채가 본업 구조이므로 II번 기준이 정량적 의미가 약합니다.")
        en_parts.append("For banking and insurance, debt is structural to the business model - Criterion II is less meaningful here.")

    ko_parts.append("이 도구는 정량 진단이며, 실제 투자 결정 전 경제적 해자와 밸류에이션을 별도로 검토하시기 바랍니다.")
    en_parts.append("This is a quantitative screen only; review the economic moat and current valuation separately before any decision.")

    return {
        "ko": " ".join(ko_parts),
        "en": " ".join(en_parts),
    }


def analyze(ticker_in):
    display_ticker = ticker_in.upper().strip()
    fmp_ticker = to_fmp_ticker(display_ticker)

    profile_data = fmp_get("profile", {"symbol": fmp_ticker})
    if not profile_data or isinstance(profile_data, dict):
        return {"error": f"Ticker '{display_ticker}' not found on FMP."}
    profile = profile_data[0]

    income = fmp_get("income-statement", {"symbol": fmp_ticker, "limit": 5})
    balance = fmp_get("balance-sheet-statement", {"symbol": fmp_ticker, "limit": 5})
    cashflow = fmp_get("cash-flow-statement", {"symbol": fmp_ticker, "limit": 5})
    ratios = fmp_get("ratios", {"symbol": fmp_ticker, "limit": 5})

    # Detect premium-blocked symbols (returns text instead of JSON array)
    if (is_premium_blocked(income) or is_premium_blocked(balance) or
            is_premium_blocked(cashflow)):
        return {
            "errorCode": "PREMIUM_REQUIRED",
            "ticker": display_ticker,
            "company": profile.get("companyName", display_ticker),
        }

    # Normalize: only proceed if we got real arrays
    if not (isinstance(income, list) and isinstance(balance, list)
            and isinstance(cashflow, list)):
        return {"error": f"Insufficient financial data for '{display_ticker}'."}
    if not (income and balance and cashflow):
        return {"error": f"Insufficient financial data for '{display_ticker}'."}
    # ratios may not be available but we have a fallback
    if not isinstance(ratios, list):
        ratios = []

    # ===== ROE =====
    roe_values = []
    for i in range(min(5, len(ratios))):
        npe = safe(ratios[i].get("netIncomePerShare"))
        bvps = safe(ratios[i].get("bookValuePerShare"))
        if bvps > 0 and npe != 0:
            roe_values.append((npe / bvps) * 100)
    if not roe_values:
        for i in range(min(5, len(income), len(balance))):
            ni = safe(income[i].get("netIncome"))
            eq = safe(balance[i].get("totalStockholdersEquity"))
            if eq > 0 and ni != 0:
                roe_values.append((ni / eq) * 100)
    avg_roe = sum(roe_values) / len(roe_values) if roe_values else 0
    score_roe = score_linear(avg_roe, 15, higher_is_better=True)

    # ===== Debt =====
    latest_balance = balance[0]
    long_term_debt = safe(latest_balance.get("longTermDebt"))
    if long_term_debt == 0:
        long_term_debt = safe(latest_balance.get("totalNonCurrentLiabilities"))
    net_income = safe(income[0].get("netIncome"))
    if net_income > 0:
        debt_ratio = long_term_debt / net_income
    else:
        debt_ratio = safe(ratios[0].get("debtToEquityRatio")) * 3 if ratios else 99
    score_debt = score_linear(debt_ratio, 3, higher_is_better=False)

    # ===== EPS CAGR =====
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

    # ===== CCC =====
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

    # ===== Retained efficiency =====
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
        score_retained = max(0, min(100, round(efficiency * 70)))
    else:
        score_retained = 50
        efficiency = 0

    # ===== FCF margin =====
    fcf_values = []
    for i in range(min(5, len(cashflow))):
        fcf = safe(cashflow[i].get("freeCashFlow"))
        rev = safe(income[i].get("revenue"), 1) if i < len(income) else 1
        if rev > 0:
            fcf_values.append((fcf / rev) * 100)
    avg_fcf_margin = sum(fcf_values) / len(fcf_values) if fcf_values else 0
    score_fcf = score_linear(avg_fcf_margin, 7, higher_is_better=True)

    scores = [score_roe, score_debt, score_growth, score_ccc, score_retained, score_fcf]
    avg_score = round(sum(scores) / 6)

    raw = {
        "roe": round(avg_roe, 1),
        "debtRatio": round(debt_ratio, 2),
        "epsCagr": round(eps_cagr, 1),
        "ccc": round(ccc),
        "retainedEfficiency": round(efficiency, 2),
        "fcfMargin": round(avg_fcf_margin, 1),
    }
    narratives = generate_narratives(raw, scores, profile.get("industry", ""))

    return {
        "ticker": display_ticker,
        "company": profile.get("companyName", display_ticker),
        "exchange": profile.get("exchangeShortName", profile.get("exchange", "")),
        "price": profile.get("price"),
        "industry": profile.get("industry"),
        "scores": scores,
        "averageScore": avg_score,
        "raw": raw,
        "rawLabels": [
            f"ROE {avg_roe:.1f}%",
            f"{debt_ratio:.1f}\u00d7",
            f"{eps_cagr:.1f}% CAGR",
            f"CCC {ccc:.0f}d",
            f"Ratio {efficiency:.2f}",
            f"FCF {avg_fcf_margin:.1f}%",
        ],
        "narratives": narratives,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(path.query)
        ticker = (params.get("ticker") or [""])[0]

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        # Cache successful results, NOT error responses
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
            # FMP returns 402 for BOTH real quota exhaustion AND
            # symbol-level premium restrictions. Distinguish by body text.
            if e.code == 402:
                try:
                    body = e.read().decode("utf-8", errors="ignore").lower()
                except Exception:
                    body = ""
                if "premium" in body or "subscription" in body:
                    self.wfile.write(json.dumps({
                        "errorCode": "PREMIUM_REQUIRED",
                        "ticker": ticker.upper().strip(),
                    }).encode("utf-8"))
                else:
                    self.wfile.write(json.dumps({
                        "errorCode": "RATE_LIMIT",
                        "error": "Daily data quota reached.",
                    }).encode("utf-8"))
            elif e.code == 403:
                self.wfile.write(json.dumps({
                    "errorCode": "FORBIDDEN",
                    "error": "FMP API key rejected.",
                }).encode("utf-8"))
            else:
                self.wfile.write(json.dumps({
                    "error": f"FMP API error {e.code}: {e.reason}",
                }).encode("utf-8"))
        except Exception as e:
            self.wfile.write(json.dumps({
                "error": f"Server error: {type(e).__name__}: {e}",
            }).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
