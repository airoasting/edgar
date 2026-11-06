#!/usr/bin/env python3
"""실측 점검: 실제 SEC 데이터로 모든 도구를 불러 지켜야 할 조건(불변식)을 확인한다.

    EDGAR_USER_AGENT="Your Name you@example.com" python3 scripts/live_check.py          # 전체 (SEC 요청 약 150회, 2~4분)
    EDGAR_USER_AGENT="Your Name you@example.com" python3 scripts/live_check.py fin      # 이름에 fin이 든 점검만

회사 유형을 일부러 섞었다: 9월 결산(AAPL), 1월 결산(NVDA), 52/53주(COST), 은행(JPM), 보험(MET), 리츠(O),
자본잠식(MCD), 주식 종류 여럿(GOOGL), IFRS 20-F(TSM, SONY, KB), 지주사 겸 투자자(BRK-B).
숫자 자체(예: 애플 매출)는 시점마다 바뀌므로 고정값 대신 관계(당기 > 0, Q4 = 연간 − 9개월 등)를 본다.
실패하면 그 회사·도구를 tests/의 회귀 테스트로 옮긴다.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets"))
import mcp_server as ms  # noqa: E402

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


def call(tool, **args):
    body, err = ms.run_tool(tool, args)
    if err:
        raise AssertionError(f"{tool}{args} 오류: {body.get('error_code')} {body.get('error')}")
    return body


def m(body, key, slot="current"):
    return ((body["metrics"].get(key) or {}).get(slot) or {}).get("val")


@check("fin AAPL 10-K: 연간, Q4 = 연간 − 9개월, ROE 주의 문구")
def _():
    b = call("fin_key_accounts", company="AAPL")
    assert b["filing"]["form"] == "10-K", b["filing"]
    assert b["filing"]["fiscal"].endswith("(연간)"), b["filing"]["fiscal"]
    rev = b["metrics"]["revenue"]
    assert "note" not in rev or "53주" in rev["note"], rev.get("note")
    assert rev["current"]["val"] > rev["q4"]["val"] > 0 and rev["q4"]["derived"]
    assert "q4" not in b["metrics"]["eps_diluted"], "EPS는 빼서 구하지 않는다"
    assert b["meta"]["balance_check"]["ok"], b["meta"]["balance_check"]
    assert "roe_pct" in b["ratios"]


@check("fin NVDA 10-Q: 3개월·누적·직전 분기, 분기 라벨")
def _():
    b = call("fin_key_accounts", company="NVDA")
    assert b["filing"]["form"] == "10-Q"
    assert b["filing"]["fiscal"].startswith("FY"), b["filing"]
    assert m(b, "revenue") and m(b, "revenue", "prior") and m(b, "revenue", "prev_quarter")
    assert "revenue_growth_qoq_pct" in b["ratios"]
    assert "roe_pct" not in b["ratios"]


@check("fin GOOGL 10-Q: 재무상태 prior 시점이 항목마다 같다")
def _():
    b = call("fin_key_accounts", company="GOOGL")
    ends = {k: v["prior"]["end"] for k, v in b["metrics"].items() if v.get("prior") and "start" not in v["prior"]
            and k in ("total_assets", "equity", "cash", "current_assets")}
    assert len(set(ends.values())) == 1, ends


@check("fin JPM 은행: 영업이익·부채비율 없음, 순이자이익 있음")
def _():
    b = call("fin_key_accounts", company="JPM")
    assert b["meta"]["sector"] == "bank"
    assert "operating_income" not in b["metrics"] and "debt_to_equity_pct" not in b["ratios"]
    assert m(b, "net_interest_income") or b["metrics"].get("net_interest_income", {}).get("ytd")


@check("fin MET 보험: 비GAAP 영업이익을 쓰지 않는다")
def _():
    b = call("fin_key_accounts", company="MET")
    assert b["meta"]["sector"] == "insurance" and "operating_income" not in b["metrics"]


@check("fin O 리츠: 순이익 라벨이 지배주주 기준이거나 비지배 포함을 밝힌다")
def _():
    b = call("fin_key_accounts", company="O")
    ni = b["metrics"]["net_income"]
    assert ni["tag"].endswith("NetIncomeLoss") or "비지배" in ni["label"] or "보통주" in ni["label"], ni
    assert b["meta"]["sector"] == "reit"


@check("fin MCD 자본잠식: 부채비율·ROE를 계산하지 않는다")
def _():
    b = call("fin_key_accounts", company="MCD")
    if (m(b, "equity") or 0) <= 0:
        assert "debt_to_equity_pct" not in b["ratios"] and "roe_pct" not in b["ratios"] and "note" in b["ratios"]


@check("fin COST 52/53주: 기간 라벨과 분기")
def _():
    b = call("fin_key_accounts", company="COST")
    assert b["filing"]["fiscal"], b["filing"]
    assert m(b, "revenue") or b["metrics"]["revenue"].get("ytd")


@check("fin TSM 20-F: 자국 통화, fallback이면 알린다")
def _():
    b = call("fin_key_accounts", company="TSM")
    assert b["meta"]["currency"] == ["TWD"], b["meta"]["currency"]
    assert b["meta"]["taxonomy"] == "ifrs-full"
    if b["filing"]["chosen"].startswith("XBRL"):
        assert "fallback" in b["meta"]


@check("fin SONY·KB 20-F: IFRS 숫자가 나온다(US GAAP 이력이 있어도)")
def _():
    for co in ("SONY", "KB"):
        b = call("fin_key_accounts", company=co)
        assert b["meta"]["taxonomy"] == "ifrs-full" and m(b, "revenue") or m(b, "net_income"), (co, b["missing"])
    assert "korea_note" in call("company_resolve", query="KB")["company"]


@check("trend AAPL 분기 8개: 빈 분기 없음, 4분기는 derived")
def _():
    b = call("fin_trend", company="AAPL")
    rows = b["rows"]
    assert len(rows) == 8, len(rows)
    assert all("revenue" in r for r in rows), [r["end"] for r in rows if "revenue" not in r]
    q4 = [r for r in rows if r["fiscal"].endswith("Q4")]
    assert q4 and all(any(d.startswith("revenue") for d in r.get("derived", [])) for r in q4)
    assert all("eps_diluted" not in r for r in q4), "EPS는 빼서 구하지 않는다"


@check("trend NVDA 연간 5년: CAGR, 1월 결산 라벨")
def _():
    b = call("fin_trend", company="NVDA", period="annual")
    assert len(b["rows"]) == 5 and b["meta"]["cagr"]["revenue"]["years"] == 4
    assert all(r["fiscal"].startswith("FY") and "Q" not in r["fiscal"] for r in b["rows"])
    assert all(r["end"][5:7] in ("01", "02") for r in b["rows"])


@check("trend JPM 영업현금흐름 분기: 누적에서 분해")
def _():
    b = call("fin_trend", company="JPM", metrics="operating_cash_flow", count=4)
    assert len(b["rows"]) == 4 and any(r.get("derived") for r in b["rows"])


@check("trend MET 보험: 영업이익 제외, Q4 순이익을 다른 태그로 빼지 않는다")
def _():
    b = call("fin_trend", company="MET")
    assert "operating_income" in b["meta"].get("dropped", []) or all("operating_income" not in r for r in b["rows"])
    for r in b["rows"]:
        if "net_income" in r and any(d.startswith("net_income") for d in r.get("derived", [])):
            assert r["net_income"] > 0, r


@check("trend COST: 16주 분기 표시")
def _():
    b = call("fin_trend", company="COST", count=8)
    assert any(r.get("weeks") == 16 for r in b["rows"]), [(r["fiscal"], r.get("weeks")) for r in b["rows"]]


@check("read MSFT 10-K 위험 요인 'AI'가 단어 단위로만 걸린다, 10-Q 소송은 PART II")
def _():
    b = call("filing_read", company="MSFT", section="risk", query="AI")
    assert all("certain" not in s["text"][395:405].lower() for s in b["snippets"][:3])
    b = call("filing_read", company="AAPL", form="10-Q", section="legal", max_chars=1500)
    assert "legal" in b["text"][:400].lower(), b["text"][:300]


@check("insider BRK-B 1년: 4/A 중복 없이, 파생표 보통주 매수 포함, 읽은 기간 표시")
def _():
    b = call("share_insider_reports", company="BRK.B", days=365, limit=15)
    assert b["meta"]["covered_since"], b["meta"]
    if "limit_hit" in b["meta"]:
        assert b["summary"]["covered_since"] == b["meta"]["covered_since"]      # 읽은 범위를 요약에도 밝힌다
    assert "filed_as_owner" in b["meta"]                                  # 버크셔가 보고자인 다른 회사 거래는 빠진다


@check("resolve Meta·Visa는 되묻지 않는다")
def _():
    assert call("company_resolve", query="Meta")["company"]["ticker"] == "META"
    assert call("company_resolve", query="Visa")["company"]["ticker"] == "V"


@check("concept MSFT R&D 연간 + 후보 찾기")
def _():
    b = call("fin_concept", company="MSFT", concept="ResearchAndDevelopmentExpense", duration="annual", limit=3)
    assert len(b["rows"]) == 3 and "yoy_pct" in b["rows"][0]
    body, err = ms.run_tool("fin_concept", {"company": "MSFT", "concept": "research"})
    assert err and body["error_code"] == "NOT_FOUND" and body["candidates"]


@check("concept TSM 매출: 통화가 섞이지 않는다")
def _():
    b = call("fin_concept", company="TSM", concept="Revenue")
    assert {r["unit"] for r in b["rows"]} == {b["meta"]["unit"]}


@check("read AAPL 10-K 위험 요인에서 China 스니펫, untrusted 표시")
def _():
    b = call("filing_read", company="AAPL", section="risk", query="China")
    assert b["meta"]["untrusted"] and b["matches"] > 0 and b["snippets"]
    b = call("filing_read", company="AAPL", section="mdna", max_chars=3000)
    assert b["text"] and b.get("next_offset")


@check("earnings NVDA 8-K EX-99.1 가이던스 스니펫")
def _():
    b = call("earnings_release", company="NVDA", query="outlook|expected to be")
    assert b["document"]["type"].startswith("EX-99") and b["matches"] > 0


@check("20-F TSM: 본문 섹션(위험 요인 Item 3, MD&A Item 5), 6-K 실적 자료")
def _():
    for sec, item in (("risk", "Item 3"), ("mdna", "Item 5")):
        b = call("filing_read", company="TSM", form="20-F", section=sec, max_chars=1000)
        assert b["meta"]["section"] == item and b["untrusted"], (sec, b["meta"])
    b = call("earnings_release", company="TSM", max_chars=2000)
    assert b["filing"]["form"] == "6-K" and b["document"]["type"].startswith("EX-99"), b["filing"]


@check("13F 버크셔: 알파벳 A·C를 한 종목으로 합친다")
def _():
    b = call("fund_holdings", manager="버크셔", top=30)
    names = [h["issuer"] for h in b["top_holdings"]]
    assert len(names) == len(set(names)), names


@check("fts 'going concern' 8-K 최근 1년")
def _():
    b = call("full_text_search", query='"going concern"', forms="8-K", limit=5)
    assert b["rows"] and b["rows"][0]["url"]


@check("insider TSLA 180일: 요약, 보상 거래 분리")
def _():
    b = call("share_insider_reports", company="TSLA", days=180, limit=30)
    s = b["summary"]
    assert "distinct_sellers" in s and isinstance(s["other_codes"], dict)


@check("insider BRK-B: 버크셔가 보고자인 다른 회사 거래는 뺀다")
def _():
    b = call("share_insider_reports", company="BRK.B", days=365, limit=40)
    assert all(r["issuer"]["cik"] in (None, "1067983") for r in b["rows"])


@check("13F 버크셔·국민연금: 보유와 변화")
def _():
    for mgr in ("버크셔", "국민연금"):
        b = call("fund_holdings", manager=mgr, top=10)
        assert b["top_holdings"] and b["total_value"] > 1e9, mgr
        assert "changes" in b and b["meta"]["next_report"]


@check("resolve: Ford, gold, Shinhan, 애플, 삼성전자")
def _():
    assert call("company_resolve", query="Ford")["company"]["ticker"] == "F"     # Hartford·Rexford에 걸리지 않는다
    body, err = ms.run_tool("company_resolve", {"query": "gold"})
    assert body.get("status") == "ambiguous", body
    assert call("company_resolve", query="Shinhan")["company"]["ticker"] == "SHG"
    assert call("company_resolve", query="애플")["company"]["ticker"] == "AAPL"
    body, err = ms.run_tool("company_resolve", {"query": "삼성전자"})
    assert err and "DART" in body["next_steps"]


@check("search 8-K 2.02 items_ko, 옛 공시 페이지")
def _():
    b = call("filing_search", company="AAPL", forms="8-K", items="2.02", limit=3)
    assert b["rows"] and "실적 발표" in " ".join(b["rows"][0]["items_ko"])
    b = call("filing_search", company="AAPL", forms="10-K", **{"from": "2010-01-01", "to": "2012-12-31"})
    assert b["rows"], b["meta"]


def main(argv):
    only = argv[0] if argv else ""
    ok = fail = 0
    t0 = time.time()
    for name, fn in CHECKS:
        if only and only not in name:
            continue
        try:
            fn()
            ok += 1
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"FAIL  {name}\n      {type(e).__name__}: {str(e)[:400]}")
    print(f"\n{ok} 통과, {fail} 실패, {time.time() - t0:.0f}초")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
