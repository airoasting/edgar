"""XBRL companyfacts에서 재무 숫자를 꺼내고 계산한다. 네트워크를 쓰지 않는다(입력은 companyfacts JSON).

원칙
- 한 공시 기준(extract): 당기와 비교 기간을 같은 공시(accession)에서 꺼낸다. 정정 전후가 섞이지 않는다.
- 추이(trend): 같은 기간 값이 여러 공시에 실리면 가장 나중에 낸 값(정정 반영)을 쓴다. 기간 이름은 fy·fp가 아니라
  기간 끝 날짜(end)로 붙인다. companyfacts의 fy·fp는 그 값을 실은 공시의 것이라 비교 기간에도 같은 값이 붙는다.
- 파생값(Q4 = 연간 − 9개월 누적, 누적 현금흐름의 분기 분해)은 derived와 계산식을 남긴다. EPS·재무상태 항목은 빼지 않는다.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

PERIODIC_FORMS = ("10-K", "10-Q", "20-F", "40-F")
ANNUAL_FORMS = ("10-K", "20-F", "40-F")
FIN_TAX = ("us-gaap", "ifrs-full")     # 재무 숫자 분류 체계. dei(표지)·srt(보조)는 공시 판정에 쓰지 않는다
UNIT_PREF = ("USD", "USD/shares", "shares")

# (키, 한국어 이름, 종류, us-gaap 후보, ifrs-full 후보). flow=기간 값, stock=시점 값, eps=주당 값(빼기 불가)
METRICS: list[tuple[str, str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("revenue", "매출", "flow",
     ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
      "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "RevenuesNetOfInterestExpense"),
     ("Revenue", "RevenueFromContractsWithCustomers")),
    ("gross_profit", "매출총이익", "flow", ("GrossProfit",), ("GrossProfit",)),
    ("operating_income", "영업이익", "flow", ("OperatingIncomeLoss",), ("ProfitLossFromOperatingActivities",)),
    ("net_income", "순이익(지배주주)", "flow",
     ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"),
     ("ProfitLossAttributableToOwnersOfParent", "ProfitLoss")),
    ("pretax_income", "세전이익", "flow",
     ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
      "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
     ("ProfitLossBeforeTax",)),
    ("eps_diluted", "희석 EPS", "eps", ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
     ("DilutedEarningsLossPerShare",)),
    ("operating_cash_flow", "영업현금흐름", "flow",
     ("NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
     ("CashFlowsFromUsedInOperatingActivities",)),
    ("capex", "설비투자(CAPEX)", "flow", ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
     ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",)),
    ("total_assets", "자산총계", "stock", ("Assets",), ("Assets",)),
    ("total_liabilities", "부채총계", "stock", ("Liabilities",), ("Liabilities",)),
    ("equity", "자본총계(지배주주)", "stock",
     ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
     ("EquityAttributableToOwnersOfParent", "Equity")),
    ("current_assets", "유동자산", "stock", ("AssetsCurrent",), ("CurrentAssets",)),
    ("current_liabilities", "유동부채", "stock", ("LiabilitiesCurrent",), ("CurrentLiabilities",)),
    ("cash", "현금성자산", "stock",
     ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
     ("CashAndCashEquivalents",)),
]
# 업종 전용 지표 (sector가 맞을 때만 꺼낸다)
SECTOR_METRICS: dict[str, list[tuple[str, str, str, tuple[str, ...], tuple[str, ...]]]] = {
    "bank": [("net_interest_income", "순이자이익", "flow", ("InterestIncomeExpenseNet",),
              ("InterestRevenueExpense", "NetInterestIncome")),     # RevenueFromInterest는 이자수익 총액이라 쓰지 않는다(KB)
             ("provision_credit_losses", "대손충당금 전입", "flow",
              ("ProvisionForLoanLeaseAndOtherLosses", "FinancingReceivableExcludingAccruedInterestCreditLossExpenseReversal",
               "ProvisionForLoanAndLeaseLosses", "CreditLossExpenseReversal", "ProvisionForCreditLosses"), ())],
}
# 업종에서 뜻이 없거나 다른 뜻으로 쓰이는 지표. 보험사의 OperatingIncomeLoss는 비GAAP 세그먼트 합계로 쓰이기도 한다(MET)
SECTOR_SKIP = {"bank": {"gross_profit", "operating_income", "current_assets", "current_liabilities"},
               "insurance": {"gross_profit", "operating_income", "current_assets", "current_liabilities"}}
SECTOR_NOTE = {"bank": "은행은 매출총이익·영업이익·유동 구분이 없습니다. 순이자이익·대손충당금 전입·세전이익을 봅니다. 부채비율은 뜻이 없어 뺐습니다.",
               "insurance": "보험사는 매출총이익·영업이익이 GAAP 항목이 아니라서 뺐습니다(회사가 쓰는 '영업이익'은 비GAAP일 수 있습니다). 세전이익을 봅니다.",
               "reit": "리츠의 핵심 지표 FFO·AFFO는 XBRL 표준 항목이 아니라서 없습니다. 실적 발표(earnings_release)에서 확인합니다."}
METRIC = {m[0]: m for m in METRICS + [x for v in SECTOR_METRICS.values() for x in v]}


def sector_of(sic: Any) -> str | None:
    """SIC 코드 → 업종 구분 (bank, insurance, reit). 나머지는 None."""
    try:
        n = int(str(sic))
    except (TypeError, ValueError):
        return None
    if n in (6021, 6022, 6029, 6035, 6036):
        return "bank"
    if 6311 <= n <= 6411:
        return "insurance"
    if n == 6798:
        return "reit"
    return None
# 태그가 바뀌어도 뜻이 같아 전년 대비를 계산해도 되는 지표(매출·현금흐름 등). 순이익·자본은 태그마다 정의가 달라 비교하지 않는다
COMPARABLE_ACROSS_TAGS = {"revenue", "eps_diluted"}      # 세전이익(지분법 포함 여부)·현금(제한 현금)·CAPEX는 태그마다 범위가 다르다
# 같은 기간에 후보 태그가 여럿 있으면 가장 큰 값을 쓰는 지표. 보험·은행은 고객계약 매출(일부)과 총수익(전체)을
# 함께 내므로 첫 후보만 쓰면 일부를 매출로 잘못 말한다(MetLife 24억 vs 771억 달러 사례, edgartools #1138).
PICK_MAX = {"revenue"}


# ── 기간 도우미 ────────────────────────────────────────────────────────────

def days(f: dict) -> int | None:
    if not f.get("start"):
        return None
    return (dt.date.fromisoformat(f["end"]) - dt.date.fromisoformat(f["start"])).days + 1


def kind(d: int | None) -> str:
    """기간 길이로 종류를 정한다. 52/53주 회계연도(364·371일)도 annual에 든다."""
    if d is None:
        return "instant"
    if 80 <= d <= 100:
        return "quarter"
    if 140 <= d <= 229:
        return "h1"           # 6개월 누적
    if 230 <= d <= 329:
        return "m9"           # 9개월 누적
    if 350 <= d <= 380:
        return "annual"
    return "other"


def is_ytd(k: str) -> bool:
    return k in ("h1", "m9")


def near(a: str, b: str, tol: int = 10) -> bool:
    return abs((dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days) <= tol


def year_before(d: str) -> str:
    return (dt.date.fromisoformat(d) - dt.timedelta(days=364)).isoformat()


def next_day(d: str) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()


def fiscal_label(end: str, fye: str | None) -> str | None:
    """기간 끝 날짜 + 회계연도 말(MMDD) → 'FY2027 Q2'. 52/53주 회계연도는 월 초(7일 이내)를 전달로 본다.

    회사 관행대로 회계연도가 끝나는 해를 FY로 쓴다(NVDA 2026-07-26 끝 분기 = FY2027 Q2, 애플 2025-12-27 = FY2026 Q1).
    """
    if not fye or len(fye) != 4 or not fye.isdigit():
        return None
    e = dt.date.fromisoformat(end)
    if e.day <= 7:
        e -= dt.timedelta(days=e.day)
    fm, fd = int(fye[:2]), int(fye[2:])
    if fd <= 7:
        fm = 12 if fm == 1 else fm - 1
    diff = (e.month - fm) % 12            # 회계연도 말 다음 달부터 몇 달째에 끝났나 (0이면 회계연도 말)
    q = 4 if diff == 0 else (diff - 1) // 3 + 1
    fy = e.year if e.month <= fm else e.year + 1
    return f"FY{fy} Q{q}"


# ── 단위와 태그 고르기 ─────────────────────────────────────────────────────

def pick_unit(units: dict, accn: str | None, foreign: bool) -> str | None:
    """값이 있는 단위. 20-F 기업은 자국 통화(TWD, EUR 등)로 내고 USD 환산값을 일부만 붙이므로,
    외국 기업이면 (이 공시 안에서) 값이 가장 많은 단위를 고른다."""
    count = {u: sum(1 for f in vals if accn is None or f.get("accn") == accn) for u, vals in units.items()}
    if foreign:
        return max((u for u in count if count[u] and u != "pure"), key=lambda u: count[u], default=None)
    for u in UNIT_PREF:
        if count.get(u):
            return u
    return None


def taxonomy_of(facts: dict, accn: str | None = None) -> str:
    """분류 체계. accn을 주면 그 공시에 숫자가 더 많은 쪽을 고른다. US GAAP에서 IFRS로 바꾼 외국 기업(SONY, KB)은
    회사 전체로 보면 us-gaap 이력이 있어 IFRS 공시를 못 읽는다."""
    f = facts.get("facts", {})
    if accn:
        count = {t: sum(1 for g in f.get(t, {}).values() for vals in g.get("units", {}).values()
                        for x in vals if x.get("accn") == accn) for t in FIN_TAX}
        if any(count.values()):
            return max(count, key=lambda t: count[t])
    return "us-gaap" if "us-gaap" in f else "ifrs-full"


def candidates(facts: dict, key: str, accn: str | None, foreign: bool, tax: str | None = None) -> list[tuple[str, str, list, list]]:
    """지표 후보 태그 중 값이 있는 것들: [(태그, 단위, 이 공시 값들, 모든 값)]. 우선순위 순서."""
    tax = tax or taxonomy_of(facts, accn)
    tags = METRIC[key][3] if tax == "us-gaap" else METRIC[key][4]
    g_all = facts.get("facts", {}).get(tax, {})
    out = []
    for tag in tags:
        g = g_all.get(tag)
        if not g:
            continue
        u = pick_unit(g.get("units", {}), accn, foreign)
        if not u:
            continue
        # 정기보고서 값만 쓴다. 위임장(DEF 14A)의 '보수와 성과' 표도 NetIncomeLoss 태그를 쓰는데 정의가 다르고,
        # 가장 늦게 낸 값이라 10-K 값을 덮어쓴다(MET FY2025 순이익 31.73억 vs 10-K 33.79억)
        allv = [f for f in g["units"][u] if str(f.get("form", "")).removesuffix("/A") in PERIODIC_FORMS]
        inacc = [f for f in allv if accn is None or f.get("accn") == accn]
        if inacc:
            out.append((f"{tax}:{tag}", u, inacc, allv))
    return out


def has_facts(facts: dict, accn: str) -> bool:
    """재무 숫자(us-gaap·ifrs-full)가 이 공시에 있나. 표지 정보(dei)만 있으면 없는 것으로 본다."""
    return any(f.get("accn") == accn for name, tax in facts.get("facts", {}).items() if name in FIN_TAX
               for g in tax.values() for vals in g.get("units", {}).values() for f in vals)


def latest_fact_filing(facts: dict) -> dict | None:
    """companyfacts에 실린 가장 최근 정기보고서 숫자의 공시 (최신 공시가 아직 반영 안 됐을 때 쓴다)."""
    best = None
    for name, tax in facts.get("facts", {}).items():
        if name not in FIN_TAX:
            continue
        for g in tax.values():
            for vals in g.get("units", {}).values():
                for f in vals:
                    if str(f.get("form", "")) in PERIODIC_FORMS and (best is None or f["filed"] > best["filed"]):
                        best = f
    return best


def find_accession(facts: dict, fy: str, fp: str) -> dict | None:
    """fy·fp로 공시를 찾는다. 원본(정정 아님)을, 그중 먼저 낸 것을 고른다."""
    seen: dict[str, dict] = {}
    for name, tax in facts.get("facts", {}).items():
        if name not in FIN_TAX:
            continue
        for g in tax.values():
            for vals in g.get("units", {}).values():
                for f in vals:
                    if str(f.get("fy")) == str(fy) and f.get("fp") == fp and \
                            str(f.get("form", "")).removesuffix("/A") in PERIODIC_FORMS:
                        seen.setdefault(f["accn"], f)
    if not seen:
        return None
    return sorted(seen.values(), key=lambda x: (str(x["form"]).endswith("/A"), x["filed"]))[0]


def period_end(facts: dict, accn: str) -> str | None:
    """공시의 기간 끝. 이 공시에 실린 기간 값 중 가장 늦은 끝 날짜."""
    ends = [f["end"] for name, tax in facts.get("facts", {}).items() if name in FIN_TAX
            for g in tax.values() for vals in g.get("units", {}).values() for f in vals
            if f.get("accn") == accn and f.get("start")]
    return max(ends) if ends else None


def _v(f: dict | None, **extra: Any) -> dict | None:
    if not f:
        return None
    out = {"val": f["val"], "end": f["end"]}
    if f.get("start"):
        out["start"] = f["start"]
    return {**out, **extra}


# ── 한 공시 기준 추출 ──────────────────────────────────────────────────────

def _flow_entry(inacc: list, allv: list, pend: str, annual: bool, typ: str) -> dict:
    cur = [f for f in inacc if near(f["end"], pend, 3)]
    want = "annual" if annual else "quarter"
    current = next((f for f in cur if kind(days(f)) == want), None)
    if not current and not annual:       # 12주 분기를 쓰는 회사의 4번째 분기(16주) 같은 비정형 분기
        current = next((f for f in cur if kind(days(f)) == "other" and 100 < (days(f) or 0) <= 125), None)
    ytd = None if annual else max((f for f in cur if is_ytd(kind(days(f)))), key=days, default=None)

    def same_len_year_before(ref: dict | None) -> dict | None:
        if not ref:
            return None
        d = days(ref) or 0
        return next((f for f in inacc if f["end"] < pend and near(f["end"], year_before(pend))
                     and abs((days(f) or 0) - d) <= 7), None)

    prior = same_len_year_before(current)
    entry: dict[str, Any] = {"current": _v(current), "prior": _v(prior)}
    if prior:
        # 비교 기간 값이 처음 공시 때와 다르면(재작성·재분류) 원래 값을 함께 준다
        first = min((f for f in allv if f.get("start") == prior.get("start") and f["end"] == prior["end"]),
                    key=lambda f: f["filed"], default=None)
        if first and first["val"] != prior["val"] and first["filed"] < prior.get("filed", ""):
            entry["prior_restated"] = {"original": first["val"], "original_filed": first["filed"],
                                       "note": "비교 기간 값이 처음 공시 때와 다릅니다(재작성·재분류). 증감률은 이 공시의 재작성 값 기준입니다"}
    if current and not annual and (days(current) or 0) > 100:
        entry["note"] = f"이 분기는 {round((days(current) or 0) / 7)}주입니다(회사 회계 달력)"
    if annual and current and (days(current) or 0) >= 370:
        entry["note"] = "이 회계연도는 53주입니다(전년보다 1주 깁니다)"
    if ytd:
        entry["ytd"], entry["ytd_prior"] = _v(ytd), _v(same_len_year_before(ytd))
        if not current:
            entry["note"] = "이 공시에는 3개월 값이 없고 회계연도 누적(ytd)만 있습니다"
    if current and not annual and typ != "stock":
        # 직전 분기(3개월): 앞 공시의 3개월 값, 없으면 이 공시의 6개월 누적 − 이번 분기(2분기일 때)
        pstart = (dt.date.fromisoformat(current["start"]) - dt.timedelta(days=1)).isoformat()
        prev = max((f for f in allv if kind(days(f)) == "quarter" and near(f["end"], pstart, 5)),
                   key=lambda f: f["filed"], default=None)
        if prev:
            entry["prev_quarter"] = _v(prev)
        elif typ == "flow" and ytd and kind(days(ytd)) == "h1" and near(ytd["start"], pstart, 100):
            entry["prev_quarter"] = {"val": ytd["val"] - current["val"], "end": pstart, "derived": "6개월 누적 − 이번 분기"}
    if annual and current and typ == "flow":
        # 4분기(3개월) = 연간 − 같은 회계연도 9개월 누적. 9개월 값은 그해 3분기 10-Q에서 온다(가장 나중에 낸 값)
        for ann, slot in ((current, "q4"), (same_len_year_before(current), "q4_prior")):
            if not ann:
                continue
            # 9개월 값은 이 10-K보다 먼저 낸 공시에서만 (뒤에 낸 공시의 재분류 값이 섞이지 않게)
            nine = max((f for f in allv if kind(days(f)) == "m9" and near(f["start"], ann["start"], 3)
                        and f["end"] < ann["end"] and f["filed"] <= (current.get("filed") or "9999")),
                       key=lambda f: f["filed"], default=None)
            if nine:
                q = {"val": ann["val"] - nine["val"], "start": next_day(nine["end"]), "end": ann["end"],
                     "derived": "연간 − 9개월 누적", "sources": [ann.get("accn"), nine.get("accn")]}
                if (dt.date.fromisoformat(ann["end"]) - dt.date.fromisoformat(nine["end"])).days > 100:
                    q["note"] = "4분기가 13주보다 깁니다(53주 회계연도 또는 16주 분기)"
                entry[slot] = q
    return entry


def prior_instant(facts: dict, accn: str, pend: str, tax: str) -> str | None:
    """재무상태 비교 시점을 공시 하나에 한 번만 정한다. 자산총계가 실린 직전 시점(10-K는 전년 말, 10-Q는 직전 회계연도 말).
    항목마다 따로 고르면 자본만 분기 초 잔액(자본변동표)을 집어 기준 시점이 섞인다."""
    g = facts.get("facts", {}).get(tax, {}).get("Assets", {})
    ends = [f["end"] for vals in g.get("units", {}).values() for f in vals
            if f.get("accn") == accn and not f.get("start") and f["end"] < pend and not near(f["end"], pend, 3)]
    return max(ends) if ends else None


def _stock_entry(inacc: list, pend: str, prior_date: str | None) -> dict:
    current = next((f for f in inacc if not f.get("start") and near(f["end"], pend, 3)), None)
    if prior_date:
        prior = next((f for f in inacc if not f.get("start") and near(f["end"], prior_date, 3)), None)
    else:
        pool = [f for f in inacc if not f.get("start") and f["end"] < pend and not near(f["end"], pend, 3)]
        prior = max(pool, key=lambda f: f["end"]) if pool else None
    return {"current": _v(current), "prior": _v(prior)}


def extract(facts: dict, accn: str, form: str, pend_hint: str | None = None, sector: str | None = None) -> dict:
    """한 공시 안의 숫자로 당기·전기(·누적·직전 분기·Q4)를 고른다."""
    tax = taxonomy_of(facts, accn)
    foreign = form.startswith(("20-F", "40-F")) or tax == "ifrs-full"
    pend = pend_hint or period_end(facts, accn)
    if not pend:
        return {"period_end": None, "metrics": {}, "missing": [m[0] for m in METRICS], "taxonomy": tax, "currency": []}
    annual = form.startswith(ANNUAL_FORMS)
    pdate = prior_instant(facts, accn, pend, tax)
    metrics: dict[str, dict] = {}
    missing: list[str] = []
    skipped = sorted(SECTOR_SKIP.get(sector or "", set()))
    currency: set[str] = set()
    for key, label, typ, _us, _ifrs in METRICS + SECTOR_METRICS.get(sector or "", []):
        if key in skipped:
            continue
        best = None
        for tag, unit, inacc, allv in candidates(facts, key, accn, foreign, tax):
            e = _stock_entry(inacc, pend, pdate) if typ == "stock" else _flow_entry(inacc, allv, pend, annual, typ)
            if not e["current"] and not e.get("ytd"):
                continue
            cand = (tag, unit, e)
            if best is None:
                best = cand
                if key not in PICK_MAX:
                    break
            elif e["current"] and best[2]["current"] and abs(e["current"]["val"]) > abs(best[2]["current"]["val"]) * 1.05:
                best = cand
        if not best:
            missing.append(key)
            continue
        tag, unit, e = best
        if "/" not in unit and unit != "shares":
            currency.add(unit)
        if key == "net_income" and tag.endswith(":ProfitLoss"):
            label = "순이익(비지배지분 포함)"
            e["note"] = "지배주주 순이익 태그가 없어 비지배지분을 포함한 순이익을 썼습니다"
        elif key == "net_income" and tag.endswith("AvailableToCommonStockholdersBasic"):
            label = "순이익(보통주 귀속)"
            e["note"] = "지배주주 순이익 태그가 없어 우선주 배당을 뺀 보통주 귀속 순이익을 썼습니다"
        metrics[key] = {"label": label, "tag": tag, "unit": unit, **{k: v for k, v in e.items() if v is not None}}
    _derive_liabilities(facts, accn, pend, pdate, foreign, metrics, missing, tax)
    _add_unfunded_provision(facts, accn, pend, annual, metrics, tax)
    shares = None
    for f in facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", []):
        if f.get("accn") == accn:
            shares = {"val": f["val"], "as_of": f["end"]}
    if shares is None:
        shares = {"val": None, "note": "표지에 주식 종류별로만 실려(예: Class A·C) 합계가 없습니다. 공시 표지를 확인하세요"}
    return {"period_end": pend, "prior_instant": pdate, "taxonomy": tax, "metrics": metrics, "missing": missing,
            "sector_skipped": skipped, "shares_outstanding": shares, "currency": sorted(currency),
            "balance_check": balance_check(facts, accn, pend, foreign, metrics, tax)}


def _instant(facts: dict, tags: tuple[str, ...], accn: str, at: str, foreign: bool, tax: str) -> tuple[str, float] | None:
    for tag in tags:
        g = facts.get("facts", {}).get(tax, {}).get(tag)
        if not g:
            continue
        u = pick_unit(g.get("units", {}), accn, foreign)
        if not u:
            continue
        f = next((x for x in g["units"][u] if x.get("accn") == accn and not x.get("start") and near(x["end"], at, 3)), None)
        if f:
            return tag, f["val"]
    return None


def _add_unfunded_provision(facts: dict, accn: str, pend: str, annual: bool, metrics: dict, tax: str) -> None:
    """CECL 이후 은행은 대손 전입을 대출분과 미사용 약정분 태그로 나눠 싣는다. 대출분만 잡혔으면 둘을 더해 총액으로 맞춘다(BAC).
    같은 기간 값이 있는 칸만 더하고, 하나라도 더하지 못한 칸이 있으면 그 칸에 표시해 섞인 비교를 막는다."""
    m = metrics.get("provision_credit_losses")
    if not m or not m["tag"].endswith("FinancingReceivableExcludingAccruedInterestCreditLossExpenseReversal"):
        return
    g = facts.get("facts", {}).get(tax, {}).get("OffBalanceSheetCreditLossLiabilityCreditLossExpenseReversal")
    if not g:
        return
    vals = g.get("units", {}).get("USD", [])
    added = 0
    for slot in ("current", "prior", "ytd", "ytd_prior", "prev_quarter", "q4", "q4_prior"):
        e = m.get(slot)
        if not e or e.get("derived") and slot.startswith("q4"):
            if e and slot.startswith("q4"):
                e["note"] = "대출분만의 계산값입니다(미사용 약정분 미포함)"
            continue
        add = max((f for f in vals if f.get("start") == e.get("start") and f["end"] == e["end"]),
                  key=lambda f: f["filed"], default=None)
        if add:
            e["val"] += add["val"]
            e["derived"] = "대출분 + 미사용 약정분"
            added += 1
        else:
            e["note"] = "미사용 약정분 값이 없어 대출분만입니다. 다른 칸과 비교할 때 주의하세요"
    if added:
        m["label"] = "대손충당금 전입(대출분 + 미사용 약정분)"


TEMP_EQUITY = ("TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
               "TemporaryEquityCarryingAmountAttributableToParent", "RedeemableNoncontrollingInterestEquityCarryingAmount")
EQUITY_ALL = ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "StockholdersEquity")


def _derive_liabilities(facts: dict, accn: str, pend: str, pdate: str | None, foreign: bool, metrics: dict,
                        missing: list, tax: str) -> None:
    """부채총계를 따로 내지 않는 회사가 많다: 부채와자본총계 − 자본총계(비지배 포함) − 메자닌 자본으로 계산한다."""
    if "total_liabilities" in metrics or tax != "us-gaap":
        return

    def at(d: str | None) -> tuple[float, str] | None:
        if not d:
            return None
        le = _instant(facts, ("LiabilitiesAndStockholdersEquity",), accn, d, foreign, tax)
        eq = _instant(facts, EQUITY_ALL, accn, d, foreign, tax)
        if not (le and eq):
            return None
        temp = _instant(facts, TEMP_EQUITY, accn, d, foreign, tax)
        return le[1] - eq[1] - (temp[1] if temp else 0), f"LiabilitiesAndStockholdersEquity − {eq[0]}" + (f" − {temp[0]}" if temp else "")

    cur = at(pend)
    if not cur:
        return
    entry = {"label": "부채총계", "tag": "계산: " + cur[1], "unit": "USD", "current": {"val": cur[0], "end": pend},
             "derived": cur[1]}
    pri = at(pdate)
    if pri:
        entry["prior"] = {"val": pri[0], "end": pdate}
    metrics["total_liabilities"] = entry
    if "total_liabilities" in missing:
        missing.remove("total_liabilities")


def balance_check(facts: dict, accn: str, pend: str, foreign: bool, metrics: dict, tax: str) -> dict | None:
    """자산 = 부채 + 자본(비지배 포함) + 메자닌 자본 이 1% 안에서 맞는지. 태그를 잘못 골랐을 때 먼저 드러난다."""
    a = ((metrics.get("total_assets") or {}).get("current") or {}).get("val")
    lia = metrics.get("total_liabilities") or {}
    if a is None or not lia.get("current") or lia.get("derived"):
        return None            # 부채를 계산으로 만들었으면 항등식이라 점검 의미가 없다
    eq_all = _instant(facts, EQUITY_ALL[:1] if tax == "us-gaap" else ("Equity",), accn, pend, foreign, tax)
    eq_val = eq_all[1] if eq_all else ((metrics.get("equity") or {}).get("current") or {}).get("val")
    if eq_val is None:
        return None
    temp = _instant(facts, TEMP_EQUITY, accn, pend, foreign, tax) if tax == "us-gaap" else None
    total = lia["current"]["val"] + eq_val + (temp[1] if temp else 0)
    gap = (a - total) / a * 100 if a else 0
    return {"ok": abs(gap) <= 1.0, "gap_pct": round(gap, 3),
            "note": None if abs(gap) <= 1.0 else "자산 ≠ 부채 + 자본. 태그 선택이나 비지배지분 처리를 원문에서 확인하세요"}


# ── 비율 ───────────────────────────────────────────────────────────────────

def _pct(n: float | None, d: float | None) -> float | None:
    if n is None or d is None or d == 0:
        return None
    return round(n / d * 100, 2)


def growth(cur: float | None, prev: float | None) -> float | None:
    """전년 대비 증감률(%). 이전 값이 0이거나 부호가 바뀌면 계산하지 않는다(흑자 전환 등은 turn으로 말한다)."""
    if cur is None or prev is None or prev == 0 or (cur < 0) != (prev < 0):
        return None
    return round((cur - prev) / abs(prev) * 100, 2)


def turn(cur: float | None, prev: float | None) -> str | None:
    if cur is None or prev is None:
        return None
    if prev < 0 <= cur:
        return "흑자전환"
    if prev >= 0 > cur:
        return "적자전환"
    if prev < 0 and cur < 0:
        return "적자지속"
    return None


def ratios(m: dict, annual: bool, sector: str | None = None) -> dict:
    """공시 숫자로 직접 계산한 지표. SEC가 주는 값이 아니다."""
    def cur(k: str) -> float | None:
        return ((m.get(k) or {}).get("current") or {}).get("val")

    def pri(k: str) -> float | None:
        return ((m.get(k) or {}).get("prior") or {}).get("val")

    def pq(k: str) -> float | None:
        return ((m.get(k) or {}).get("prev_quarter") or {}).get("val")

    def ytd(k: str) -> float | None:
        return ((m.get(k) or {}).get("ytd") or {}).get("val")

    eq = cur("equity")
    fin = sector in ("bank", "insurance")
    out: dict[str, Any] = {
        "gross_margin_pct": _pct(cur("gross_profit"), cur("revenue")),
        "operating_margin_pct": _pct(cur("operating_income"), cur("revenue")),
        "pretax_margin_pct": _pct(cur("pretax_income"), cur("revenue")) if fin else None,
        "net_margin_pct": _pct(cur("net_income"), cur("revenue")),
        "revenue_growth_yoy_pct": growth(cur("revenue"), pri("revenue")),
        "revenue_growth_qoq_pct": growth(cur("revenue"), pq("revenue")),
        "operating_income_growth_yoy_pct": growth(cur("operating_income"), pri("operating_income")),
        "net_income_growth_yoy_pct": growth(cur("net_income"), pri("net_income")),
        "eps_growth_yoy_pct": growth(cur("eps_diluted"), pri("eps_diluted")),
        "debt_to_equity_pct": _pct(cur("total_liabilities"), eq) if eq and eq > 0 and not fin else None,
        "current_ratio_pct": _pct(cur("current_assets"), cur("current_liabilities")) if not fin else None,
    }
    turns = {k: turn(cur(k), pri(k)) for k in ("operating_income", "net_income")}
    turns = {k: v for k, v in turns.items() if v}
    if turns:
        out["turn"] = turns
    ocf, cap = cur("operating_cash_flow"), cur("capex")
    if ocf is not None and cap is not None:
        out["free_cash_flow"] = ocf - cap
    elif ytd("operating_cash_flow") is not None and ytd("capex") is not None:
        out["free_cash_flow_ytd"] = ytd("operating_cash_flow") - ytd("capex")
    if annual and cur("net_income") is not None and eq and eq > 0:
        eqp = pri("equity")
        if eqp and eqp > 0:
            out["roe_pct"], out["roe_basis"] = _pct(cur("net_income"), (eq + eqp) / 2), "기초·기말 평균 자본"
        else:
            out["roe_pct"], out["roe_basis"] = _pct(cur("net_income"), eq), "기말 자본(기초 자본 없음)"
    if eq is not None and eq <= 0:
        out["note"] = "자본이 0 이하(자본잠식, 자사주 매입 누적 등)라 부채비율·ROE를 계산하지 않았습니다"
    elif out.get("roe_pct") and out["roe_pct"] > 60:
        out["note"] = "ROE가 매우 높은 것은 자사주 매입 등으로 자본이 작아서일 수 있습니다. 순이익률·자산 규모와 함께 읽으세요"
    return {k: v for k, v in out.items() if v is not None}


# ── 추이 ───────────────────────────────────────────────────────────────────

def _series_raw(facts: dict, key: str, foreign: bool, tax: str | None = None) -> tuple[dict[tuple, dict], list[str]]:
    """기간(start, end) → 값. 같은 태그 안에서는 가장 나중에 낸 값(정정 반영)을 쓴다.
    태그 사이에서는 '가장 최근 기간을 실은 태그'(기준 태그)를 모든 기간에 먼저 쓰고, 그 태그에 값이 없는 기간만
    우선순위대로 채운다. 회사가 태그를 바꾸면 다음 공시가 비교 기간까지 새 태그로 다시 싣기 때문에 정의가 이어지고(O),
    다른 표의 태그(현금흐름표의 제한 현금 포함 현금)가 기간마다 섞이지 않는다(COST). 매출은 한 태그가 5% 넘게 크면 그 합계를 쓴다."""
    pick_max = key in PICK_MAX
    order: list[str] = []
    per: dict[tuple, dict[str, dict]] = {}
    for tag, unit, _inacc, allv in candidates(facts, key, None, foreign, tax):
        order.append(tag)
        for f in allv:
            k = (f.get("start"), f["end"])
            cur = per.setdefault(k, {}).get(tag)
            if cur is None or f["filed"] > cur["filed"]:
                per[k][tag] = {**f, "tag": tag, "unit": unit}
    if not per:
        return {}, []
    latest_end = max(k[1] for k in per)
    dominant = next((t for t in order if any(t in v for k, v in per.items() if k[1] == latest_end)), order[0])
    merged: dict[tuple, dict] = {}
    for k, by_tag in per.items():
        best = by_tag.get(dominant) or next(by_tag[t] for t in order if t in by_tag)
        if pick_max:
            big = max(by_tag.values(), key=lambda f: abs(f["val"]))
            if abs(big["val"]) > abs(best["val"]) * 1.05:
                best = big
        merged[k] = best
    used = sorted({f["tag"] for f in merged.values()})
    return merged, used


def _sum_q(qs: list[dict]) -> float:
    return sum(q["val"] for q in qs)


def _weeks(d: int | None) -> int | None:
    return round(d / 7) if d else None


def trend(facts: dict, keys: list[str], mode: str, count: int, fye: str | None, foreign: bool,
          tax: str | None = None) -> dict:
    """지표별 기간 값 → 표. mode=quarter면 단독 분기(파생 포함), annual이면 회계연도."""
    cols: dict[str, dict[str, dict]] = {}       # 지표 → end → {val, tag, days, derived?}
    tags_used: dict[str, list[str]] = {}
    for key in keys:
        typ = METRIC[key][2]
        raw, used = _series_raw(facts, key, foreign, tax)
        tags_used[key] = used
        by_end: dict[str, dict] = {}
        if typ == "stock":
            for (s, e), f in raw.items():
                if s is None:
                    by_end[e] = {"val": f["val"], "tag": f["tag"]}
        elif mode == "annual":
            for (s, e), f in raw.items():
                if kind(days(f)) == "annual":
                    by_end[e] = {"val": f["val"], "tag": f["tag"], "days": days(f)}
        else:
            by_end = _quarters(raw, additive=(typ == "flow"), cross_tag=key in COMPARABLE_ACROSS_TAGS)
        cols[key] = by_end
    flow_ends = {e for k, c in cols.items() if METRIC[k][2] != "stock" for e in c}
    ends = sorted(flow_ends or {e for c in cols.values() for e in c}, reverse=True)
    ends = _dedupe_near(ends)[:count]
    normal = (52,) if mode == "annual" else (12, 13)      # 12주 분기(COST 1~3분기)는 정상, 14주(53주 연도의 한 분기)는 표시
    rows = []
    for e in ends:
        lab = fiscal_label(e, fye)
        row: dict[str, Any] = {"end": e, "fiscal": (lab.split()[0] if lab and mode == "annual" else lab)}
        derived, notes = [], []
        for key in keys:
            v = _lookup(cols[key], e)
            if v is None:
                continue
            row[key] = v["val"]
            if "weeks" not in row and v.get("days") and _weeks(v["days"]) not in normal:
                row["weeks"] = _weeks(v["days"])
            prev = _lookup(cols[key], year_before(e), tol=20)
            if prev is not None and prev.get("tag") != v.get("tag") and key not in COMPARABLE_ACROSS_TAGS:
                notes.append(f"{key}: 전년 같은 기간과 쓴 항목(태그)이 달라 증감률을 계산하지 않았습니다")
            else:
                if prev is not None and prev.get("tag") != v.get("tag"):
                    notes.append(f"{key}: 전년과 태그가 다르지만 같은 뜻(합계)이라 비교했습니다")
                g = growth(v["val"], prev["val"] if prev else None)
                if g is not None:
                    row[f"{key}_yoy_pct"] = g
                pw, cw = _weeks((prev or {}).get("days")), _weeks(v.get("days"))
                if g is not None and pw and cw and pw != cw and "weeks_note" not in row:
                    row["weeks_note"] = f"전년 같은 기간은 {pw}주, 이번은 {cw}주라 증감률에 기간 차이가 섞입니다"
                t = turn(v["val"], prev["val"] if prev else None)
                if t:
                    row[f"{key}_turn"] = t
            if v.get("derived"):
                derived.append(f"{key}: {v['derived']}")
        if derived:
            row["derived"] = derived
        if notes:
            row["notes"] = notes
        rows.append(row)
    shown = {k: sorted({(_lookup(cols[k], r["end"]) or {}).get("tag") for r in rows if k in r} - {None}) for k in keys}
    out: dict[str, Any] = {"rows": rows, "tags": shown}
    changed = {k: v for k, v in shown.items() if len(v) > 1}
    if changed:
        out["tag_changes"] = {k: "여러 태그를 합쳤습니다(회계 기준 변경·태그 교체). 태그가 바뀐 기간끼리는 증감률을 내지 않습니다"
                              for k in changed}
    if mode == "annual" and len(rows) >= 2:
        gaps = [(a["end"], b["end"]) for a, b in zip(rows, rows[1:])
                if (dt.date.fromisoformat(a["end"]) - dt.date.fromisoformat(b["end"])).days > 400]
        if gaps:
            out["gaps"] = [f"{b} 다음 {a} 사이 연도 값이 없습니다" for a, b in gaps]
            return out          # 중간 연도가 비면 CAGR을 계산하지 않는다
        cagr = {}
        for key in keys:
            vals = [(r["end"], r[key]) for r in rows if key in r]
            if len(vals) < 2 or METRIC[key][2] == "stock" or not (vals[0][1] > 0 and vals[-1][1] > 0):
                continue
            if key not in COMPARABLE_ACROSS_TAGS and len({(_lookup(cols[key], x[0]) or {}).get("tag") for x in vals}) > 1:
                continue        # 정의가 바뀐 구간으로 CAGR을 내지 않는다
            years = round((dt.date.fromisoformat(vals[0][0]) - dt.date.fromisoformat(vals[-1][0])).days / 365.25)
            if years >= 1:
                cagr[key] = {"pct": round(((vals[0][1] / vals[-1][1]) ** (1 / years) - 1) * 100, 2), "years": years,
                             "from": vals[-1][0], "to": vals[0][0]}
        if cagr:
            out["cagr"] = cagr
    return out


def _dedupe_near(ends: list[str]) -> list[str]:
    out: list[str] = []
    for e in ends:
        if not out or not near(out[-1], e, 5):
            out.append(e)
    return out


def _lookup(col: dict[str, dict], end: str, tol: int = 10) -> dict | None:
    if end in col:
        return col[end]
    best = None
    for e, v in col.items():
        d = abs((dt.date.fromisoformat(e) - dt.date.fromisoformat(end)).days)
        if d <= tol and (best is None or d < best[0]):
            best = (d, v)
    return best[1] if best else None


def _quarters(raw: dict[tuple, dict], additive: bool, cross_tag: bool = False) -> dict[str, dict]:
    """단독 분기 값. 공시된 3개월(12~16주 포함) 값을 먼저 쓰고, 없으면 누적에서 뺀다(additive일 때만, 같은 태그끼리만).

    Q2 = 6개월 − Q1, Q3 = 9개월 − 6개월(또는 − Q1 − Q2), Q4 = 연간 − 9개월(또는 − Q1 − Q2 − Q3).
    """
    def is_q(f: dict) -> bool:
        d = days(f) or 0
        return kind(d) == "quarter" or 100 < d <= 125      # 16주 분기(COST 4분기)

    q = {e: {"val": f["val"], "tag": f["tag"], "start": s, "days": days(f)} for (s, e), f in raw.items() if is_q(f)}
    if not additive:
        return q
    by_kind: dict[str, list[dict]] = {"h1": [], "m9": [], "annual": []}
    for (s, e), f in raw.items():
        k = kind(days(f))
        if k in by_kind:
            by_kind[k].append(f)

    def same(a: str, b: str) -> bool:
        return cross_tag or a == b

    def q_in(start: str, before: str, tag: str) -> list[dict]:
        return sorted((v | {"end": e} for e, v in q.items() if v.get("start") and same(v["tag"], tag) and
                       dt.date.fromisoformat(start) - dt.timedelta(days=3) <= dt.date.fromisoformat(v["start"])
                       and e < before and not near(e, before, 5)), key=lambda x: x["end"])

    def ytd_at(k: str, start: str, end_lt: str, tag: str) -> dict | None:
        return max((f for f in by_kind[k] if same(f["tag"], tag) and near(f["start"], start, 3) and f["end"] < end_lt
                    and not near(f["end"], end_lt, 5)), key=lambda f: f["end"], default=None)

    for k_total, k_prev, n_prev in (("h1", None, 1), ("m9", "h1", 2), ("annual", "m9", 3)):
        for tot in by_kind[k_total]:
            e = tot["end"]
            if _lookup(q, e, 5):
                continue
            prev = ytd_at(k_prev, tot["start"], e, tot["tag"]) if k_prev else None
            if prev:
                val, how, pend = tot["val"] - prev["val"], f"{_LBL[k_total]} − {_LBL[k_prev]}", prev["end"]
            else:
                qs = q_in(tot["start"], e, tot["tag"])
                if len(qs) != n_prev:
                    continue
                val, how, pend = tot["val"] - _sum_q(qs), f"{_LBL[k_total]} − 앞 분기 {n_prev}개 합", qs[-1]["end"]
            d = (dt.date.fromisoformat(e) - dt.date.fromisoformat(pend)).days
            q[e] = {"val": val, "tag": tot["tag"], "start": None, "derived": how, "days": d}
    return q


_LBL = {"h1": "6개월 누적", "m9": "9개월 누적", "annual": "연간"}
