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


import re as _re
COVER_QTY = (_re.compile(r"(?:approximately\s+)?([\d,]+(?:\.\d+)?)\s+(?:of\s+(?:these|the|such)\s+)?shares\s+(?:were\s+)?"
                         r"(?:sold|withheld)\s+(?:solely\s+)?to\s+(?:cover|satisfy)"),
             _re.compile(r"(?:sold|withheld)\s+(?:approximately\s+)?([\d,]+(?:\.\d+)?)\s+shares\s+(?:[a-z ]{0,30}?)"
                         r"(?:to|in order to)\s+(?:cover|satisfy)"))
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
    cover_left: dict[str, float] = {}       # 각주 하나의 세금용 수량은 그 각주를 단 줄들에 한 번만 나눠 준다
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
            # "approximately 1,337 shares were sold to cover"처럼 일부만 세금용이면 그 수량만 따로 둔다(TSLA CFO).
            # 같은 각주를 여러 줄이 공유하면 앞 줄부터 차례로 배분한다. 수량이 없으면 그 줄 전체를 세금용으로 본다
            cover_qty = None
            shares_ = _num(_t(t, "transactionAmounts/transactionShares")) or 0
            if cover:
                fid = next((i for i in fn_ids if any(w in notes.get(i, "").lower() for w in COVER)), None)
                if fid not in cover_left:
                    m_ = next((p.search(notes.get(fid, "").lower()) for p in COVER_QTY if p.search(notes.get(fid, "").lower())), None)
                    cover_left[fid] = _num(m_.group(1)) if m_ else None
                left = cover_left.get(fid)
                if left is not None:
                    take = min(left, shares_)
                    cover_left[fid] = left - take
                    if take <= 0:
                        cover = False
                    elif take < shares_:
                        cover_qty = take
            txs.append({"security": _t(t, "securityTitle"), "date": _t(t, "transactionDate"), "code": code,
                        "code_desc": ("세금 납부용 매도" if cover and not cover_qty else "매도(일부 세금 납부용)" if cover
                                      else TX_CODE.get(code or "")), "derivative": deriv,
                        "shares": _num(_t(t, "transactionAmounts/transactionShares")),
                        "price": _num(_t(t, "transactionAmounts/transactionPricePerShare")),
                        "acquired_disposed": _t(t, "transactionAmounts/transactionAcquiredDisposedCode"),
                        "owned_after": _num(_t(t, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                        "direct_indirect": _t(t, "ownershipNature/directOrIndirectOwnership"),
                        "plan_10b5_1": plan_tx, "sell_to_cover": cover or None,
                        "cover_shares": cover_qty,
                        "price_note": "가중평균 가격(각주에 가격 범위)" if "weighted average" in fn_text else None})
    return {"period": root.findtext("periodOfReport"), "issuer": issuer, "owners": owners, "transactions": txs,
            "plan_10b5_1": plan}


STOCK_WORDS = ("common", "class a", "class b", "class c", "ordinary")
NON_STOCK = ("option", "unit", "warrant", "right", "note", "swap", "debenture", "preferred")


def _counts_as_stock(t: dict) -> bool:
    """파생표에 실린 거래라도 증권 이름이 보통주면 주식 거래로 본다(BRK Class A 매수가 파생표에 실린 사례)."""
    title = (t.get("security") or "").lower()
    if any(w in title for w in NON_STOCK):          # 우선주·워런트 등은 표와 상관없이 주식 거래가 아니다
        return False
    if not t.get("derivative"):
        return True
    return any(w in title for w in STOCK_WORDS) and not any(w in title for w in NON_STOCK)


def summarize_insiders(rows: list[dict], days: int, today: dt.date | None = None, covered_since: str | None = None) -> dict:
    """장내 매수(P)·매도(S)만 모은다. 보상(A)·옵션 행사(M)·세금 처분(F)은 따로 센다.
    정정 공시(4/A)와 원본이 같은 거래를 실으면 한 번만 센다(보고자·날짜·코드·수량·단가가 같으면 같은 거래)."""
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()      # 거래일 기준. 읽은 범위(covered_since)는 접수일이라 거르지 않는다
    seen: dict[tuple, int] = {}
    buyers: dict[str, dict] = {}
    sellers: dict[str, dict] = {}
    other: dict[str, int] = {}
    big_other: list[dict] = []
    plan_sell = 0.0
    plan_unknown = 0.0
    sell_total = 0.0
    for ri, r in enumerate(rows):
        who = ", ".join(o["name"] or "?" for o in r.get("owners", []))
        role = "; ".join(filter(None, (o.get("title") or "/".join(o.get("roles") or []) for o in r.get("owners", []))))
        for t in r.get("_all_transactions") or r.get("transactions", []):
            if not _counts_as_stock(t) or not t.get("date") or t["date"] < since:
                continue
            key = (who, t["date"], t.get("code"), t.get("shares"), t.get("price"), t.get("security"))
            if seen.get(key, ri) != ri:
                continue            # 다른 공시에 똑같이 실린 거래(정정 전후)는 한 번만. 같은 공시 안의 같은 줄은 각각 센다
            seen[key] = ri
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
        key = (r["cusip"][:6], r["put_call"], r.get("share_type") or "SH")     # 원금(PRN)과 주식 수(SH)는 더하지 않는다
        g = agg.setdefault(key, {"issuer": r["issuer"], "cusip6": key[0], "put_call": r["put_call"], "classes": [],
                                 "share_type": key[2], "value": 0, "shares": 0})
        g["value"] += r["value"]
        g["shares"] += r["shares"]
        g["classes"].append({"class": r["class"], "cusip": r["cusip"], "shares": r["shares"], "value": r["value"]})
    for g in agg.values():
        if len(g["classes"]) == 1:
            g["class"] = g["classes"][0]["class"]
            del g["classes"]
    return list(agg.values())


def _nm(s: str) -> str:
    return " ".join(str(s).lower().replace(".", " ").replace(",", " ").split())


SPLITS = (2, 3, 4, 5, 8, 10, 15, 20, 25, 30, 40, 50)


def _split_ratio(cur: dict, prev: dict) -> int | None:
    """분할(역분할)로 보이면 배수를 돌려준다. 주식 수 비율이 정수에 0.5% 안으로 딱 맞고 주당 가치가 크게 줄었으면 분할로 본다
    (KLA 9.994배는 분기 중 주가가 2배 올라 주당 가치 비율이 4.9배여도 분할. 정확히 2배 사고 주가가 15% 내린 매수는 분할이 아니다).
    그보다 어긋나면 주당 가치 비율도 배수의 15% 안에서 맞아야 한다.
    역분할(주식 수가 1/k)은 음수로 돌려준다."""
    a, b = cur["shares"], prev["shares"]
    if not a or not b or not cur["value"] or not prev["value"]:
        return None
    for sign, r, px in ((1, a / b, (prev["value"] / b) / (cur["value"] / a)), (-1, b / a, (cur["value"] / a) / (prev["value"] / b))):
        if r < 1.8:
            continue
        k = min(SPLITS, key=lambda x: abs(x - r))
        exact = abs(r - k) / k < 0.005 and px >= max(1.5, k / 2.5)      # 정수배에 딱 맞고 주당 가치도 크게 줄었다
        close = abs(r - k) / k < 0.03 and abs(px - k) / k < 0.15        # 조금 어긋나면 주당 가치 비율도 맞아야 한다
        if exact or close:
            return sign * k
    return None


def diff_13f(cur: list[dict], prev: list[dict]) -> dict:
    """직전 분기 대비: 신규, 전량 매도, 주식 수 증가·감소. 주식 수 기준이다(금액은 주가 변동이 섞인다)."""
    def key(r: dict) -> tuple:
        return (r.get("cusip6") or r["cusip"], r["put_call"], r.get("share_type") or "SH")

    p = {key(r): r for r in prev}
    c = {key(r): r for r in cur}
    new = [c[k] for k in c if k not in p]
    exited = [p[k] for k in p if k not in c]
    # CUSIP이 바뀐 같은 회사(분사·재상장 등)는 신규·청산이 아니라 같은 종목으로 묶는다
    renamed = []
    from collections import Counter
    cnt_new = Counter((_nm(r["issuer"]), r["put_call"]) for r in new)
    cnt_ex = Counter((_nm(r["issuer"]), r["put_call"]) for r in exited)
    ex_by_name = {(_nm(r["issuer"]), r["put_call"]): r for r in exited}
    for r in list(new):
        kk = (_nm(r["issuer"]), r["put_call"])
        o = ex_by_name.get(kk) if cnt_new[kk] == 1 and cnt_ex[kk] == 1 else None     # 이름 전체가 같고 1대1일 때만
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
            what = f"주식 분할로 보입니다(약 {ratio}:1)" if ratio > 0 else f"주식 병합(역분할)으로 보입니다(약 1:{-ratio})"
            splits.append({**row, "split_suspected": f"{what}. 주식 수 변화를 매매로 읽지 마세요"})
            continue
        (up if a > b else down).append(row)
    key = lambda r: -abs(r.get("value", 0))  # noqa: E731
    # 신규 종목 이름이 기존 보유 종목과 첫 단어가 같으면 분사·합병 전환일 수 있다(Honeywell → Honeywell Aerospace)
    held = {str(r["issuer"]).split()[0].lower() for r in cur if r not in new and str(r["issuer"]).split()}
    for r in new:
        w = str(r["issuer"]).split()[0].lower() if str(r["issuer"]).split() else ""
        if len(w) >= 4 and w in held:
            r["note"] = "같은 이름의 기존 보유 종목이 있어 분사(스핀오프)로 받은 주식일 수 있습니다. 신규 매수로 단정하지 마세요"
    key = lambda r: -abs(r.get("value", 0))  # noqa: E731
    return {"new": sorted(new, key=key), "exited": sorted(exited, key=key), "splits": splits, "cusip_changed": renamed,
            "increased": sorted(up, key=lambda r: -abs(r["shares_change"] * (r["value"] / r["shares"] if r["shares"] else 0))),
            "decreased": sorted(down, key=lambda r: -abs(r["shares_change"] * (r["value"] / r["shares"] if r["shares"] else 0)))}
