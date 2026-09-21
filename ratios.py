"""
ratios.py — Workstream B: a library of financial ratios and identities built
from the reference PDFs, evaluated against a filing's XBRL facts.

Two different jobs, kept separate on purpose:

1. RATIOS (compute_ratios): ~45 ratio formulas. A ratio is NOT an equation a
   filing must satisfy (a current ratio of 0.9 or 5 can both be healthy), so
   ratios are never used to declare a filing wrong. For each ratio we record
   whether it was computable ("computed") or not ("not_usable" + why), which
   XBRL facts fed it, which PDF(s) define it, and — advisory only — whether it
   falls outside a textbook rule of thumb.
   One ratio doubles as a real cross-check: basic EPS computed from net income
   and weighted shares must match the EPS the filing reports (within rounding).

2. IDENTITIES (identity_constraints): equalities the ratio definitions rely on
   (gross profit = revenue - cost of revenue, assets = liabilities + equity,
   ...). These ARE rules a clean filing must satisfy, so they are emitted in
   the same constraint format as filer-calc rules (rule_source
   "ratio-identity") and flow through check.py, solver.py (Z3), orphans.py and
   inject.py unchanged.

Formula sources: the CFA Level II ratio list and the Duke FSA note are read
as text. In the CFI and Gillingham PDFs the formulas are images, so those two
are cited only by page (where the ratio is discussed), not for formula text.

The formula-term -> XBRL-concept mapping (TERMS below) is ours, not from the
PDFs: no PDF says which us-gaap tag is "cash". Every input a ratio used is
returned so the mapping can be audited per filing.
"""

from datetime import date
from decimal import Decimal

import check
import constraints as constraints_mod

CFA = "CFA Level II ratio list"
DUKE = "Duke FSA note"
CFI = "CFI ratios e-book"
GILL = "Gillingham FRA book"

DAYS = Decimal(365)  # Duke FSA note uses 365


class Missing(Exception):
    def __init__(self, term):
        self.term = term


def _t(kind, *names):
    return {"kind": kind, "concepts": ["us-gaap:" + n for n in names]}


TERMS = {
    # balance sheet (instants)
    "current_assets": _t("instant", "AssetsCurrent"),
    "current_liabilities": _t("instant", "LiabilitiesCurrent"),
    "cash": _t("instant", "CashAndCashEquivalentsAtCarryingValue", "Cash"),
    "st_investments": _t("instant", "MarketableSecuritiesCurrent", "ShortTermInvestments",
                         "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
    "receivables": _t("instant", "AccountsReceivableNetCurrent", "ReceivablesNetCurrent",
                      "AccountsAndOtherReceivablesNetCurrent"),
    "inventory": _t("instant", "InventoryNet"),
    "accounts_payable": _t("instant", "AccountsPayableCurrent"),
    "total_assets": _t("instant", "Assets"),
    "total_liabilities": _t("instant", "Liabilities"),
    "equity": _t("instant", "StockholdersEquity"),
    "ppe_net": _t("instant", "PropertyPlantAndEquipmentNet",
                  "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"),
    "ppe_gross": _t("instant", "PropertyPlantAndEquipmentGross"),
    "accumulated_depreciation": _t("instant", "AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment"),
    "shares_outstanding": _t("instant", "CommonStockSharesOutstanding"),
    "commercial_paper": _t("instant", "CommercialPaper"),
    "short_term_debt": _t("instant", "DebtCurrent", "LongTermDebtCurrent"),
    "long_term_debt": _t("instant", "LongTermDebtNoncurrent", "LongTermDebt"),
    "total_debt": {"sum": [("commercial_paper", "opt"), ("short_term_debt", "opt"), ("long_term_debt", "req")]},
    # income statement / cash flow (fiscal-year durations)
    "revenue": _t("duration", "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                  "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    "cogs": _t("duration", "CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"),
    "gross_profit": _t("duration", "GrossProfit"),
    "operating_income": _t("duration", "OperatingIncomeLoss"),
    "pretax_income": _t("duration",
                        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
    "net_income": _t("duration", "NetIncomeLoss", "ProfitLoss"),
    "net_income_common": _t("duration", "NetIncomeLossAvailableToCommonStockholdersBasic", "NetIncomeLoss"),
    "income_tax": _t("duration", "IncomeTaxExpenseBenefit"),
    "interest_expense": _t("duration", "InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"),
    "cfo": _t("duration", "NetCashProvidedByUsedInOperatingActivities"),
    "capex": _t("duration", "PaymentsToAcquirePropertyPlantAndEquipment"),
    "dividends_paid": _t("duration", "PaymentsOfDividendsCommonStock", "PaymentsOfDividends"),
    "debt_repayments": _t("duration", "RepaymentsOfLongTermDebt", "RepaymentsOfDebt"),
    "lease_payments": _t("duration", "OperatingLeasePayments"),
    "interest_paid": _t("duration", "InterestPaidNet", "InterestPaid"),
    "taxes_paid": _t("duration", "IncomeTaxesPaidNet", "IncomeTaxesPaid"),
    "depreciation": _t("duration", "Depreciation", "DepreciationDepletionAndAmortization", "DepreciationAndAmortization"),
    "eps_basic": _t("duration", "EarningsPerShareBasic"),
    "shares_basic": _t("duration", "WeightedAverageNumberOfSharesOutstandingBasic"),
}


def _ratio(rid, name, category, formula, needs, fn, sources, rule_of_thumb=None, unusable=None):
    return {"id": rid, "name": name, "category": category, "formula": formula, "needs": needs,
            "fn": fn, "sources": sources, "rule_of_thumb": rule_of_thumb, "unusable": unusable}


def _src(*pairs):
    return [{"pdf": p, "ref": r} for p, r in pairs]


# token forms in `needs`: "x" (value at fiscal year end / duration), "avg:x"
# (mean of year-end and prior year-end balance), "prior:x", "opt:x" (0 if unreported)
RATIOS = [
    _ratio("current_ratio", "Current ratio", "liquidity", "Current assets / Current liabilities",
           ["current_assets", "current_liabilities"], lambda v: v["current_assets"] / v["current_liabilities"],
           _src((CFA, "#1"), (DUKE, "p2"), (CFI, "p22"), (GILL, "p28")), (">", Decimal(2), "Duke p2 rule of thumb: > 2")),
    _ratio("quick_ratio", "Quick ratio", "liquidity", "(Cash + ST investments + Receivables) / Current liabilities",
           ["cash", "opt:st_investments", "receivables", "current_liabilities"],
           lambda v: (v["cash"] + v["opt:st_investments"] + v["receivables"]) / v["current_liabilities"],
           _src((CFA, "#2"), (DUKE, "p2"), (GILL, "p30")), (">", Decimal(1), "Duke p2 rule of thumb: > 1")),
    _ratio("cash_ratio", "Cash ratio", "liquidity", "(Cash + ST investments) / Current liabilities",
           ["cash", "opt:st_investments", "current_liabilities"],
           lambda v: (v["cash"] + v["opt:st_investments"]) / v["current_liabilities"],
           _src((CFA, "#3"), (DUKE, "p2"), (GILL, "p31")), (">", Decimal("0.4"), "Duke p2 rule of thumb: > 40-50%")),
    _ratio("defensive_interval", "Defensive interval ratio", "liquidity",
           "(Cash + ST investments + Receivables) / Daily cash expenditures", [], None,
           _src((CFA, "#4"), (DUKE, "p2")),
           unusable=("no_standard_xbrl_concept", "daily cash expenditures (COGS + operating expenses excluding depreciation, per day) has no standard XBRL concept")),
    _ratio("working_capital", "Working capital", "liquidity", "Current assets - Current liabilities",
           ["current_assets", "current_liabilities"], lambda v: v["current_assets"] - v["current_liabilities"],
           _src((DUKE, "p2"), (GILL, "p51"))),
    _ratio("cfo_ratio", "CFO ratio", "liquidity", "CFO / Average current liabilities",
           ["cfo", "avg:current_liabilities"], lambda v: v["cfo"] / v["avg:current_liabilities"],
           _src((DUKE, "p2"), (CFI, "p24")), (">", Decimal("0.4"), "Duke p2 rule of thumb: > 40-50%")),
    _ratio("receivables_turnover", "Receivables turnover", "activity", "Revenue / Average receivables",
           ["revenue", "avg:receivables"], lambda v: v["revenue"] / v["avg:receivables"],
           _src((CFA, "#5"), (DUKE, "p3"))),
    _ratio("dso", "Days sales outstanding", "activity", "365 / Receivables turnover",
           ["revenue", "avg:receivables"], lambda v: DAYS / (v["revenue"] / v["avg:receivables"]),
           _src((CFA, "#6"), (DUKE, "p3"))),
    _ratio("inventory_turnover", "Inventory turnover", "activity", "COGS / Average inventory",
           ["cogs", "avg:inventory"], lambda v: v["cogs"] / v["avg:inventory"],
           _src((CFA, "#7"), (DUKE, "p3"), (CFI, "p19"), (GILL, "p48"))),
    _ratio("days_inventory", "Days of inventory on hand", "activity", "365 / Inventory turnover",
           ["cogs", "avg:inventory"], lambda v: DAYS / (v["cogs"] / v["avg:inventory"]),
           _src((CFA, "#8"), (DUKE, "p3"))),
    _ratio("payables_turnover", "Payables turnover", "activity",
           "Purchases (= COGS + change in inventory) / Average accounts payable",
           ["cogs", "inventory", "prior:inventory", "avg:accounts_payable"],
           lambda v: (v["cogs"] + v["inventory"] - v["prior:inventory"]) / v["avg:accounts_payable"],
           _src((CFA, "#9"), (DUKE, "p3"))),
    _ratio("days_payables", "Days of payables", "activity", "365 / Payables turnover",
           ["cogs", "inventory", "prior:inventory", "avg:accounts_payable"],
           lambda v: DAYS / ((v["cogs"] + v["inventory"] - v["prior:inventory"]) / v["avg:accounts_payable"]),
           _src((CFA, "#10"), (DUKE, "p3"))),
    _ratio("cash_conversion_cycle", "Cash conversion cycle", "activity", "DOH + DSO - Days of payables",
           ["revenue", "cogs", "inventory", "prior:inventory", "avg:inventory", "avg:receivables", "avg:accounts_payable"],
           lambda v: (DAYS / (v["cogs"] / v["avg:inventory"]) + DAYS / (v["revenue"] / v["avg:receivables"])
                      - DAYS / ((v["cogs"] + v["inventory"] - v["prior:inventory"]) / v["avg:accounts_payable"])),
           _src((CFA, "#11"), (DUKE, "p3"))),
    _ratio("working_capital_turnover", "Working capital turnover", "activity", "Revenue / Average working capital",
           ["revenue", "avg:current_assets", "avg:current_liabilities"],
           lambda v: v["revenue"] / (v["avg:current_assets"] - v["avg:current_liabilities"]),
           _src((CFA, "#12"), (DUKE, "p3"), (GILL, "p54"))),
    _ratio("fixed_asset_turnover", "Fixed asset turnover", "activity", "Revenue / Average net fixed assets",
           ["revenue", "avg:ppe_net"], lambda v: v["revenue"] / v["avg:ppe_net"],
           _src((CFA, "#13"), (DUKE, "p3"))),
    _ratio("asset_turnover", "Total asset turnover", "activity", "Revenue / Average total assets",
           ["revenue", "avg:total_assets"], lambda v: v["revenue"] / v["avg:total_assets"],
           _src((CFA, "#14"), (DUKE, "p3"), (CFI, "p18"), (GILL, "p39"))),
    _ratio("ppe_age", "Average PPE age", "activity", "Accumulated depreciation / Depreciation expense",
           ["accumulated_depreciation", "depreciation"], lambda v: v["accumulated_depreciation"] / v["depreciation"],
           _src((DUKE, "p3"))),
    _ratio("ppe_useful_life", "Average PPE useful life", "activity", "Gross PPE / Depreciation expense",
           ["ppe_gross", "depreciation"], lambda v: v["ppe_gross"] / v["depreciation"],
           _src((DUKE, "p3"))),
    _ratio("gross_margin", "Gross profit margin", "profitability", "Gross profit / Revenue",
           ["gross_profit", "revenue"], lambda v: v["gross_profit"] / v["revenue"],
           _src((CFA, "#15"), (DUKE, "p4"), (CFI, "p8"), (GILL, "p33"))),
    _ratio("operating_margin", "Operating profit margin", "profitability", "Operating income / Revenue",
           ["operating_income", "revenue"], lambda v: v["operating_income"] / v["revenue"],
           _src((CFA, "#16"), (DUKE, "p4"))),
    _ratio("pretax_margin", "Pretax margin", "profitability", "Earnings before tax / Revenue",
           ["pretax_income", "revenue"], lambda v: v["pretax_income"] / v["revenue"],
           _src((CFA, "#17"), (GILL, "p34"))),
    _ratio("net_margin", "Net profit margin", "profitability", "Net income / Revenue",
           ["net_income", "revenue"], lambda v: v["net_income"] / v["revenue"],
           _src((CFA, "#18"), (DUKE, "p4"), (CFI, "p10"), (GILL, "p34"))),
    _ratio("operating_roa", "Operating return on assets", "profitability", "Operating income / Average total assets",
           ["operating_income", "avg:total_assets"], lambda v: v["operating_income"] / v["avg:total_assets"],
           _src((CFA, "#19"))),
    _ratio("roa", "Return on assets", "profitability", "Net income / Average total assets",
           ["net_income", "avg:total_assets"], lambda v: v["net_income"] / v["avg:total_assets"],
           _src((CFA, "#20"), (CFI, "p6"), (GILL, "p37"))),
    _ratio("roa_duke", "Return on assets (Duke definition)", "profitability",
           "(Net income + Interest expense x (1 - tax rate)) / Average total assets",
           ["net_income", "interest_expense", "income_tax", "pretax_income", "avg:total_assets"],
           lambda v: (v["net_income"] + v["interest_expense"] * (1 - v["income_tax"] / v["pretax_income"])) / v["avg:total_assets"],
           _src((DUKE, "p4"))),
    _ratio("roe", "Return on equity", "profitability", "Net income / Average shareholders' equity",
           ["net_income", "avg:equity"], lambda v: v["net_income"] / v["avg:equity"],
           _src((CFA, "#21"), (DUKE, "p4"), (CFI, "p7"), (GILL, "p38"))),
    _ratio("roic_pretax", "Return on invested capital (pre-tax)", "profitability",
           "EBIT / (Average debt + Average equity), EBIT taken as operating income",
           ["operating_income", "avg:total_debt", "avg:equity"],
           lambda v: v["operating_income"] / (v["avg:total_debt"] + v["avg:equity"]),
           _src((CFA, "#22"))),
    _ratio("roic", "Return on invested capital", "profitability",
           "EBIT x (1 - effective tax rate) / (Average debt + Average equity)",
           ["operating_income", "income_tax", "pretax_income", "avg:total_debt", "avg:equity"],
           lambda v: v["operating_income"] * (1 - v["income_tax"] / v["pretax_income"]) / (v["avg:total_debt"] + v["avg:equity"]),
           _src((CFA, "#23"), (DUKE, "p4"))),
    _ratio("tax_burden", "Tax burden", "profitability", "Net income / Earnings before taxes",
           ["net_income", "pretax_income"], lambda v: v["net_income"] / v["pretax_income"], _src((CFA, "#25"))),
    _ratio("interest_burden", "Interest burden", "profitability", "Earnings before taxes / EBIT",
           ["pretax_income", "operating_income"], lambda v: v["pretax_income"] / v["operating_income"], _src((CFA, "#26"))),
    _ratio("ebit_margin", "EBIT margin", "profitability", "EBIT (operating income) / Revenue",
           ["operating_income", "revenue"], lambda v: v["operating_income"] / v["revenue"], _src((CFA, "#27"))),
    _ratio("cash_roa", "Cash return on assets", "profitability", "CFO / Average total assets",
           ["cfo", "avg:total_assets"], lambda v: v["cfo"] / v["avg:total_assets"], _src((DUKE, "p4"))),
    _ratio("financial_leverage", "Financial leverage", "solvency", "Average total assets / Average shareholders' equity",
           ["avg:total_assets", "avg:equity"], lambda v: v["avg:total_assets"] / v["avg:equity"],
           _src((CFA, "#28"), (DUKE, "p5"))),
    _ratio("debt_to_assets", "Debt-to-assets", "solvency", "Total debt / Total assets",
           ["total_debt", "total_assets"], lambda v: v["total_debt"] / v["total_assets"],
           _src((CFA, "#30"), (DUKE, "p5"))),
    _ratio("debt_to_equity", "Debt-to-equity", "solvency", "Total debt / Total shareholders' equity",
           ["total_debt", "equity"], lambda v: v["total_debt"] / v["equity"],
           _src((CFA, "#31"), (DUKE, "p5"))),
    _ratio("debt_to_capital", "Debt-to-capital", "solvency", "Total debt / (Total debt + Equity)",
           ["total_debt", "equity"], lambda v: v["total_debt"] / (v["total_debt"] + v["equity"]),
           _src((CFA, "#32"))),
    _ratio("interest_coverage", "Interest coverage (times interest earned)", "solvency", "EBIT / Interest expense",
           ["operating_income", "interest_expense"], lambda v: v["operating_income"] / v["interest_expense"],
           _src((CFA, "#33"), (DUKE, "p5")), (">", Decimal(2), "Duke p5 rule of thumb: minimum 2-4")),
    _ratio("fixed_charge_coverage", "Fixed charge coverage", "solvency",
           "(EBIT + Lease payments) / (Interest + Lease payments)",
           ["operating_income", "lease_payments", "interest_expense"],
           lambda v: (v["operating_income"] + v["lease_payments"]) / (v["interest_expense"] + v["lease_payments"]),
           _src((CFA, "#34"))),
    _ratio("cfo_to_interest", "CFO to interest", "solvency", "(CFO + interest and taxes paid) / Interest expense",
           ["cfo", "interest_paid", "taxes_paid", "interest_expense"],
           lambda v: (v["cfo"] + v["interest_paid"] + v["taxes_paid"]) / v["interest_expense"],
           _src((DUKE, "p5")), (">", Decimal(2), "Duke p5 rule of thumb: >= 2-4")),
    _ratio("cfo_to_debt", "CFO to debt", "solvency", "(CFO + interest and taxes paid) / Average total liabilities",
           ["cfo", "interest_paid", "taxes_paid", "avg:total_liabilities"],
           lambda v: (v["cfo"] + v["interest_paid"] + v["taxes_paid"]) / v["avg:total_liabilities"],
           _src((DUKE, "p5"))),
    _ratio("cash_flow_adequacy", "Cash flow adequacy", "solvency", "CFO / (Capex + debt repayments + dividends)",
           ["cfo", "capex", "opt:debt_repayments", "opt:dividends_paid"],
           lambda v: v["cfo"] / (v["capex"] + v["opt:debt_repayments"] + v["opt:dividends_paid"]),
           _src((DUKE, "p5")), (">", Decimal(1), "Duke p5 rule of thumb: 1")),
    _ratio("cfo_to_operating_income", "CFO to operating earnings", "solvency", "CFO / Operating income",
           ["cfo", "operating_income"], lambda v: v["cfo"] / v["operating_income"],
           _src((DUKE, "p5")), (">", Decimal(1), "Duke p5 rule of thumb: > 1")),
    _ratio("dividend_payout", "Dividend payout", "shareholder", "Dividends / Net income",
           ["dividends_paid", "net_income_common"], lambda v: v["dividends_paid"] / v["net_income_common"],
           _src((CFA, "#35"), (DUKE, "p4"), (GILL, "p60"))),
    _ratio("retention_rate", "Retention rate", "shareholder", "1 - Dividend payout",
           ["dividends_paid", "net_income_common"], lambda v: 1 - v["dividends_paid"] / v["net_income_common"],
           _src((CFA, "#36"))),
    _ratio("sustainable_growth", "Sustainable growth rate", "shareholder", "Retention rate x ROE",
           ["dividends_paid", "net_income_common", "net_income", "avg:equity"],
           lambda v: (1 - v["dividends_paid"] / v["net_income_common"]) * v["net_income"] / v["avg:equity"],
           _src((CFA, "#37"))),
    _ratio("eps_basic_check", "Earnings per share (basic)", "shareholder", "Net income to common / Weighted average shares",
           ["net_income_common", "shares_basic"], lambda v: v["net_income_common"] / v["shares_basic"],
           _src((CFA, "#38"), (DUKE, "p4"), (CFI, "p32"), (GILL, "p60"))),
    _ratio("book_value_per_share", "Book value per share", "shareholder", "Common equity / Shares outstanding",
           ["equity", "shares_outstanding"], lambda v: v["equity"] / v["shares_outstanding"],
           _src((CFA, "#39"), (DUKE, "p5"))),
    _ratio("fcfe", "Free cash flow to equity", "shareholder", "CFO - Capex + Net borrowing", [], None, _src((CFA, "#40"), (GILL, "p58")),
           unusable=("no_standard_xbrl_concept", "net borrowing needs debt issued minus repaid, tagged differently by every filer")),
    _ratio("fcff", "Free cash flow to the firm", "shareholder", "CFO + Interest x (1 - tax rate) - Capex",
           ["cfo", "interest_expense", "income_tax", "pretax_income", "capex"],
           lambda v: v["cfo"] + v["interest_expense"] * (1 - v["income_tax"] / v["pretax_income"]) - v["capex"],
           _src((CFA, "#41"), (GILL, "p59"))),
    _ratio("price_earnings", "Price-earnings ratio", "market", "Market price / EPS", [], None, _src((DUKE, "p4"), (CFI, "p32")),
           unusable=("not_in_filing", "needs the stock price, which is not in the 10-K's XBRL statements")),
    _ratio("market_to_book", "Market-to-book ratio", "market", "Market value of equity / Book value of equity", [], None,
           _src((DUKE, "p4")), unusable=("not_in_filing", "needs market value of equity, which is not in the 10-K's XBRL statements")),
    _ratio("dividend_yield", "Dividend yield", "market", "Dividends per share / Price per share", [], None,
           _src((DUKE, "p4")), unusable=("not_in_filing", "needs the stock price, which is not in the 10-K's XBRL statements")),
]


# ----------------------------------------------------------------------------
# evaluation
# ----------------------------------------------------------------------------

class Context:
    """Dimensionless facts indexed for lookup at the fiscal-year end / start."""

    def __init__(self, unique_numeric_facts: list):
        self.index = {}
        durations = []
        for f in unique_numeric_facts:
            if f["value"] is None or f["context_ref"] is None or f["context_ref"]["dimensions"]:
                continue
            p = f["context_ref"]["period"]
            self.index.setdefault((f["concept"], tuple(sorted(p.items()))), f)
            if p["type"] == "duration":
                days = (date.fromisoformat(p["end"][:10]) - date.fromisoformat(p["start"][:10])).days
                if 350 <= days <= 380:
                    durations.append((p["end"], p["start"]))
        self.end, self.start = max(durations) if durations else (None, None)

    def find(self, concept, kind, at):
        if self.end is None:
            return None
        if kind == "duration":
            if at != "end":
                return None
            period = {"type": "duration", "start": self.start, "end": self.end}
        else:
            period = {"type": "instant", "instant": self.end if at == "end" else self.start}
        return self.index.get((concept, tuple(sorted(period.items()))))


def _resolve(term, at, ctx, inputs):
    spec = TERMS[term]
    if "sum" in spec:
        total = Decimal(0)
        for part, mode in spec["sum"]:
            try:
                total += _resolve(part, at, ctx, inputs)
            except Missing as m:
                if mode == "req":
                    raise
                inputs.append({"term": part, "at": at, "assumed_zero": True})
        return total
    for concept in spec["concepts"]:
        fact = ctx.find(concept, spec["kind"], at)
        if fact is not None:
            inputs.append({"term": term, "at": at, "concept": concept, "fact_id": fact["fact_id"],
                           "value": fact["value"], "decimals": fact["decimals"]})
            return fact["value"]
    raise Missing(term)


def _evaluate(ratio, ctx):
    result = {
        "id": ratio["id"], "name": ratio["name"], "category": ratio["category"],
        "formula": ratio["formula"], "sources": ratio["sources"],
        "status": None, "reason": None, "detail": None, "value": None, "inputs": [],
        "advisory": None,
    }
    if ratio["unusable"]:
        result.update(status="not_usable", reason=ratio["unusable"][0], detail=ratio["unusable"][1])
        return result
    if ctx.end is None:
        result.update(status="not_usable", reason="missing_terms", detail="no fiscal-year duration found")
        return result

    values, inputs, missing, missing_prior = {}, [], [], []
    for token in ratio["needs"]:
        mode, _, term = token.rpartition(":")
        try:
            if mode == "avg":
                end = _resolve(term, "end", ctx, inputs)
                try:
                    prior = _resolve(term, "prior", ctx, inputs)
                except Missing:
                    missing_prior.append(term)
                    continue
                values[token] = (end + prior) / 2
            elif mode == "prior":
                values[token] = _resolve(term, "prior", ctx, inputs)
            elif mode == "opt":
                try:
                    values[token] = _resolve(term, "end", ctx, inputs)
                except Missing:
                    values[token] = Decimal(0)
                    inputs.append({"term": term, "at": "end", "assumed_zero": True})
            else:
                values[token] = _resolve(term, "end", ctx, inputs)
        except Missing as m:
            if mode == "prior":
                missing_prior.append(m.term)
            else:
                missing.append(m.term)
    result["inputs"] = inputs

    if missing:
        result.update(status="not_usable", reason="missing_terms", detail="no matching fact for: " + ", ".join(sorted(set(missing))))
        return result
    if missing_prior:
        result.update(status="not_usable", reason="needs_prior_period",
                      detail="no prior-year balance for: " + ", ".join(sorted(set(missing_prior))))
        return result
    try:
        result["value"] = ratio["fn"](values)
    except ZeroDivisionError:
        result.update(status="not_usable", reason="division_by_zero", detail="denominator is zero")
        return result

    result["status"] = "computed"
    if ratio["rule_of_thumb"]:
        op, threshold, text = ratio["rule_of_thumb"]
        result["advisory"] = {
            "rule_of_thumb": text,
            "outside_rule_of_thumb": bool(result["value"] < threshold),
            "note": "advisory only; never counted as a filing error",
        }
    if ratio["id"] == "eps_basic_check":
        result["crosscheck"] = _eps_crosscheck(result, ctx)
    return result


def _half_unit(decimals):
    if decimals in (None, "INF"):
        return Decimal(0)
    return Decimal("0.5") * Decimal(10) ** Decimal(-int(decimals))


def _eps_crosscheck(result, ctx):
    """Computed basic EPS must match reported EPS within reporting precision."""
    reported = ctx.find("us-gaap:EarningsPerShareBasic", "duration", "end")
    if reported is None:
        return {"status": "not_checked", "reason": "filing reports no basic EPS"}
    by_term = {i["term"]: i for i in result["inputs"] if "fact_id" in i}
    ni, sh = by_term["net_income_common"], by_term["shares_basic"]
    computed = result["value"]
    tolerance = (_half_unit(reported["decimals"]) + _half_unit(ni["decimals"]) / sh["value"]
                 + abs(computed) * _half_unit(sh["decimals"]) / sh["value"])
    diff = abs(computed - reported["value"])
    return {"status": "consistent" if diff <= tolerance else "inconsistent",
            "computed": computed, "reported": reported["value"], "abs_diff": diff, "tolerance": tolerance,
            "reported_fact_id": reported["fact_id"]}


def compute_ratios(unique_numeric_facts: list) -> dict:
    ctx = Context(unique_numeric_facts)
    results = [_evaluate(r, ctx) for r in RATIOS]
    return {
        "fiscal_period": {"start": ctx.start, "end": ctx.end},
        "results": results,
        "summary": summarize(results),
    }


def summarize(results: list) -> dict:
    computed = [r for r in results if r["status"] == "computed"]
    reasons = {}
    for r in results:
        if r["status"] == "not_usable":
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    by_pdf = {}
    for r in computed:
        for pdf in {s["pdf"] for s in r["sources"]}:
            by_pdf[pdf] = by_pdf.get(pdf, 0) + 1
    checks = [r["crosscheck"] for r in results if r.get("crosscheck")]
    return {
        "ratios_in_library": len(results),
        "computed": len(computed),
        "not_usable": len(results) - len(computed),
        "not_usable_by_reason": reasons,
        "computed_by_source_pdf": by_pdf,
        "outside_rule_of_thumb_advisory": sum(
            1 for r in computed if r["advisory"] and r["advisory"]["outside_rule_of_thumb"]),
        "eps_crosscheck": checks[0]["status"] if checks else None,
    }


# ----------------------------------------------------------------------------
# identities -> constraints usable by check.py / solver.py / inject.py
# ----------------------------------------------------------------------------

def _q(name):
    return "us-gaap:" + name


REVENUES = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]
COSTS = ["CostOfRevenue", "CostOfGoodsAndServicesSold"]
PRETAX = ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
          "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]

# each identity: (id, description, sources, variants); a variant is (total, [(child, weight)]).
# Variants are tried in order per period; the first one whose children are ALL
# reported is used (most specific first), so e.g. Tesla's non-controlling and
# redeemable-NCI lines are included when present.
IDENTITIES = [
    ("gross_profit_identity", "Gross profit = Revenue - Cost of revenue",
     _src((CFA, "#15"), (DUKE, "p4")),
     [("GrossProfit", [(r, 1), (c, -1)]) for r in REVENUES for c in COSTS]),
    ("operating_income_identity", "Operating income = Gross profit - Operating expenses",
     _src((CFA, "#16"), (DUKE, "p4")),
     [("OperatingIncomeLoss", [("GrossProfit", 1), ("OperatingExpenses", -1)])]),
    ("net_income_bridge", "Net income (incl. non-controlling interest) = Pretax income - Income tax",
     _src((CFA, "#25")),
     [(n, [(p, 1), ("IncomeTaxExpenseBenefit", -1)]) for n in ("ProfitLoss", "NetIncomeLoss") for p in PRETAX]),
    ("balance_sheet_balances", "Total assets = Total liabilities and equity",
     _src((CFA, "#28"), (CFA, "#30"), (DUKE, "p5")),
     [("Assets", [("LiabilitiesAndStockholdersEquity", 1)])]),
    ("accounting_equation", "Liabilities and equity = Liabilities + Equity (incl. non-controlling / redeemable interests when reported)",
     _src((CFA, "#31"), (DUKE, "p5")),
     [("LiabilitiesAndStockholdersEquity", [("Liabilities", 1), ("StockholdersEquity", 1), ("MinorityInterest", 1),
                                            ("TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests", 1)]),
      ("LiabilitiesAndStockholdersEquity", [("Liabilities", 1),
                                            ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", 1),
                                            ("TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests", 1)]),
      ("LiabilitiesAndStockholdersEquity", [("Liabilities", 1), ("StockholdersEquity", 1), ("MinorityInterest", 1)]),
      ("LiabilitiesAndStockholdersEquity", [("Liabilities", 1), ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", 1)]),
      ("LiabilitiesAndStockholdersEquity", [("Liabilities", 1), ("StockholdersEquity", 1)])]),
]


def identity_constraints(unique_numeric_facts: list) -> list:
    """Constraint dicts (rule_source 'ratio-identity') for every complete identity instance."""
    facts = [f for f in unique_numeric_facts
             if f["value"] is not None and f["context_ref"] and not f["context_ref"]["dimensions"]]
    fact_by_id = {f["fact_id"]: f for f in unique_numeric_facts}

    templates, meta = {}, {}
    for rid, desc, sources, variants in IDENTITIES:
        for i, (total, children) in enumerate(variants):
            key = (f"{rid}#{i}", _q(total))
            templates[key] = [(_q(c), w) for c, w in children]
            meta[f"{rid}#{i}"] = (rid, desc, sources, i)

    instantiated, _ = constraints_mod.instantiate_filer_calc_constraints(templates, facts)

    chosen = {}
    for c in instantiated:
        if c["partial"]:
            continue
        rid, desc, sources, variant = meta[c["linkrole"]]
        key = (rid, constraints_mod.context_signature(fact_by_id[c["total_fact_id"]]["context_ref"]))
        if key not in chosen or variant < chosen[key][1]:
            chosen[key] = (c, variant, desc, sources)

    out = []
    for (rid, _), (c, variant, desc, sources) in sorted(chosen.items(), key=lambda kv: (kv[0][0], kv[1][0]["total_fact_id"])):
        c = dict(c)
        c.update(
            constraint_id=f"RATIO-{rid}-{c['total_fact_id']}",
            rule_source="ratio-identity",
            ratio_rule_id=rid,
            ratio_rule_description=desc,
            sources=sources,
            variant_index=variant,
        )
        c["spans_sections"] = constraints_mod.compute_spans_sections(c["involved_fact_ids"], fact_by_id)
        out.append(c)
    return out


def analyze(unique_numeric_facts: list) -> dict:
    """Everything the pipeline needs from this file: ratio results + checked identity constraints."""
    ratio_out = compute_ratios(unique_numeric_facts)
    checked = check.check_constraints(identity_constraints(unique_numeric_facts), unique_numeric_facts)
    return {"ratios": ratio_out, "identities": checked}


def ratio_rows(ratio_out: dict) -> list:
    """Flat rows for CSV: one per ratio."""
    rows = []
    for r in ratio_out["results"]:
        rows.append({
            "ratio_id": r["id"], "name": r["name"], "category": r["category"], "formula": r["formula"],
            "status": r["status"], "reason": r["reason"] or "", "detail": r["detail"] or "",
            "value": float(r["value"]) if r["value"] is not None else "",
            "sources": "; ".join(f"{s['pdf']} {s['ref']}" for s in r["sources"]),
            "inputs": "; ".join(
                f"{i['term']}@{i['at']}=" + ("0 (unreported)" if i.get("assumed_zero") else f"{i['concept'].split(':')[1]}[{i['fact_id']}]={i['value']}")
                for i in r["inputs"]),
            "outside_rule_of_thumb": (r["advisory"] or {}).get("outside_rule_of_thumb", ""),
            "eps_crosscheck": (r.get("crosscheck") or {}).get("status", ""),
        })
    return rows


if __name__ == "__main__":
    import sys

    import extract
    import fetch

    target = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    if target.startswith("http"):
        url = target
    else:
        cik = fetch.get_cik_for_ticker(target)
        latest = fetch.get_recent_10k_filings(cik, limit=1)[0]
        url = fetch.build_filing_url(cik, latest["accession_number"], latest["primary_document"])

    print(f"Loading {url} ...")
    model = extract.load_filing(url)
    unique, _ = extract.dedupe_numeric_facts(extract.extract_facts(model))
    out = analyze(unique)
    ro = out["ratios"]

    print(f"\nfiscal year: {ro['fiscal_period']['start'][:10]} .. {ro['fiscal_period']['end'][:10]}\n")
    for r in ro["results"]:
        if r["status"] == "computed":
            print(f"  ok   {r['id']:26s} {float(r['value']):>14.4f}")
        else:
            print(f"  --   {r['id']:26s} {r['reason']}: {r['detail']}")
    print("\nsummary:", ro["summary"])

    ids = out["identities"]
    bad = [c for c in ids if not c["pass_rounding_tolerance"]]
    print(f"\nidentity constraints: {len(ids)} instantiated, {len(bad)} fail the rounding band")
    for c in bad:
        print("  FAIL", c["constraint_id"], "residual", c["residual"], "band", c["band"])
