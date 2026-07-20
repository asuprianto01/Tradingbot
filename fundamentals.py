"""
Fundamental data for IDX stocks via Yahoo Finance.

Counterpart to idx_data.py, but for balance-sheet / valuation metrics instead
of price candles. Returns a normalised dict per ticker so the quality-value
screener can score every stock the same way, gracefully handling the fields
Yahoo leaves empty (common for banks: debt/equity, current ratio, FCF).

Requires: pip install yfinance
"""

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import math

import yfinance as yf

import idx_data


def _pct(x):
    """Yahoo reports ratios like ROE/margins as fractions (0.23 = 23%)."""
    return x * 100 if x is not None else None


def _ratio(x, max_sane):
    """
    Sanitise a valuation ratio (P/E, P/B, PEG). Yahoo occasionally returns
    garbage (e.g. P/B in the thousands, or a negative P/E for a loss-maker).
    Return None for anything non-positive or absurdly large so it's treated
    as "unavailable" rather than skewing the score.
    """
    if x is None or x <= 0 or x > max_sane:
        return None
    return x


def _cagr(statement, row_names, max_years=3):
    """
    Compound annual growth rate (percent) for a line item over the most recent
    `max_years` years available. Uses the newest valid year and the valid year
    up to `max_years` back. Returns None if fewer than two positive data points
    exist (CAGR is meaningless across a sign change or from a loss).
    """
    if statement is None or getattr(statement, "empty", True):
        return None
    series = None
    for name in row_names:
        if name in statement.index:
            series = statement.loc[name]
            break
    if series is None:
        return None

    points = []  # (year, value) for valid, positive values
    for col, val in series.items():
        if val is None or val != val or val <= 0:   # NaN or non-positive
            continue
        year = getattr(col, "year", None)
        if year is not None:
            points.append((year, float(val)))
    if len(points) < 2:
        return None

    points.sort()                              # oldest .. newest
    points = points[-(max_years + 1):]         # keep newest (max_years+1) points
    (y0, v0), (y1, v1) = points[0], points[-1]
    years = y1 - y0
    if years <= 0:
        return None
    return ((v1 / v0) ** (1 / years) - 1) * 100


def _col(df, names, idx=0):
    """Value of the first matching row at column `idx` (0 = latest year)."""
    if df is None or getattr(df, "empty", True):
        return None
    for name in names:
        if name in df.index:
            try:
                v = df.loc[name].iloc[idx]
            except (IndexError, KeyError):
                return None
            if v is not None and v == v:       # not NaN
                return float(v)
    return None


def piotroski_f_score(income, balance, cashflow):
    """
    Piotroski F-Score (0-9): nine yes/no tests of profitability, leverage/
    liquidity and operating efficiency, comparing the latest year to the prior
    one. Designed for non-financial firms — several tests can't be computed for
    banks (no current assets/liabilities, no gross profit), so those are marked
    N/A rather than failed.

    Returns (score, na_count, criteria) where criteria is a list of
    (label, awarded) and awarded is True / False / None(=N/A).
    """
    ni0 = _col(income, ["Net Income", "Net Income Common Stockholders"], 0)
    ni1 = _col(income, ["Net Income", "Net Income Common Stockholders"], 1)
    ta0 = _col(balance, ["Total Assets"], 0)
    ta1 = _col(balance, ["Total Assets"], 1)
    rev0 = _col(income, ["Total Revenue", "Operating Revenue"], 0)
    rev1 = _col(income, ["Total Revenue", "Operating Revenue"], 1)
    cfo0 = _col(cashflow, ["Operating Cash Flow"], 0)
    if cfo0 is None:
        cfo0 = _col(cashflow, ["Free Cash Flow"], 0)

    def has(*xs):
        return all(x is not None for x in xs)

    criteria = []
    # --- Profitability ---
    criteria.append(("Laba bersih positif", (ni0 > 0) if ni0 is not None else None))
    criteria.append(("Arus kas operasi positif", (cfo0 > 0) if cfo0 is not None else None))
    criteria.append(("ROA naik (YoY)",
                     (ni0 / ta0) > (ni1 / ta1) if has(ni0, ni1, ta0, ta1) and ta0 > 0 and ta1 > 0 else None))
    criteria.append(("Kualitas laba (CFO > Laba)",
                     (cfo0 > ni0) if has(cfo0, ni0) else None))
    # --- Leverage / Liquidity ---
    ltd0 = _col(balance, ["Long Term Debt", "Total Debt"], 0)
    ltd1 = _col(balance, ["Long Term Debt", "Total Debt"], 1)
    criteria.append(("Leverage turun",
                     (ltd0 / ta0) < (ltd1 / ta1) if has(ltd0, ltd1, ta0, ta1) and ta0 > 0 and ta1 > 0 else None))
    ca0, ca1 = _col(balance, ["Current Assets"], 0), _col(balance, ["Current Assets"], 1)
    cl0, cl1 = _col(balance, ["Current Liabilities"], 0), _col(balance, ["Current Liabilities"], 1)
    criteria.append(("Current ratio naik",
                     (ca0 / cl0) > (ca1 / cl1) if has(ca0, ca1, cl0, cl1) and cl0 > 0 and cl1 > 0 else None))
    sh0 = _col(balance, ["Ordinary Shares Number", "Share Issued"], 0)
    sh1 = _col(balance, ["Ordinary Shares Number", "Share Issued"], 1)
    criteria.append(("Tidak menerbitkan saham baru",
                     (sh0 <= sh1 * 1.001) if has(sh0, sh1) else None))
    # --- Operating efficiency ---
    gp0, gp1 = _col(income, ["Gross Profit"], 0), _col(income, ["Gross Profit"], 1)
    criteria.append(("Gross margin naik",
                     (gp0 / rev0) > (gp1 / rev1) if has(gp0, gp1, rev0, rev1) and rev0 > 0 and rev1 > 0 else None))
    criteria.append(("Asset turnover naik",
                     (rev0 / ta0) > (rev1 / ta1) if has(rev0, rev1, ta0, ta1) and ta0 > 0 and ta1 > 0 else None))

    score = sum(1 for _, a in criteria if a is True)
    na = sum(1 for _, a in criteria if a is None)
    return score, na, criteria


def graham_margin_of_safety(info, price):
    """
    Graham Number as a conservative fair value: sqrt(22.5 * EPS * BVPS), and the
    margin of safety vs the current price. Positive MoS% = trading below fair
    value. Returns (fair_value, mos_pct), or (None, None) if EPS/BVPS unusable.

    Note: the Graham Number suits stable, profitable, non-financial companies
    and understates fair value for high-growth firms. First-pass gauge only.
    """
    eps = info.get("trailingEps")
    bvps = info.get("bookValue")
    if not price or eps is None or bvps is None or eps <= 0 or bvps <= 0:
        return None, None
    fair = math.sqrt(22.5 * eps * bvps)
    # Guard against unit mismatches (some IDX firms — e.g. coal/energy — report
    # EPS & book value in USD while the price is in IDR). If fair value is more
    # than 10x off the price, the inputs are almost certainly inconsistent.
    if fair < price / 10 or fair > price * 10:
        return None, None
    mos = (fair - price) / fair * 100
    return round(fair), round(mos, 1)


def _debt_to_equity(info, balance_sheet):
    """
    Debt/Equity in percent. Prefer Yahoo's info value; if missing (common for
    banks), compute it from the balance sheet. Returns None if unavailable.
    """
    de = info.get("debtToEquity")
    if de is not None:
        return de
    if balance_sheet is None or getattr(balance_sheet, "empty", True):
        return None
    try:
        debt = balance_sheet.loc["Total Debt"].dropna()
        equity = balance_sheet.loc["Stockholders Equity"].dropna()
        if len(debt) and len(equity) and equity.iloc[0] > 0:
            return float(debt.iloc[0]) / float(equity.iloc[0]) * 100
    except (KeyError, IndexError, ZeroDivisionError):
        pass
    return None


def get_profile(ticker):
    """Lightweight company profile (name, sector, industry, business summary)
    for the detail panel — used by the technical tab which otherwise doesn't
    fetch fundamentals."""
    symbol = idx_data.to_yahoo_symbol(ticker)
    info = yf.Ticker(symbol).info or {}
    return {
        "ticker": idx_data.display_symbol(ticker),
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "summary": info.get("longBusinessSummary"),
        "website": info.get("website"),
    }


def get_fundamentals(ticker):
    """
    Return a dict of normalised fundamentals for one ticker. Missing values
    are None (not skipped) so the scorer knows the difference between a bad
    number and an unavailable one.

    Percent fields (roe, roa, margins, growth, dividend_yield) are in percent.
    Ratio fields (pe, pb, peg, debt_to_equity, current_ratio) are as-is.
    """
    symbol = idx_data.to_yahoo_symbol(ticker)
    tk = yf.Ticker(symbol)
    info = tk.info or {}

    # Annual statements for multi-year growth, F-Score and a D/E fallback.
    # Wrapped defensively: some tickers return nothing here.
    try:
        income = tk.income_stmt
    except Exception:
        income = None
    try:
        balance = tk.balance_sheet
    except Exception:
        balance = None
    try:
        cashflow = tk.cashflow
    except Exception:
        cashflow = None

    net_profit_cagr = _cagr(income, ["Net Income", "Net Income Common Stockholders"], max_years=3)
    revenue_cagr = _cagr(income, ["Total Revenue", "Operating Revenue"], max_years=3)

    f_score, f_na, f_criteria = piotroski_f_score(income, balance, cashflow)

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    fair_value, margin_of_safety = graham_margin_of_safety(info, price)

    # dividendYield in the installed yfinance is already expressed in percent
    # (e.g. 6.41 = 6.41%); keep it as-is.
    div_yield = info.get("dividendYield")

    return {
        "ticker": idx_data.display_symbol(ticker),
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "summary": info.get("longBusinessSummary"),
        "price": price,
        "market_cap": info.get("marketCap"),
        # --- Value (ratios sanitised: junk / negative -> None) ---
        "pe": _ratio(info.get("trailingPE"), 1000),
        "forward_pe": _ratio(info.get("forwardPE"), 1000),
        "pb": _ratio(info.get("priceToBook"), 1000),
        "peg": _ratio(info.get("pegRatio"), 100),
        "dividend_yield": div_yield,
        # --- Quality ---
        "roe": _pct(info.get("returnOnEquity")),
        "roa": _pct(info.get("returnOnAssets")),
        "profit_margin": _pct(info.get("profitMargins")),
        "operating_margin": _pct(info.get("operatingMargins")),
        "earnings_growth": _pct(info.get("earningsGrowth")),      # YoY
        "revenue_growth": _pct(info.get("revenueGrowth")),        # YoY
        "net_profit_cagr_3y": net_profit_cagr,                    # 3y CAGR
        "revenue_cagr_3y": revenue_cagr,                          # 3y CAGR
        "debt_to_equity": _debt_to_equity(info, balance),
        "current_ratio": info.get("currentRatio"),
        # --- Piotroski F-Score & Graham margin of safety ---
        "f_score": f_score,
        "f_score_na": f_na,
        "f_criteria": f_criteria,
        "fair_value": fair_value,
        "margin_of_safety": margin_of_safety,
    }
