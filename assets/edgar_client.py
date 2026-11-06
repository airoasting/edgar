"""SEC EDGAR 클라이언트. API 키는 없고, SEC 공정 접근 정책에 따라 요청마다 이름과 이메일을 User-Agent에 담는다.

    정책: https://www.sec.gov/os/accessing-edgar-data  (User-Agent 필수, 초당 10건 이하)

이름과 이메일은 처음 쓸 때 사용자에게 받아 ~/.claude/skills/edgar/.env 에 저장한다(save_identity).
플러그인 설치 경로는 업데이트 때마다 바뀌므로 고정 위치를 쓴다. DART 스킬의 키 파일 규칙과 같다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from http_compat import requests  # noqa: E402  (requests가 없으면 urllib 대체품)

DATA = "https://data.sec.gov"
WWW = "https://www.sec.gov"
TICKERS_URL = f"{WWW}/files/company_tickers_exchange.json"
MIN_INTERVAL = 0.12        # 초당 약 8건. SEC 상한(10건)보다 조금 낮게 둔다
TICKERS_TTL = 24 * 3600    # 티커 목록은 하루에 한 번만 새로 받는다
SUBMISSIONS_TTL = 600      # 공시 목록 10분
FACTS_TTL = 3600           # XBRL 재무 1시간 (SEC도 공시 뒤 몇 분~하루 늦게 반영한다)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


# ── 사용자 정보 (User-Agent) ────────────────────────────────────────────────

def home() -> Path:
    """설정 폴더. 테스트와 특수 설치를 위해 EDGAR_HOME으로 바꿀 수 있다."""
    return Path(os.environ.get("EDGAR_HOME") or Path.home() / ".claude" / "skills" / "edgar")


def env_file() -> Path:
    return home() / ".env"


def _read_env(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def find_identity() -> dict | None:
    """이름·이메일을 찾는 단 하나의 규칙.

    순서: 환경변수 EDGAR_USER_AGENT → 환경변수 EDGAR_NAME·EDGAR_EMAIL → ~/.claude/skills/edgar/.env
    → 프로젝트 루트 .env → assets/.env.
    """
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if ua:
        return {"user_agent": ua, "source": "환경변수 EDGAR_USER_AGENT"}
    name, email = os.environ.get("EDGAR_NAME", "").strip(), os.environ.get("EDGAR_EMAIL", "").strip()
    if name and email:
        return {"name": name, "email": email, "user_agent": f"{name} {email}", "source": "환경변수 EDGAR_NAME·EDGAR_EMAIL"}
    for p in (env_file(), HERE.parent / ".env", HERE / ".env"):
        env = _read_env(p)
        if env.get("EDGAR_USER_AGENT"):
            return {"user_agent": env["EDGAR_USER_AGENT"], "source": str(p)}
        if env.get("EDGAR_NAME") and env.get("EDGAR_EMAIL"):
            return {"name": env["EDGAR_NAME"], "email": env["EDGAR_EMAIL"],
                    "user_agent": f"{env['EDGAR_NAME']} {env['EDGAR_EMAIL']}", "source": str(p)}
    return None


def validate_identity(name: str, email: str) -> tuple[str, str]:
    """이름과 이메일을 다듬어 돌려준다. 틀리면 ValueError (메시지는 사용자에게 그대로 보여 줄 수 있다)."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    email = str(email or "").strip()
    if not name:
        raise ValueError("이름이 비었습니다.")
    if not name.isascii() or not email.isascii():
        # HTTP 헤더는 라틴 문자만 실을 수 있다. 한글 이름을 저장하면 이후 모든 요청이 인코딩 오류로 실패한다
        raise ValueError("SEC 요청 헤더는 영문만 받습니다. 이름을 영문으로 적어 주세요(예: Gildong Hong, 회사 영문명도 됩니다).")
    if len(name) > 80 or re.search(r"[\"'=\\\r\n\t]", name):
        raise ValueError("이름은 80자 이내로, 따옴표·등호·역슬래시 없이 적어 주세요.")
    if not EMAIL_RE.match(email) or len(email) > 120:
        raise ValueError(f"이메일 형식이 아닙니다: {email or '(빈 값)'}")
    return name, email


def save_identity(name: str, email: str) -> Path:
    """~/.claude/skills/edgar/.env 에 EDGAR_NAME·EDGAR_EMAIL을 쓴다. 파일의 다른 줄은 그대로 둔다. 권한은 600."""
    name, email = validate_identity(name, email)
    p = env_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = ["# EDGAR 스킬 설정. SEC 요청 헤더(User-Agent)에만 쓴다."]
    new = {"EDGAR_NAME": f'"{name}"', "EDGAR_EMAIL": email}
    kept = [ln for ln in lines if ln.strip().partition("=")[0].strip() not in (*new, "EDGAR_USER_AGENT")]
    kept += [f"{k}={v}" for k, v in new.items()]
    p.write_text("\n".join(kept) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


class SetupRequired(RuntimeError):
    """이름·이메일이 아직 없다. 서버는 이것을 setup_required 응답으로 바꾼다."""


# ── 접속 ───────────────────────────────────────────────────────────────────

class EdgarError(ConnectionError):
    """SEC 접속 실패(재시도 후). code는 서버 응답의 error_code로 그대로 나간다."""

    def __init__(self, message: str, code: str = "SEC_UNAVAILABLE") -> None:
        super().__init__(message)
        self.code = code
        self.detail: str | None = None


class ToolError(Exception):
    """사용자나 모델이 고칠 수 있는 입력 문제. code는 error_code, data는 후보 목록처럼 다음 행동에 필요한 것."""

    def __init__(self, message: str, data: dict | None = None, code: str = "BAD_INPUT") -> None:
        super().__init__(message)
        self.data = data or {}
        self.code = code


def cik10(cik: Any) -> str:
    return str(int(str(cik).strip())).zfill(10)


class EdgarClient:
    def __init__(self, user_agent: str | None = None) -> None:
        if not user_agent:
            ident = find_identity()
            if not ident:
                raise SetupRequired("SEC EDGAR 접속에 쓸 이름과 이메일이 아직 등록되지 않았습니다.")
            user_agent = ident["user_agent"]
            if not user_agent.isascii():
                raise SetupRequired(f"저장된 이름·이메일({ident.get('source')})에 영문이 아닌 글자가 있어 SEC 요청 헤더에 실을 수 "
                                    "없습니다. 이름을 영문으로 다시 등록해야 합니다.")
        self.headers = {"User-Agent": user_agent, "Accept": "application/json, text/xml, */*"}
        self._last = 0.0
        self._mem: dict[tuple[str, str], tuple[float, Any]] = {}
        self._tickers: list[dict] | None = None
        self.stale: set[str] = set()          # SEC 접속 실패로 오래된 캐시를 쓴 항목

    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        self._last = time.monotonic()

    def get(self, url: str, retries: int = 3):
        """GET → 응답. 429·5xx는 쉬었다 다시 부른다. 404는 None."""
        last = ""
        for i in range(retries + 1):
            self._wait()
            try:
                r = requests.get(url, headers=self.headers, timeout=30)
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if r.status_code == 404:
                    return None
                if r.status_code == 403:
                    raise EdgarError("SEC가 요청을 거절했습니다(403). 대개 짧은 시간에 요청이 몰렸을 때 생깁니다. 10분 뒤 다시 "
                                     "시도하세요. 계속되면 회사망·VPN을 끄거나, 등록한 이메일이 실제로 받을 수 있는 주소인지 "
                                     "확인하세요(\"EDGAR 이메일 바꿔줘\").", "RATE_LIMITED")
                if r.status_code < 400:
                    return r
                last = f"HTTP {r.status_code}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            if i < retries:
                time.sleep(2 ** i)
        if last == "HTTP 429":
            raise EdgarError("SEC 요청 한도를 넘었습니다(429). 바로 다시 부르지 말고 10분 뒤 시도하세요.", "RATE_LIMITED")
        err = EdgarError("SEC에 연결하지 못했습니다. 인터넷 연결(회사망·VPN 포함)을 확인하고 1분 뒤 다시 시도하세요. "
                         "SEC 점검 시간일 수도 있습니다.")
        err.detail = f"{last} · {url}"
        raise err

    def get_json(self, url: str) -> Any:
        r = self.get(url)
        return None if r is None else r.json()

    def get_text(self, url: str) -> str | None:
        """공시 원문(/Archives/)은 바뀌지 않으므로 디스크에 영구 캐시한다. 다른 주소는 매번 받는다."""
        cache = None
        if "/Archives/edgar/data/" in url and not url.endswith("-index.htm"):
            import hashlib
            cache = home() / "cache" / "archives" / (hashlib.sha1(url.encode()).hexdigest() + ".txt")
            if cache.exists():
                try:
                    return cache.read_text(encoding="utf-8")
                except OSError:
                    pass
        r = self.get(url)
        if r is None:
            return None
        text = r.text
        if cache is not None:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(text, encoding="utf-8")
            except OSError:
                pass
        return text

    # ── 데이터 묶음 ──

    def tickers(self) -> list[dict]:
        """[{cik, name, ticker, exchange}]. 디스크 캐시(하루)를 쓰고, 받기 실패하면 오래된 캐시라도 쓴다."""
        if self._tickers is not None:
            return self._tickers
        cache = home() / "cache" / "company_tickers_exchange.json"
        raw = None
        fresh = cache.exists() and time.time() - cache.stat().st_mtime < TICKERS_TTL
        if fresh:
            try:
                raw = json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
        if raw is None:
            try:
                raw = self.get_json(TICKERS_URL)
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(raw), encoding="utf-8")
            except EdgarError:
                if not cache.exists():
                    raise
                raw = json.loads(cache.read_text(encoding="utf-8"))
        fields = raw["fields"]
        self._tickers = [dict(zip(fields, row)) for row in raw["data"]]
        return self._tickers

    def _cached_json(self, bucket: str, key: str, url: str, ttl: int) -> Any:
        """메모리 → 디스크 → SEC 순서로 찾는다. ttl(초)이 지나면 새로 받는다. stdio 서버는 며칠씩 떠 있어서
        만료가 없으면 새 10-Q가 안 보인다. 받기 실패하면 오래된 디스크 캐시라도 쓰고 stale로 표시한다."""
        now = time.time()
        mem = self._mem.get((bucket, key))
        if mem and now - mem[0] < ttl:
            return mem[1]
        path = home() / "cache" / bucket / f"{key}.json"
        if path.exists() and now - path.stat().st_mtime < ttl:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._remember(bucket, key, data, path.stat().st_mtime)
                return data
            except (OSError, ValueError):
                pass
        try:
            data = self.get_json(url)
        except EdgarError:
            if not path.exists():
                raise
            data = json.loads(path.read_text(encoding="utf-8"))
            self.stale.add(f"{bucket}/{key}")
            return data
        if data is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data), encoding="utf-8")
            except OSError:
                pass
        self._remember(bucket, key, data, now)
        return data

    def _remember(self, bucket: str, key: str, data: Any, at: float) -> None:
        if bucket == "facts":       # companyfacts는 큰 회사면 수 MB다. 메모리에는 최근 3개만 둔다
            olds = [k for k in self._mem if k[0] == "facts"]
            if len(olds) >= 3:
                self._mem.pop(olds[0])
        self._mem[(bucket, key)] = (at, data)

    def submissions(self, cik: Any) -> dict:
        c = cik10(cik)
        data = self._cached_json("submissions", c, f"{DATA}/submissions/CIK{c}.json", SUBMISSIONS_TTL)
        if data is None:
            raise EdgarError(f"CIK {c}의 공시 목록이 없습니다. CIK가 맞는지 확인하세요.", "NOT_FOUND")
        return data

    def submissions_page(self, name: str) -> dict:
        """recent에 안 든 오래된 공시 목록 페이지(filings.files[].name). 이미 끝난 기간이라 하루 캐시한다."""
        return self._cached_json("submissions", name.removesuffix(".json"), f"{DATA}/submissions/{name}", TICKERS_TTL) or {}

    def companyfacts(self, cik: Any) -> dict | None:
        """XBRL 재무 숫자 전체. XBRL을 낸 적 없는 회사(일부 외국 기업·펀드)는 None."""
        c = cik10(cik)
        return self._cached_json("facts", c, f"{DATA}/api/xbrl/companyfacts/CIK{c}.json", FACTS_TTL)


def filing_rows(subs: dict) -> list[dict]:
    """submissions의 열 묶음(recent 또는 오래된 페이지)을 행 목록으로. 최근 접수순. recent에는 최근 1,000건 남짓이 있다."""
    r = subs.get("filings", {}).get("recent", subs if "accessionNumber" in subs else {})
    keys = list(r)
    n = len(r.get("accessionNumber", []))
    return [{k: r[k][i] for k in keys} for i in range(n)]


def filing_urls(cik: Any, accession: str, primary_doc: str = "") -> dict:
    nodash = accession.replace("-", "")
    base = f"{WWW}/Archives/edgar/data/{int(str(cik))}/{nodash}"
    out = {"index_url": f"{base}/{accession}-index.htm"}
    if primary_doc:
        out["url"] = f"{base}/{primary_doc}"
    return out
