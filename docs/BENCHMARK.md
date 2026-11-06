# 비교 분석: GitHub의 EDGAR 스킬·MCP 서버 (2026-11-06 기준)

읽기만 했고(gh API), 비교 대상 코드는 내려받거나 실행하지 않았다.

## 비교 대상

| 저장소 | 형태 | 최근 커밋 | 한 줄 평 |
|---|---|---|---|
| [dgunning/edgartools](https://github.com/dgunning/edgartools) | Python 라이브러리 + MCP 13개 | 2026-11-05 | 가장 깊다. 태그 2,900개 표준화, Q4·TTM 계산, 13F 비교, 본문 섹션 읽기. 대신 pandas 등 의존성이 무겁고 신원 설정은 환경변수 경고뿐 |
| [stefanoamorelli/sec-edgar-mcp](https://github.com/stefanoamorelli/sec-edgar-mcp) | MCP 21개 | 2026-10-03 | 도구 수는 많지만 7개는 설명이 비어 있다(f-string docstring). 기간을 `fy`로 골라 비교 기간이 섞인다 |
| [sec-edgar/sec-edgar](https://github.com/sec-edgar/sec-edgar) | 공시 대량 다운로드 라이브러리 | 2025-12-09 | 내려받기 도구다. 분석·LLM 응답 설계는 없다. 속도 제한과 재시도 설계는 참고할 만하다 |
| [datasets/edgar](https://github.com/datasets/edgar) | 데이터 안내 문서 | 2026-03-06 | 코드가 아니라 EDGAR 경로·색인 설명이다. 스크립트는 Python 2 예시. 분기별 재무제표 데이터셋(DERA) 안내가 쓸모 있다 |
| [eddmpython/dartlab](https://github.com/eddmpython/dartlab) | DART+EDGAR 한국 라이브러리 | 2026-10-12 | 같은 문제를 푸는 한국 프로젝트. Q4 = FY − Q1 − Q2 − Q3, 공시 본문을 외부 텍스트로 표시, 한영 회사명 사전 |
| [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | 스킬 2개 | 2026-08-29 | 8-K 항목별 영향 표, 내부자 신호 표. 점수화가 투자 권유로 흐른다 |
| [lkcair/sec-ai-mcp](https://github.com/lkcair/sec-ai-mcp) | MCP 22개 | 2026-10-21 | 본문 키워드 스니펫 검색, 전문 검색, frames 동종 순위, 자가 점검 도구 |
| [monriah-studio/sec-edgar-mcp-remote](https://github.com/monriah-studio/sec-edgar-mcp-remote) | 원격 MCP + 스킬 | 2026-10-24 | 집단 매수 판정, 기간 라벨 규칙(end 기준, fy 아님), 프롬프트 10개를 실서버로 돌리는 테스트 |
| [emresoykan/edgar-mcp](https://github.com/emresoykan/edgar-mcp) | MCP | 2026-08-19 | 누적 현금흐름의 분기 분해, 부호 이상 감지, 대차 균형 점검 |

## 우리가 이미 앞선 점

- **신원 설정.** 비교 대상은 환경변수만 보거나(edgartools, sec-edgar-mcp는 없으면 서버가 아예 안 뜬다) 기본값을 보낸다. 우리는 처음 쓸 때 사용자에게 묻고, 추측을 막고, `.env`에 저장한다.
- **최신 공시 지연.** companyfacts가 최신 공시를 아직 싣지 않은 경우를 명시적으로 처리하는 곳은 우리뿐이다(`meta.fallback`).
- **한 공시 기준 비교.** 당기와 전기를 같은 공시에서 꺼내서 정정 전후가 섞이지 않는다. edgartools `edgar_trends`는 같은 기간에서 가장 큰 값을 고르고, sec-edgar-mcp는 `fy`로 골라 비교 기간이 섞인다.
- **20-F 통화.** 공시 안에서 값이 가장 많은 통화를 고른다. 다른 곳은 USD 묶음이나 행이 많은 묶음을 집어 일부만 환산된 값을 쓴다.
- **의존성.** 표준 라이브러리만 쓴다.

## 가져올 것 (우선순위)

| 순위 | 항목 | 근거 | 0.2.0 반영 |
|---|---|---|---|
| 1 | Q4 계산(FY − 9개월 누적), 누적 현금흐름의 분기 분해, 파생 표시 | edgartools `ttm/calculator.py`, dartlab `pivot.py`, emresoykan | 반영 |
| 2 | `fin_trend`: 8분기·5년 추이, 전년 동기 대비, CAGR, 태그 변경 표시 | monriah 스킬 §3, edgartools `trends.py` | 반영 |
| 3 | 매출 태그 오선택 방지(같은 기간이면 큰 합계를 쓴다) | edgartools `_prefer_consolidated_totals` (MetLife) | 반영 |
| 4 | 공시 본문 읽기: 섹션, 키워드 스니펫, offset 페이지 | edgartools `reader.py`, lkcair `download_filing_text` | 반영 |
| 5 | 8-K 실적 발표(EX-99.1) 읽기 | edgartools `_extract_8k_section` | 반영 |
| 6 | 전문 검색(efts.sec.gov) | lkcair, clearsmog | 반영 |
| 7 | 내부자 요약: 기간 창, 매수자 수, 집단 매수, 10b5-1 비중 (신호 점수는 안 한다) | monriah `api.js`, Vibe-Trading | 반영 |
| 8 | 13F 보유와 직전 분기 대비 변화 (2023년부터 금액은 달러 단위) | edgartools `ownership.py` | 반영 |
| 9 | 구조화된 오류(error_code, next_steps) | edgartools `classify_error` | 반영 |
| 10 | 공시 원문 캐시(불변이라 영구), 오래된 공시 목록 페이지 | edgartools `httpclient.py`, getregdata | 반영 |
| 11 | 대차 균형 점검 | emresoykan `_add_balance_check` | 반영 |
| 12 | 한국 기업 미국 상장 라우팅 규칙, 평가 세트 | dartlab 규칙 4, monriah 프롬프트 테스트 | 반영 |
| 13 | frames 동종 순위 | lkcair `get_xbrl_frames` | 보류 (달력 기간 기준이라 회계연도와 어긋난다. 0.3 후보) |
| 14 | 세그먼트 매출 | dartlab(DERA 1GB 묶음) | 보류 (무겁다) |
