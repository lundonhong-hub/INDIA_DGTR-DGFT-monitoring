# 인도 무역규제 모니터 (2차 그물) — DGTR + DGFT

동관(copper) 수출기업 관점에서 인도의 무역구제·수입정책 변화를 하루 2회 자동 감시.
동관 관련 규제가 뜨는 순간 **이메일 + 텔레그램**으로 즉시 알림.

## 3중 그물 구조에서의 위치
| 단계 | 소스 | 성격 | 저장소 |
|------|------|------|--------|
| 1차 | PIB (RSS) | 정부 보도자료 | `India_PIB-monitor` |
| **2차** | **DGTR + DGFT** | **무역 실무 공고** | **이 저장소** |
| 3차 | eGazette (관보) | 법적 확정 | `egazette_monitor` |

## 감시 목적
동관을 제조해 인도로 수출한다. **인도가 동관 수입을 막는 규제 = 수출길 차단.**
- **DGTR**(무역구제총국): 반덤핑 조사. 동관에 반덤핑 관세 → 가격경쟁력 상실.
- **DGFT**(대외무역총국): 수입정책. 동관(Chapter 74) Free→Restricted, BIS/QCO 의무화 → 통관 차단.

## 소스별 수집 방식 (최종 확정)

### DGTR — 직접 접속 ✅
- URL: https://www.dgtr.gov.in/en/anti-dumping-investigation-in-india
- Drupal 서버렌더 → requests + BeautifulSoup으로 충분. GitHub Actions IP 통과됨.
- 고유 ID: 케이스 슬러그(`/anti-dumping-cases/{슬러그}`). 날짜 없어 슬러그로 신규 판단.

### DGFT — 공식 사이트 + ScraperAPI 경유 ✅ (핵심)
- URL: https://www.dgft.gov.in/CP/?opt=notification
- **문제:** 공식 사이트가 GitHub Actions(데이터센터) IP를 차단 → 919바이트 로그인 페이지만 반환.
  requests·curl 모두 동일하게 막힘(= 세션 문제 아닌 IP 차단). 회장 브라우저(집/회사 IP)에서만 열림.
- **해결:** **ScraperAPI 무료 플랜**으로 인도 IP(`country_code=in`) 경유 요청 → 차단 우회.
  결과: STATUS 200, 약 288KB 수신, 232건 파싱 성공.
- **미러 폐기:** stargroup(2025.1에서 데이터 멈춤)·caalley(선별 게재로 Chapter 74 누락) 모두 신뢰 불가 → 사용 안 함. 공식 단일 소스로 확정.
- 데이터 구조: `<table id="metaTable">`에 서버렌더. td[1]=공고번호(고유ID), td[3]=제목, td[4]=날짜, td[6]=PDF 원문 링크(content.dgft.gov.in).
- 고유 ID: 공고번호(예: `22/2026-27`).

## ScraperAPI 설정
- 무료 플랜: 월 1,000 크레딧 자동 리셋(이월 없음), 최대 5 동시연결. 가입 후 7일간 5,000크레딧.
- 크레딧 소모: 표준 1 / 봇차단 우회 시 +10. DGFT는 요청당 최대 ~11크레딧 추정.
- **회장 사용량:** 하루 2회 × 30일 = 60요청. 최대 660크레딧/월 → 무료 1,000 안쪽. **월 0원.**
- 실패한 요청은 크레딧 차감 안 됨(성공분만 과금).
- ⚠️ API 키 노출 시 대시보드 → API key → MANAGE에서 Reset 후 GitHub Secret 교체.

## 감시 키워드 (monitor.py 상단에서 수정)
### (A) 동관 직접 — 양쪽 소스 공통
`copper`, `brass`, `bronze`, `refined copper`, `chapter 74`, `nfmims`,
`7407`~`7412`(동관·봉·선·판 HS코드)
- 인도 공고는 한국 규격(KS) 아닌 **영문명 + Chapter + HS코드**로 표기.
### (B) 제도 키워드 — DGFT 보조 신호
`qco`, `quality control order`, `bis requirement`, `compulsory registration`, `import monitoring`
- 동관에 BIS/QCO 걸리면 수출 직격탄.

## 알림 로직 (최종)
- 양쪽 소스를 한 번에 수집 → **동관 매칭된 것만** 이메일 + 텔레그램 발송.
- 무관 항목(농산물·귀금속 등)은 state에만 기록, 알림 없음.
- **첫 실행(state=[])이라도 매칭된 항목은 알림 발송.** (232건 전량이 아니라 매칭 소수만 나가므로 폭탄 아님)
  → 과거엔 첫 실행 시 알림을 전부 억제했으나, 이 때문에 필요한 동관 알림까지 막혀 수정함.
- 알림 전에 state를 먼저 저장 → 발송 실패해도 다음 실행에서 중복 발송 안 됨.

## state.json 구조
```json
{
  "DGTR": ["DGTR:slug1", ...],
  "DGFT": ["22/2026-27", ...],
  "empty_streak": {"DGTR": 0, "DGFT": 0},
  "alert_state": {},
  "last_run": "..."
}
```
- **[] 로 비우면 전체 재평가** → 현재 목록 중 매칭 항목이 다시 알림됨(테스트용).
- 평소엔 건드리지 말 것. 비우면 매칭 항목 재발송됨.

## 기술 스택
Python 3.12 · GitHub Actions(하루 2회, KST 09/18시) · Gmail SMTP · 텔레그램 봇 · ScraperAPI(DGFT 전용) · state.json 자동 커밋 · 별도 저장소 독립 운영.

## GitHub Secrets (6개)
| 이름 | 용도 |
|------|------|
| `GMAIL_USER` | 발송 Gmail |
| `GMAIL_APP_PASSWORD` | Gmail 앱 비밀번호 |
| `NOTIFY_TO` | 수신 주소(쉼표로 복수 가능) |
| `SCRAPERAPI_KEY` | DGFT 우회용 |
| `TELEGRAM_TOKEN` | 봇 토큰(@BotFather) |
| `TELEGRAM_CHAT_ID` | 알림 수신 chat id |
- YML env에도 위 6개 모두 전달돼야 함(누락 시 해당 알림만 조용히 건너뜀).

## "조용한 0건" 방어
- 소스별 파싱 건수 < 임계치(DGTR 10 / DGFT 5)면 구조 깨짐 경보.
- 동일 장애 반복 시 경보 1회만(중복 억제). seen 미갱신으로 오염 방지.
- DGFT 232건 정상 → 파싱 급감하면 사이트 구조 변경·ScraperAPI 장애 신호.

## 배경: 동관 수입규제 현황
- 인도는 2021년부터 동(Chapter 74) 수입에 **NFMIMS 등록 의무**(Notification 61/2015-2020).
- 향후 위협: 이 규제 강화(등록→제한→금지) 또는 BIS 인증 추가.
- 실제 감지 예: **22/2026-27**(Chapter 74 수입정책 개정), **20/2026-27**(QCO/BIS 적용).

## TODO (3중 그물 확장)
- **BIS 채널 직접 감시** — 품질인증 의무화는 동관 수출 최대 위협. DGFT엔 예고만, 실제 발효는 BIS 사이트.
