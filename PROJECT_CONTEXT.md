# DGTR/DGFT 인도 무역규제 모니터 (2차 그물)

인도 무역규제 감시 **3중 그물** 구조의 두 번째 그물. DGTR(무역구제총국)의
반덤핑 조사를 감시해, PIB(1차)에 안 잡히는 실무 공고를 잡아낸다.

| 단계 | 소스 | 성격 | 타이밍 | 저장소 |
|------|------|------|--------|--------|
| 1차 그물 | PIB (RSS) | 정부 발표 | 가장 빠름 | `India_PIB-monitor` |
| **2차 그물** | **DGTR/DGFT** | **무역 실무 공고** | **중간** | **이 저장소** |
| 3차 그물 | eGazette (관보) | 법적 확정 | 가장 늦음 | `egazette_monitor` |

## 왜 필요한가
DGTR 반덤핑 조사 개시(initiation)는 수출기업에 직격탄이지만, 언론이
관심 없는 기술적 공고라 PIB 보도자료에 누락되기 쉽다. DGTR 사이트를
직접 감시해야 하는 이유다.

## 무엇을 감시하나
- **대상 페이지:** https://www.dgtr.gov.in/en/anti-dumping-investigation-in-india
- **감지 단위:** 케이스 슬러그(URL). **이 목록엔 날짜가 없어** 날짜 비교가
  불가능하므로, `/anti-dumping-cases/{슬러그}`를 고유 ID로 삼아 신규 여부를 판단한다.
  → 1차 그물(PIB, 날짜 기반)과 근본적으로 다른 설계 포인트.
- **분류:** 신규 항목은 **전부** 추적하되, copper 키워드에 걸리면 🔴 긴급,
  아니면 ⚪ 일반으로 나눠 알림.
  - DGTR은 반덤핑 전문 기관이라 조사 건수 자체가 적음(연 수십 건, 동관은 드묾).
    노이즈가 적으므로 필터는 "감지"가 아니라 "분류"에만 쓴다 — 놓침 방지 우선.

## 감시 키워드
`copper`, `copper tube`, `copper alloy`, `refined copper`, `brass`, `bronze`
- 인도는 자국 규격(IS)·HS코드로 발표하므로 한국 규격코드(KS D5301 등)는 무의미.
  인도가 실제로 쓸 **영문 제품명**으로 감시한다.

## 기술 스택 (기존 2개와 통일)
- Python 3.12
- GitHub Actions (Public 저장소, cron 무료 실행) — 하루 2회 (KST 09시/18시)
- Gmail SMTP 알림 (앱 비밀번호, GitHub Secrets)
- `state.json` 중복 방지 (Actions가 자동 커밋)
- 별도 저장소 독립 운영 (장애 격리)

## 접근 방식 결정 기록
- **RSS 없음** 확인 → HTML 파싱.
- **Playwright 불필요** — DGTR은 서버사이드 렌더링(Drupal)이라 HTML에 데이터가
  그대로 있음. `requests` + `BeautifulSoup`으로 충분. (eGazette와 달리 JS 렌더링 아님.)
- 항목 링크는 모두 `/anti-dumping-cases/`를 포함 → 이 선택자로 왼쪽 메뉴
  잡링크(checklist 등)와 구분.

## 1차 그물에서 얻은 교훈 (반영 완료)
1. **브라우저 UA 필수** — 기본 UA는 403. 단 데이터센터 IP는 UA 붙여도 403이고,
   **GitHub Actions IP는 통과**. (로컬/컨테이너에서 403 나도 정상)
2. **조용한 0건이 최대 위험** — 구조가 바뀌면 에러 없이 0건만 반환.
   → `MIN_EXPECTED_ITEMS`(=10) 미만이면 "구조 깨짐" 경보 발송,
   이때 `seen_slugs`는 갱신하지 않아 오염 방지.
3. **한국 규격코드 무의미** — 인도 IS/HS코드·영문 제품명으로 감시.
4. **파싱 전 수신 내용 로그 출력** — 상위 5건 제목을 로그에 찍어 눈으로 확인.

## 파일 구성
```
monitor.py                    # 메인 (수집→파싱→분류→알림→state)
requirements.txt
state.json                    # {seen_slugs, empty_streak, last_run}
.github/workflows/monitor.yml # cron 자동 실행 + state 자동 커밋
```

## 설치 (GitHub Secrets)
저장소 Settings → Secrets and variables → Actions에 등록:
- `GMAIL_USER` — 발송 Gmail 주소
- `GMAIL_APP_PASSWORD` — Gmail 앱 비밀번호(16자리)
- `NOTIFY_TO` — 수신 주소(미설정 시 GMAIL_USER로 발송, 쉼표로 여러 명 가능)

## 동작 로직
1. `state.json` 로드 → 이미 본 슬러그 집합.
2. DGTR 목록 페이지 GET (UA 헤더).
3. `/anti-dumping-cases/` 링크 파싱 → 슬러그·제목 추출.
4. **안전장치:** 파싱 건수 < 10 → 구조 깨짐 경보, seen 미갱신 후 종료.
5. **첫 실행:** 현재 목록 전부를 기준선으로 등록만, 알림 없음(폭탄 방지).
6. 신규 슬러그 추출 → copper 키워드로 🔴/⚪ 분류 → 이메일.
7. `state.json` 갱신 → Actions가 커밋.

## 로컬 테스트
```bash
pip install -r requirements.txt
python monitor.py   # 컨테이너/로컬은 403 날 수 있음(정상). Actions에서 정상 동작.
```
Gmail Secrets 없이 실행하면 메일 대신 콘솔에 미리보기가 출력된다.

## TODO — DGFT (2차 그물 확장)
DGTR 완성·안정화 후 DGFT(대외무역총국, 수입정책 공고) 추가 예정.
DGFT는 Notification/Public Notice 페이지 구조를 별도 확인해야 함
(RSS 유무·requests 접근 가능 여부부터 점검).
