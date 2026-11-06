"""지분 공시를 읽는다: Form 4(내부자 거래) 파싱과 요약, 13F(기관 보유) 파싱과 분기 비교. 네트워크를 쓰지 않는다.

신호 점수나 매수·매도 의견은 만들지 않는다. 사실(누가, 언제, 얼마나, 어떤 코드로)만 모은다.
"""
from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from typing import Any

TX_CODE = {"P": "장내·사적 매수", "S": "장내·사적 매도", "A": "보상 부여", "M": "옵션 행사·전환", "F": "세금 납부용 처분",
           "G": "증여", "D": "발행사에 처분", "C": "파생증권 전환", "X": "옵션 행사", "W": "상속", "J": "기타",
           "I": "재량 거래", "K": "스와프", "L": "소량 취득", "Z": "신탁 예치·인출", "E": "단기 파생 만료", "H": "장기 파생 만료",
           "O": "외가격 옵션 행사", "U": "공개매수 처분", "V": "자발 보고"}
OPEN_MARKET = {"P", "S"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _t(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    x = el.find(path)
    if x is None:
        return None
    v = x.findtext("value") if x.find("value") is not None else x.text
    return v.strip() if v and v.strip() else None


def _num(s: str | None) -> float | int | None:
    if s is None:
        return None
    try:
        f = float(s.replace(",", ""))
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _flag(el: ET.Element | None, path: str) -> bool:
    return (_t(el, path) or "").lower() in ("1", "true")


def _tri(el: ET.Element | None, path: str) -> bool | None:
    """있으면 True/False, 항목 자체가 없으면 None(2023년 4월 전 서식에는 10b5-1 칸이 없다)."""
    v = _t(el, path)
    if v is None:
        return None
    return v.lower() in ("1", "true")


COVER_QTY = __import__("re").compile(r"(?:approximately\s+)?([\d,]+(?:\.\d+)?)\s+(?:of\s+(?:these|the|such)\s+)?shares\s+(?:were\s+)?(?:sold|withheld)\s+to\s+(?:cover|satisfy)")
COVER = ("sell-to-cover", "sell to cover", "tax withholding", "withholding tax", "satisfy tax", "satisfy the tax",
         "cover tax", "cover the tax", "tax obligation")


def parse_form4(xml: str) -> dict:
    root = ET.fromstring(xml)
    issuer = {"cik": (root.findtext("issuer/issuerCik") or "").strip().lstrip("0") or None,
              "name": (root.findtext("issuer/issuerName") or "").strip() or None,
              "ticker": (root.findtext("issuer/issuerTradingSymbol") or "").strip() or None}
    owners = []
    for o in root.findall("reportingOwner"):
        rel = o.find("reportingOwnerRelationship")
        roles = [lbl for tag, lbl in (("isDirector", "이사"), ("isOfficer", "임원"), ("isTenPercentOwner", "10% 주주"),
                                      ("isOther", "기타")) if _flag(rel, tag)]
        owners.append({"name": _t(o, "reportingOwnerId/rptOwnerName"), "cik": _t(o, "reportingOwnerId/rptOwnerCik"),
                       "roles": roles, "title": _t(rel, "officerTitle")})
    plan = _tri(root, "aff10b5One")
    notes = {f.get("id"): " ".join((f.text or "").split()) for f in root.findall("footnotes/footnote")}
    txs = []
    for table, deriv in (("nonDerivativeTable/nonDerivativeTransaction", False),
                         ("derivativeTable/derivativeTransaction", True)):
        for t in root.findall(table):
            code = _t(t, "transactionCoding/transactionCode")
            fn_ids = [x.get("id") for x in t.iter() if _local(x.tag) == "footnoteId"]
            # 2023년 4월 이후 서식은 10b5-1 표시를 공시 단위(aff10b5One)로 하고, 이전 공시는 각주에만 적는다
            fn_text = " ".join(notes.get(i, "") for i in fn_ids).lower()
            plan_tx = True if (plan or "10b5-1" in fn_text) else plan
            cover = code in ("S", "F") and any(w in fn_text for w in COVER)
            # "approximately 1,337 shares were sold to cover"처럼 일부만 세금용이면 그 수량만 따로 둔다(TSLA CFO)
            mcov = COVER_QTY.search(fn_text) if cover else None
            cover_qty = _num(mcov.group(1)) if mcov else None
            txs.append({"security": _t(t, "securityTitle"), "date": _t(t, "transactionDate"), "code": code,
                        "code_desc": TX_CODE.get(code or ""), "derivative": deriv,
                        "shares": _num(_t(t, "transactionAmounts/transactionShares")),
                        "price": _num(_t(t, "transactionAmounts/transactionPricePerShare")),
                        "acquired_disposed": _t(t, "transactionAmounts/transactionAcquiredDisposedCode"),
                        "owned_after": _num(_t(t, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                        "direct_indirect": _t(t, "ownershipNature/directOrIndirectOwnership"),
                        "plan_10b5_1": plan_tx, "sell_to_cover": cover or None,
                        "cover_shares": cover_qty if cover_qty and cover_qty < (_num(_t(t, "transactionAmounts/transactionShares")) or 0) else None,
                        "price_note": "가중평균 가격(각주에 가격 범위)" if "weighted average" in fn_text else None})
    return {"period": root.findtext("periodOfReport"), "issuer": issuer, "owners": owners, "transactions": txs,
            "plan_10b5_1": plan}


STOCK_WORDS = ("common", "class a", "class b", "class c", "ordinary")
NON_STOCK = ("option", "unit", "warrant", "right", "note", "swap", "debenture", "preferred")


def _counts_as_stock(t: dict) -> bool:
    """파생표에 실린 거래라도 증권 이름이 보통주면 주식 거래로 본다(BRK Class A 매수가 파생표에 실린 사례)."""
    if not t.get("derivative"):
        return True
    title = (t.get("security") or "").lower()
    return any(w in title for w in STOCK_WORDS) and not any(w in title for w in NON_STOCK)


def summarize_insiders(rows: list[dict], days: int, today: dt.date | None = None, covered_since: str | None = None) -> dict:
    """장내 매수(P)·매도(S)만 모은다. 보상(A)·옵션 행사(M)·세금 처분(F)은 따로 센다.
    정정 공시(4/A)와 원본이 같은 거래를 실으면 한 번만 센다(보고자·날짜·코드·수량·단가가 같으면 같은 거래)."""
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()      # 거래일 기준. 읽은 범위(covered_since)는 접수일이라 거르지 않는다
    seen: set[tuple] = set()
    buyers: dict[str, dict] = {}
    sellers: dict[str, dict] = {}
    other: dict[str, int] = {}
    big_other: list[dict] = []
    plan_sell = 0.0
    plan_unknown = 0.0
    sell_total = 0.0
    for r in rows:
        who = ", ".join(o["name"] or "?" for o in r.get("owners", []))
        role = "; ".join(filter(None, (o.get("title") or "/".join(o.get("roles") or []) for o in r.get("owners", []))))
        for t in r.get("_all_transactions") or r.get("transactions", []):
            if not _counts_as_stock(t) or not t.get("date") or t["date"] < since:
                continue
            key = (who, t["date"], t.get("code"), t.get("shares"), t.get("price"), t.get("security"))
            if key in seen:
                continue
            seen.add(key)
            code, sh, px = t.get("code"), t.get("shares") or 0, t.get("price") or 0
            if code not in OPEN_MARKET or (t.get("sell_to_cover") and not t.get("cover_shares")):
                big_other.append({"name": who, "role": role, "date": t["date"], "code": code,
                                  "code_desc": "세금 납부용 매도" if t.get("sell_to_cover") else t.get("code_desc"),
                                  "shares": sh, "price": px or None})
            if code not in OPEN_MARKET:
                other[code or "?"] = other.get(code or "?", 0) + 1
                continue
            if code == "S" and t.get("sell_to_cover"):
                other["S(세금 납부용 매도)"] = other.get("S(세금 납부용 매도)", 0) + 1
                if not t.get("cover_shares"):
                    continue
                sh = sh - t["cover_shares"]          # 세금용을 뺀 나머지는 장내 매도로 센다
            book = buyers if code == "P" else sellers
            b = book.setdefault(who, {"name": who, "role": role, "shares": 0, "value": 0.0, "trades": 0, "last": t["date"]})
            b["shares"] += sh
            b["value"] += sh * px
            b["trades"] += 1
            b["last"] = max(b["last"], t["date"])
            if code == "S":
                sell_total += sh * px
                if t.get("plan_10b5_1"):
                    plan_sell += sh * px
                elif t.get("plan_10b5_1") is None:
                    plan_unknown += sh * px

    def tidy(book: dict) -> list[dict]:
        return sorted(({**v, "value": round(v["value"])} for v in book.values()), key=lambda x: -x["value"])

    classes = {(t.get("security") or "").strip() for r in rows for t in r.get("transactions", []) if t.get("code") in OPEN_MARKET}
    out: dict[str, Any] = {
        "window_days": days, "since": since, "covered_since": covered_since,
        "largest_other": sorted(big_other, key=lambda x: -(x["shares"] or 0))[:3],
        "buyers": tidy(buyers), "sellers": tidy(sellers),
        "buy_value": round(sum(v["value"] for v in buyers.values())),
        "sell_value": round(sell_total),
        "distinct_buyers": len(buyers), "distinct_sellers": len(sellers),
        "cluster_buy": len(buyers) >= 3,
        "sell_value_under_10b5_1_pct": round(plan_sell / sell_total * 100, 1) if sell_total else None,
        "sell_value_10b5_1_unknown_pct": round(plan_unknown / sell_total * 100, 1) if sell_total and plan_unknown else None,
        "other_codes": other,
    }
    if len(classes) > 1:
        out["classes_note"] = f"주식 종류가 섞여 있습니다({', '.join(sorted(classes))}). shares는 종류를 가리지 않고 더한 수입니다."
    if not buyers and not sellers:
        if covered_since:
            out["note"] = (f"읽은 공시({covered_since} 이후 접수) 안에는 장내 매수·매도가 없습니다. limit에 걸려 일부만 읽었으니 "
                           "limit을 늘려 다시 보세요.")
        else:
            out["note"] = (f"최근 {days}일 안에는 장내 매수·매도가 없습니다. 더 긴 흐름은 days=365로 넓혀 보세요"
                           "(Form 4가 많으면 SEC 요청이 늘어 시간이 걸립니다).")
    return out


# ── 13F ────────────────────────────────────────────────────────────────────

def parse_13f(xml: str, filed: str) -> list[dict]:
    """정보표(infoTable) → 종목별 행. 같은 CUSIP·옵션 구분은 합친다(운용 주체별로 나뉘어 실리기 때문).

    금액(value): 2023-01-03 이후 접수분은 달러, 그 전은 천 달러 단위라서 달러로 맞춘다.
    """
    root = ET.fromstring(xml)
    mult = 1 if filed >= "2023-01-03" else 1000
    agg: dict[tuple, dict] = {}
    for it in root.iter():
        if _local(it.tag) != "infoTable":
            continue
        f = {_local(c.tag): c for c in it}

        def txt(name: str) -> str:
            el = f.get(name)
            return (el.text or "").strip() if el is not None and el.text else ""

        sh_el = f.get("shrsOrPrnAmt")
        shares, sh_type = 0, ""
        if sh_el is not None:
            for c in sh_el:
                if _local(c.tag) == "sshPrnamt":
                    shares = _num(c.text) or 0
                elif _local(c.tag) == "sshPrnamtType":
                    sh_type = (c.text or "").strip()
        key = (txt("cusip"), txt("putCall"))
        row = agg.setdefault(key, {"issuer": txt("nameOfIssuer"), "class": txt("titleOfClass"), "cusip": key[0],
                                   "put_call": key[1] or None, "value": 0, "shares": 0, "share_type": sh_type})
        row["value"] += (_num(txt("value")) or 0) * mult
        row["shares"] += shares
    return list(agg.values())


def by_issuer(rows: list[dict]) -> list[dict]:
    """같은 발행사의 주식 종류(알파벳 A·C, 레나 A·B)를 합친다. CUSIP 앞 6자리가 발행사 코드다. 옵션(put·call)은 따로 둔다."""
    agg: dict[tuple, dict] = {}
    for r in rows:
        key = (r["cusip"][:6], r["put_call"])
        g = agg.setdefault(key, {"issuer": r["issuer"], "cusip6": key[0], "put_call": r["put_call"], "classes": [],
                                 "value": 0, "shares": 0})
        g["value"] += r["value"]
        g["shares"] += r["shares"]
        g["classes"].append({"class": r["class"], "cusip": r["cusip"], "shares": r["shares"], "value": r["value"]})
    for g in agg.values():
        if len(g["classes"]) == 1:
            g["class"] = g["classes"][0]["class"]
            del g["classes"]
    return list(agg.values())


def _nm(s: str) -> str:
    return " ".join(str(s).lower().replace(".", " ").replace(",", " ").split()[:2])


SPLITS = (2, 3, 4, 5, 8, 10, 15, 20, 25, 30, 40, 50)


def _split_ratio(cur: dict, prev: dict) -> int | None:
    """주식 수가 정수배로 뛰고 주당 가치는 그만큼 줄었으면 분할로 본다(KLA 10:1, Booking 25:1 등)."""
    a, b = cur["shares"], prev["shares"]
    if not a or not b or a <= b * 1.8 or not cur["value"] or not prev["value"]:
        return None
    r = a / b
    k = min(SPLITS, key=lambda x: abs(x - r))
    px_ratio = (prev["value"] / b) / (cur["value"] / a)
    if abs(r - k) / k < 0.12 and abs(px_ratio - k) / k < 0.45:
        return k
    return None


def diff_13f(cur: list[dict], prev: list[dict]) -> dict:
    """직전 분기 대비: 신규, 전량 매도, 주식 수 증가·감소. 주식 수 기준이다(금액은 주가 변동이 섞인다)."""
    def key(r: dict) -> tuple:
        return (r.get("cusip6") or r["cusip"], r["put_call"])

    p = {key(r): r for r in prev}
    c = {key(r): r for r in cur}
    new = [c[k] for k in c if k not in p]
    exited = [p[k] for k in p if k not in c]
    # CUSIP이 바뀐 같은 회사(분사·재상장 등)는 신규·청산이 아니라 같은 종목으로 묶는다
    renamed = []
    ex_by_name = {(_nm(r["issuer"]), r["put_call"]): r for r in exited}
    for r in list(new):
        o = ex_by_name.get((_nm(r["issuer"]), r["put_call"]))
        if o:
            new.remove(r)
            exited.remove(o)
            renamed.append({**r, "shares_prev": o["shares"], "note": "CUSIP이 바뀌었습니다(같은 회사로 보고 묶음)"})
    up, down, splits = [], [], []
    for k in c.keys() & p.keys():
        a, b = c[k]["shares"], p[k]["shares"]
        if a == b or not b:
            continue
        row = {**c[k], "shares_prev": b, "shares_change": a - b, "shares_change_pct": round((a - b) / b * 100, 1)}
        ratio = _split_ratio(c[k], p[k])
        if ratio:
            splits.append({**row, "split_suspected": f"주식 분할로 보입니다(약 {ratio}:1). 주식 수 변화를 매수로 읽지 마세요"})
            continue
        (up if a > b else down).append(row)
    key = lambda r: -abs(r.get("value", 0))  # noqa: E731
    return {"new": sorted(new, key=key), "exited": sorted(exited, key=key), "splits": splits, "cusip_changed": renamed,
            "increased": sorted(up, key=lambda r: -abs(r["shares_change"] * (r["value"] / r["shares"] if r["shares"] else 0))),
            "decreased": sorted(down, key=lambda r: -abs(r["shares_change"] * (r["value"] / r["shares"] if r["shares"] else 0)))}
