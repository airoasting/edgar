"""공시 본문을 읽는다: 공시 색인에서 문서 고르기, HTML → 텍스트, 10-K·10-Q 섹션 자르기, 키워드 스니펫.

네트워크를 쓰지 않는다(입력은 HTML 문자열). 본문은 외부 텍스트라서 서버가 응답에 untrusted 표시를 붙인다.
"""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article", "ul", "ol",
         "hr", "pre", "blockquote", "center", "font_block"}
SKIP = {"script", "style", "head", "title", "noscript", "ix:header", "xbrli:context"}
# 공시 HTML은 제목에 얇은 공백(U+2009)·전각 공백 같은 유니코드 공백을 쓴다(TSMC 20-F "ITEM\u20095."). 일반 공백으로 바꾼다
SPACES = {c: " " for c in (0xA0, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009,
                           0x200A, 0x202F, 0x205F, 0x3000)}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.hidden: list[str] = []     # display:none 요소의 태그 이름 스택

    def handle_starttag(self, tag: str, attrs: list) -> None:
        t = tag.lower()
        if self.hidden:
            if t == self.hidden[-1]:
                self.hidden.append(t)
            return
        style = (dict(attrs).get("style") or "").replace(" ", "").lower()
        if "display:none" in style and t not in ("br", "img", "hr"):
            self.hidden.append(t)
            return
        if t in SKIP:
            self.skip += 1
        elif t in ("td", "th"):
            self.out.append(" | ")
        elif t in BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if self.hidden:
            if t == self.hidden[-1]:
                self.hidden.pop()
            return
        if t in SKIP:
            self.skip = max(0, self.skip - 1)
        elif t in BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip and not self.hidden:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001  깨진 HTML이면 태그만 지운다
        return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))
    lines = []
    raw = "".join(p.out).translate(SPACES)
    for line in raw.splitlines():
        line = re.sub(r"[ \t​]+", " ", line).strip()
        if "|" in line:                         # 표 행: 빈 칸을 지우고 "$ | 46,743"을 "$46,743"으로 붙인다
            cells = [c.strip() for c in line.split("|") if c.strip()]
            line = " | ".join(cells)
            line = re.sub(r"\$ \| ", "$", line)
            line = re.sub(r" \| %", "%", line)
            line = re.sub(r"\( \| ", "(", line)
            line = re.sub(r" \| \)", ")", line)
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── 공시 색인 ──────────────────────────────────────────────────────────────

class _Index(HTMLParser):
    """{accession}-index.htm 의 문서 표(tableFile)를 읽는다: 순번, 설명, 파일, 유형, 크기."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.in_table = self.in_cell = False
        self.row: list[str] | None = None
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag == "table" and "tableFile" in (a.get("class") or ""):
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.in_table and tag == "td" and self.row is not None:
            self.in_cell = True
            self.row.append("")
        elif self.in_cell and tag == "a" and self.row is not None:
            self.href = a.get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self.in_table = False
        elif tag == "td":
            self.in_cell = False
        elif tag == "tr" and self.row:
            if self.href:
                self.row.append(self.href)
            self.rows.append(self.row)
            self.row, self.href = None, None

    def handle_data(self, data: str) -> None:
        if self.in_cell and self.row is not None:
            self.row[-1] += data


def parse_index(html: str) -> list[dict]:
    p = _Index()
    p.feed(html)
    docs = []
    for r in p.rows:
        if len(r) < 5:
            continue
        href = r[5] if len(r) > 5 else ""
        name = href.split("/")[-1] if href else r[2].split()[0] if r[2].split() else ""
        docs.append({"seq": r[0].strip(), "description": r[1].strip(), "document": name.strip(),
                     "type": r[3].strip(), "size": r[4].strip()})
    return [d for d in docs if d["document"]]


def pick_document(docs: list[dict], form: str, exhibit: str | None) -> dict | None:
    """exhibit(예: 99.1, 99, 10.1)이 있으면 그 첨부를, 없으면 본문(유형이 공시 유형과 같은 첫 문서)을 고른다."""
    if exhibit:
        ex = exhibit.upper().removeprefix("EX-")
        for d in docs:
            t = d["type"].upper()
            if t == f"EX-{ex}" or (t.startswith(f"EX-{ex}") and "." not in ex):
                return d
        return None
    base = form.upper().removesuffix("/A")
    for d in docs:
        if d["type"].upper().removesuffix("/A") == base and d["document"].lower().endswith((".htm", ".html", ".txt", ".xml")):
            return d
    return next((d for d in docs if d["document"].lower().endswith((".htm", ".html"))), None)


# ── 섹션 ───────────────────────────────────────────────────────────────────

# 별칭 → (10-K 항목, 10-Q 항목). 10-Q는 Part I Item 2가 MD&A, Part II Item 1A가 위험 요인이다.
# 20-F는 체계가 다르다: 위험 요인은 Item 3 안의 'Risk Factors', MD&A는 Item 5, 사업은 Item 4, 소송은 Item 8 안.
SECTION_ALIAS = {
    "business": ("1", None), "사업": ("1", None),
    "risk": ("1A", "1A"), "risk_factors": ("1A", "1A"), "위험": ("1A", "1A"), "위험요인": ("1A", "1A"),
    "cybersecurity": ("1C", None),
    "legal": ("3", "1"), "소송": ("3", "1"),
    "mdna": ("7", "2"), "md&a": ("7", "2"), "경영진분석": ("7", "2"),
    "market_risk": ("7A", "3"), "시장위험": ("7A", "3"),
    "financial_statements": ("8", "1"), "재무제표": ("8", "1"),
    "controls": ("9A", "4"),
    "executive_compensation": ("11", None), "임원보수": ("11", None),
}
ITEM_RE = re.compile(r"(?im)^[ \t|]*item[ \t\xa0]*(\d{1,2}[a-c]?)\b[ \t.:\-–—]*(.{0,120})$")


SECTION_20F = {"risk": ("3", r"risk\s+factors"), "risk_factors": ("3", r"risk\s+factors"), "위험": ("3", r"risk\s+factors"),
               "위험요인": ("3", r"risk\s+factors"), "mdna": ("5", None), "md&a": ("5", None), "경영진분석": ("5", None),
               "business": ("4", None), "사업": ("4", None), "legal": ("8", r"legal\s+proceedings"), "소송": ("8", r"legal\s+proceedings"),
               "market_risk": ("11", None), "시장위험": ("11", None), "financial_statements": ("18", None), "재무제표": ("18", None),
               "controls": ("15", None), "executive_compensation": ("6", r"compensation"), "임원보수": ("6", r"compensation")}


def section_spec(section: str, form: str) -> tuple[str | None, str | None]:
    """(항목 번호, 항목 안에서 시작할 소제목 정규식)."""
    s = section.strip().lower().replace(" ", "_")
    s = s.removeprefix("item_").removeprefix("item")
    if form.upper().startswith(("20-F", "40-F")) and s in SECTION_20F:
        return SECTION_20F[s]
    return section_key(s, form), None


def section_key(section: str, form: str) -> str | None:
    s = section.strip().lower().replace(" ", "_")
    s = s.removeprefix("item_").removeprefix("item")
    if s in SECTION_ALIAS:
        k10, k10q = SECTION_ALIAS[s]
        return k10q if form.upper().startswith("10-Q") else k10
    if re.fullmatch(r"\d{1,2}[a-c]?", s):
        return s.upper()
    return None


PART2_KEYS_10Q = {"1", "1A", "2_PART2", "5", "6"}


def part2_start(text: str) -> int:
    """10-Q 본문의 PART II 시작. 목차에도 'PART II'가 있으므로 뒤에서부터 본다."""
    ms = list(re.finditer(r"(?im)^[ \t|]*part[ \t]+ii\b(?![ \t]*i)", text))
    return ms[-1].start() if ms else 0


SECTION_TITLES = {"1": r"legal\s+proceedings|business|financial\s+statements", "1A": r"risk\s+factors"}


def find_section(text: str, key: str, subhead: str | None = None, start_at: int = 0,
                 title: str | None = None) -> tuple[int, int] | None:
    """'Item <key>' 제목부터 다음 Item 제목까지. 목차에도 같은 제목이 있으므로 가장 긴 구간을 고른다.
    subhead가 있으면 그 구간 안의 소제목(예: 20-F Item 3 안 'Risk Factors')부터 자른다."""
    heads = [(m.start(), m.group(1).upper(), m.group(2)) for m in ITEM_RE.finditer(text) if m.start() >= start_at]
    tre = re.compile(title, re.I) if title else None
    best = None
    for i, (pos, k, ttl) in enumerate(heads):
        if k != key.upper() or (tre and not tre.search(ttl or "")):
            continue
        # 끝은 번호가 다른 다음 Item. 페이지마다 반복되는 머리글('PART II')은 Item 줄이 아니라 영향이 없다
        end = next((p for p, k2, _t in heads[i + 1:] if k2 != k), len(text))
        if best is None or end - pos > best[1] - best[0]:
            best = (pos, end)
    # 목차 한 줄뿐이면 실패로 본다. 제목이 맞는 구간은 짧아도 받는다(MSFT 10-Q 소송 항목은 "Note 13 참조" 203자)
    if best and best[1] - best[0] < (120 if title else 300):
        return None
    if best and subhead:
        m = re.compile(rf"(?im)^[ \t|]*(?:[a-z]\.\s*)?{subhead}\b").search(text, best[0], best[1])
        if m:
            best = (m.start(), best[1])
    return best


def snippets(text: str, query: str, width: int = 400, limit: int = 12) -> tuple[list[dict], int]:
    """대소문자 무시 검색. 겹치는 구간은 합친다. (스니펫, 전체 일치 수)."""
    terms = [t.strip() for t in re.split(r"\s*\|\s*|\s*,\s*", query) if t.strip()] or [query.strip()]
    hits: list[int] = []
    for t in terms:
        body = re.escape(t)
        if re.fullmatch(r"[\w .&'-]+", t):        # 영숫자 단어는 단어 경계로 (AI가 certain에 걸리지 않게)
            body = rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])" if not t[-1].isalpha() or len(t) <= 3 else rf"\b{body}"
        flags = 0 if (t.isupper() and len(t) <= 4) else re.I   # 짧은 대문자 약어(AI, EV, FX)는 대소문자 구분
        hits += [m.start() for m in re.finditer(body, text, flags)]
    hits.sort()
    out: list[dict] = []
    for h in hits:
        s, e = max(0, h - width), min(len(text), h + width)
        if out and s <= out[-1]["_e"]:
            out[-1]["_e"] = e
            out[-1]["hits"] += 1
            continue
        out.append({"_s": s, "_e": e, "hits": 1})
    res = [{"offset": o["_s"], "hits": o["hits"], "text": text[o["_s"]:o["_e"]].strip()} for o in out[:limit]]
    return res, len(hits)
