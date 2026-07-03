#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
인도 무역규제 모니터 (2차 그물) — DGTR + DGFT 통합
- DGTR: 반덤핑 조사 목록 (dgtr.gov.in, 직접 접근)
- DGFT: 공식 Notification 페이지(dgft.gov.in/CP/?opt=notification)를 1순위로 직접 접근
        공식 사이트 장애/구조 변경 시에만 미러(stargroup, caalley)를 fallback으로 사용
- 동관(copper) 키워드 매칭된 것만 메일 발송, 무관은 state만 기록
- state.json을 [] 로 비우면 전체 재알림 (PIB 방식 통일)

교훈 반영:
  1. 브라우저 UA 필수 (GitHub Actions IP는 통과)
  2. 조용한 0건 방어: 소스별 임계치 미만이면 구조 깨짐 경보
  3. 한국 규격코드(KS) 무의미 → 인도 영문명·Chapter·HS코드로 감시
  4. 파싱 전 제목 로그 출력으로 눈 확인
  5. DGFT는 공식 HTML 목록을 우선 파싱하고, PDF 링크는 공식 content.dgft.gov.in을 사용
"""

import os
import re
import sys
import json
import smtplib
import datetime
from io import BytesIO
from urllib.parse import urljoin
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except Exception:  # pypdf가 없어도 목록 감시는 계속 동작
    PdfReader = None

# ─────────────────────────────────────────────
# 감시 키워드 — 여기만 수정하면 됩니다
# ─────────────────────────────────────────────
# (A) 동관 직접 키워드 — 인도 공고는 영문명 + Chapter + HS코드로 표기
KEYWORDS_PRODUCT = [
    "copper",
    "copper tube",
    "copper tubes",
    "copper pipe",
    "copper pipes",
    "brass",
    "bronze",
    "refined copper",
    "chapter 74",       # 동과 그 제품 (ITC HS)
    "nfmims",           # 비철금속 수입모니터링 (동 수입 시스템)
    "7407", "7408", "7409", "7410", "7411", "7412",  # 동관·봉·선·판 HS코드
]

# (B) 제도 키워드 — 동관에 걸리면 수출 직격탄인 규제 유형
#     DGFT에서만 보조 신호로 사용
KEYWORDS_REGIME = [
    "qco",              # 품질관리명령 (Quality Control Order)
    "quality control order",
    "bis requirement",
    "bis requirements",
    "compulsory registration",
    "import monitoring",
]

# PDF 본문까지 확인할 후보를 줄이기 위한 넓은 신호
PDF_SCAN_HINTS = [
    "import policy", "policy condition", "itc", "itc(hs)", "chapter",
    "qco", "bis", "quality control", "import monitoring", "nfmims",
]

# ─────────────────────────────────────────────
# 소스 설정
# ─────────────────────────────────────────────
SOURCES = {
    "DGTR": {
        # 단일 소스 (공식 사이트 직접 접근)
        "urls": ["https://www.dgtr.gov.in/en/anti-dumping-investigation-in-india"],
        "min_items": 10,
        "parser": "parse_dgtr",
    },
    "DGFT": {
        # 1순위: 공식 DGFT Notification 페이지 직접 접근
        # 2순위 이후: 공식 장애/구조 변경 대비용 미러 fallback
        "urls": [
            "https://www.dgft.gov.in/CP/?opt=notification",
            "https://stargroup.in/dgft_notifications_view.html",
            "https://caalley.com/legal-updates/corporate-laws/dgft",
        ],
        "min_items": 15,
        "parser": "parse_dgft",   # URL로 official/stargroup/caalley 자동 구분
    },
}

STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_TO = os.environ.get("NOTIFY_TO", GMAIL_USER)


# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_state() -> dict:
    default = {"DGTR": [], "DGFT": [], "empty_streak": {}, "last_run": None}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # [] 로 비우면 전체 초기화 (PIB 방식)
            if isinstance(data, list):
                log("state.json이 [] 형식 → 전체 초기화로 인식")
                return default
            # 누락 키 보정
            for k in ("DGTR", "DGFT"):
                data.setdefault(k, [])
            data.setdefault("empty_streak", {})
            return data
        except Exception as e:
            log(f"⚠️ state.json 읽기 실패, 새로 시작: {e}")
    return default


def save_state(state: dict) -> None:
    state["last_run"] = datetime.datetime.now().isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch(url: str) -> str:
    log(f"GET {url}")
    r = requests.get(url, headers=HEADERS, timeout=30)
    log(f"STATUS {r.status_code}, LEN {len(r.text)}")
    r.raise_for_status()
    return r.text


def fetch_with_fallback(urls: list) -> tuple:
    """urls를 순서대로 시도. 첫 성공(html, url) 반환. 전부 실패면 (None, None)."""
    for url in urls:
        try:
            html = fetch(url)
            return html, url
        except Exception as e:
            log(f"  ↳ 실패, 다음 소스 시도: {type(e).__name__}: {e}")
    return None, None


def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def make_uid_part(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._/-]+", "_", (s or "").strip())[:80]


# ─────────────────────────────────────────────
# 파서 — 소스별
# ─────────────────────────────────────────────
def parse_dgtr(html: str) -> list:
    """DGTR: /anti-dumping-cases/ 링크. 슬러그가 고유 ID."""
    soup = BeautifulSoup(html, "lxml")
    items, seen_local = [], set()
    for a in soup.select('a[href*="/anti-dumping-cases/"]'):
        href = a.get("href", "").strip()
        title = a.get_text(strip=True)
        if not href or not title:
            continue
        slug = href.rstrip("/").split("/")[-1]
        if not slug or slug in seen_local:
            continue
        seen_local.add(slug)
        if href.startswith("/"):
            href = "https://www.dgtr.gov.in" + href
        items.append({"uid": f"DGTR:{slug}", "title": title, "url": href, "search_text": title})
    return items


def parse_dgft(html: str, source_url: str = "") -> list:
    """DGFT: URL로 소스를 구분해 파싱.
    - official: DGFT 공식 Notification 테이블. uid=번호:연도:공고일, url=공식 PDF.
    - stargroup: 각 공고가 독립 링크(notification-details-{번호}) + 요약. uid=상세페이지 번호.
    - caalley: '제목 [Notification No.XX]' 텍스트 나열. uid=카테고리:번호:연도.
    """
    if "dgft.gov.in" in source_url:
        return _parse_dgft_official(html, source_url)
    if "stargroup" in source_url:
        return _parse_stargroup(html)
    return _parse_caalley(html)


def _parse_dgft_official(html: str, source_url: str) -> list:
    """공식 DGFT 목록 파서.

    현재 공식 페이지는 HTML 안에 다음 컬럼을 노출한다.
    Sl.No. / Number / Year / Description / Date / CRT DT / Attachment

    페이지 구조가 약간 바뀌어도 tr/td 기반으로 먼저 읽고,
    테이블 태그가 깨진 경우에는 텍스트 정규식 fallback으로 최소 복구한다.
    """
    soup = BeautifulSoup(html, "lxml")
    items, seen_local = [], set()

    # 1) 정상 테이블 파싱
    for tr in soup.select("tr"):
        cells = [norm_space(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        if len(cells) < 6:
            continue
        if not re.fullmatch(r"\d+", cells[0] or ""):
            continue

        sl_no, number, year, desc, date, crt_dt = cells[:6]
        if not number or not desc:
            continue

        pdf_url = ""
        for a in tr.find_all("a", href=True):
            href = a["href"].strip()
            txt = a.get_text(" ", strip=True).lower()
            if "content.dgft.gov.in" in href or ".pdf" in href.lower() or "download" in txt:
                pdf_url = urljoin(source_url, href)
                break
        if not pdf_url:
            pdf_url = source_url

        uid = f"DGFT:official:{make_uid_part(number)}:{make_uid_part(year)}:{make_uid_part(date)}"
        if uid in seen_local:
            continue
        seen_local.add(uid)

        title = f"{number} / {year} — {desc} ({date})"
        search_text = " ".join([number, year, desc, date, crt_dt])
        items.append({
            "uid": uid,
            "title": title,
            "url": pdf_url,
            "date": date,
            "crt_dt": crt_dt,
            "source": "DGFT official",
            "search_text": search_text,
        })

    if items:
        return items

    # 2) fallback: 렌더링 텍스트에서 행 패턴 복구
    text = norm_space(soup.get_text(" ", strip=True))
    pdf_links = [
        urljoin(source_url, a["href"].strip())
        for a in soup.find_all("a", href=True)
        if "content.dgft.gov.in" in a["href"] or ".pdf" in a["href"].lower()
    ]

    # Sl.No. Number Year Description Date CRT DT 형식을 최대한 복구
    # Number는 '22/2026-27', '16', 'Corrigendum to Notification ...'처럼 다양할 수 있어 넓게 잡는다.
    row_re = re.compile(
        r"(?P<sl>\d+)\s+"
        r"(?P<number>.+?)\s+"
        r"(?P<year>20\d{2}(?:[-/]\d{2,4})?)\s+"
        r"(?P<desc>.+?)\s+"
        r"(?P<date>\d{2}/\d{2}/\d{4})\s+"
        r"(?P<crt>\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})",
        re.I,
    )

    for idx, m in enumerate(row_re.finditer(text)):
        number = norm_space(m.group("number"))
        year = norm_space(m.group("year"))
        desc = norm_space(m.group("desc"))
        date = norm_space(m.group("date"))
        crt_dt = norm_space(m.group("crt"))
        uid = f"DGFT:official:{make_uid_part(number)}:{make_uid_part(year)}:{make_uid_part(date)}"
        if uid in seen_local:
            continue
        seen_local.add(uid)
        pdf_url = pdf_links[idx] if idx < len(pdf_links) else source_url
        items.append({
            "uid": uid,
            "title": f"{number} / {year} — {desc} ({date})",
            "url": pdf_url,
            "date": date,
            "crt_dt": crt_dt,
            "source": "DGFT official",
            "search_text": " ".join([number, year, desc, date, crt_dt]),
        })

    return items


def _parse_stargroup(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    items, seen_local = [], set()
    for a in soup.select('a[href*="notification-details-"]'):
        href = a.get("href", "").strip()
        text = a.get_text(" ", strip=True)
        if not text.upper().startswith("DGFT"):   # Custom/GST 제외
            continue
        m = re.search(r"notification-details-(\d+)", href)
        if not m:
            continue
        uid = f"DGFT:sg:{m.group(1)}"
        if uid in seen_local:
            continue
        seen_local.add(uid)
        title = re.sub(r"^(DGFT\s*)+[\u2013-]\s*", "", text).strip()
        if href.startswith("/"):
            href = "https://stargroup.in" + href
        items.append({"uid": uid, "title": title, "url": href, "source": "stargroup", "search_text": title})
    return items


def _parse_caalley(html: str) -> list:
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    pattern = re.compile(
        r"(.+?)\[(Notification|Public Notice|Circular|Trade Notice)\s+No\.?\s*(\d+)\]",
        re.I,
    )
    items, seen_local = [], set()
    for m in pattern.finditer(text):
        title = m.group(1).strip()
        if len(title) > 220:
            title = title[-220:]
        cat, num = m.group(2).strip(), m.group(3).strip()
        fy = re.search(r"20\d{2}[-/]\d{2,4}", title)
        year_tag = fy.group(0).replace("/", "-") if fy else ""
        uid = f"DGFT:ca:{cat}:{num}:{year_tag}"
        if uid in seen_local:
            continue
        seen_local.add(uid)
        items.append({
            "uid": uid,
            "title": title,
            "url": "https://caalley.com/legal-updates/corporate-laws/dgft",
            "source": "caalley",
            "search_text": title,
        })
    return items


# ─────────────────────────────────────────────
# PDF 본문 보강 검색
# ─────────────────────────────────────────────
def should_scan_pdf(item: dict, source: str) -> bool:
    """PDF 본문까지 확인할지 판단. 공식 DGFT PDF에만 적용."""
    if source != "DGFT":
        return False
    url = item.get("url", "")
    if "content.dgft.gov.in" not in url and not url.lower().endswith(".pdf"):
        return False
    text = item.get("search_text") or item.get("title", "")
    low = text.lower()
    return any(k in low for k in PDF_SCAN_HINTS)


def fetch_pdf_text(url: str, max_bytes: int = 5_000_000, max_pages: int = 5) -> str:
    """공식 PDF 본문 일부를 텍스트로 추출. 실패 시 빈 문자열."""
    if PdfReader is None:
        log("  ↳ pypdf 미설치 → PDF 본문 검색 생략")
        return ""

    try:
        log(f"  ↳ PDF 확인 {url}")
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        content = r.content[:max_bytes]
        reader = PdfReader(BytesIO(content))
        pages = []
        for page in reader.pages[:max_pages]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        txt = norm_space(" ".join(pages))
        log(f"  ↳ PDF 텍스트 {len(txt)}자 추출")
        return txt
    except Exception as e:
        log(f"  ↳ PDF 본문 검색 실패: {type(e).__name__}: {e}")
        return ""


# ─────────────────────────────────────────────
# 분류
# ─────────────────────────────────────────────
def classify(text: str, source: str) -> list:
    """매칭 키워드 반환. DGFT는 제도 키워드도 보조 신호로 사용."""
    low = (text or "").lower()
    hits = [k for k in KEYWORDS_PRODUCT if k in low]
    if source == "DGFT":
        hits += [k for k in KEYWORDS_REGIME if k in low]
    # 순서 유지 중복 제거
    return list(dict.fromkeys(hits))


def classify_item(item: dict, source: str) -> list:
    """목록 텍스트로 1차 매칭 후, 필요한 경우 공식 PDF 본문까지 보강 검색."""
    base_text = item.get("search_text") or item.get("title", "")
    hits = classify(base_text, source)
    low = base_text.lower()
    product_hits_in_list = [k for k in KEYWORDS_PRODUCT if k in low]

    # 제목/목록에 이미 copper, chapter 74, 7411 등 직접 키워드가 있으면
    # 불필요한 PDF 다운로드를 하지 않는다.
    # 직접 키워드는 없지만 'Import Policy / Chapter / BIS / QCO' 같은 넓은 신호가 있으면
    # 공식 PDF 본문에서 동관 키워드를 추가 확인한다.
    if not product_hits_in_list and should_scan_pdf(item, source):
        pdf_text = fetch_pdf_text(item["url"])
        if pdf_text:
            item["pdf_checked"] = True
            pdf_hits = classify(pdf_text, source)
            if pdf_hits:
                item["pdf_excerpt"] = make_excerpt(pdf_text, pdf_hits[0])
                hits = list(dict.fromkeys(hits + pdf_hits))

    return hits


def make_excerpt(text: str, keyword: str, radius: int = 160) -> str:
    low = text.lower()
    idx = low.find(keyword.lower())
    if idx < 0:
        return ""
    start = max(0, idx - radius)
    end = min(len(text), idx + len(keyword) + radius)
    return text[start:end]


# ─────────────────────────────────────────────
# 알림
# ─────────────────────────────────────────────
def send_email(subject: str, body_html: str) -> None:
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_TO):
        log("⚠️ Gmail 설정 없음 → 이메일 생략 (로컬 테스트)")
        log(f"--- 미리보기 ---\n제목: {subject}\n{body_html[:800]}")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_TO
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, NOTIFY_TO.split(","), msg.as_string())
    log(f"✅ 메일 발송 완료 → {NOTIFY_TO}")


def build_email(hits_by_source: dict) -> tuple:
    total = sum(len(v) for v in hits_by_source.values())
    subject = f"🔴 [인도규제] 동관 관련 신규 {total}건 감지"
    parts = ["<h2>🔴 인도 무역규제 — 동관 관련 신규 감지</h2>"]
    label = {"DGTR": "DGTR 반덤핑 조사", "DGFT": "DGFT 수입정책 공고"}
    for src, matched in hits_by_source.items():
        if not matched:
            continue
        parts.append(f"<h3>{label.get(src, src)} — {len(matched)}건</h3><ul>")
        for it in matched:
            kw = ", ".join(it["keywords"])
            source = it.get("source", "")
            date = it.get("date", "")
            meta = " · ".join([x for x in [source, date] if x])
            pdf_note = ""
            if it.get("pdf_checked"):
                pdf_note += "<br><small>※ 공식 PDF 본문까지 확인됨</small>"
            if it.get("pdf_excerpt"):
                pdf_note += f"<br><small>본문 근거: {it['pdf_excerpt'][:350]}</small>"
            parts.append(
                f"<li><b>{it['title']}</b><br>"
                f"{('<small>' + meta + '</small><br>') if meta else ''}"
                f"매칭: <code>{kw}</code>{pdf_note}<br>"
                f"<a href='{it['url']}'>{it['url']}</a><br><br></li>"
            )
        parts.append("</ul>")
    parts.append(
        f"<hr><small>인도규제 모니터 (2차 그물: DGTR+DGFT) · "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</small>"
    )
    return subject, "\n".join(parts)


def send_structure_alert(src: str, count: int, threshold: int, streak: int, url: str) -> None:
    subject = f"⚠️ [{src} 모니터] 구조 깨짐 의심 — {count}건 (연속 {streak}회)"
    body = (
        f"<h2>⚠️ {src} 파서 이상</h2>"
        f"<p>파싱 {count}건 &lt; 임계치 {threshold}. 연속 {streak}회.</p>"
        f"<p>소스 구조/URL 변경 가능성. 점검 필요.</p>"
        f"<p>사용 소스: <a href='{url}'>{url}</a></p>"
        f"<p>DGFT의 경우 공식 사이트 실패 시 미러 fallback이 동작합니다. "
        f"단, fallback까지 임계치 미만이면 수동 점검하세요.</p>"
    )
    send_email(subject, body)


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def _run_parser(name: str, html: str, source_url: str) -> list:
    if name == "parse_dgtr":
        return parse_dgtr(html)
    return parse_dgft(html, source_url)


def collect_source_items(src: str, cfg: dict) -> tuple:
    """공식 primary + fallback URL을 순서대로 시도해 사용 가능한 items를 반환."""
    last_url, last_count = cfg["urls"][0], 0

    for idx, url in enumerate(cfg["urls"]):
        try:
            html = fetch(url)
            items = _run_parser(cfg["parser"], html, url)
            count = len(items)
            last_url, last_count = url, count

            log(f"[{src}] 후보 소스: {url}")
            log(f"[{src}] 후보 파싱 {count}건")
            for it in items[:5]:
                log(f"    · {it['title'][:90]}")

            if count >= cfg["min_items"]:
                if idx > 0:
                    log(f"✅ [{src}] fallback 소스 사용: {url}")
                return items, url, count

            log(f"⚠️ [{src}] {count}건 < 임계치 {cfg['min_items']} → 다음 fallback 시도")

        except Exception as e:
            last_url = url
            log(f"  ↳ [{src}] 소스 실패, 다음 fallback 시도: {type(e).__name__}: {e}")

    return None, last_url, last_count


def process_source(src: str, cfg: dict, state: dict) -> list:
    """한 소스 처리(공식 primary + fallback 체인). 매칭된 신규 항목 리스트 반환."""
    seen = set(state.get(src, []))

    items, used_url, count = collect_source_items(src, cfg)
    if items is None:
        log(f"❌ [{src}] 모든 소스가 실패하거나 임계치 미만")
        streak = state["empty_streak"].get(src, 0) + 1
        state["empty_streak"][src] = streak
        log(f"⚠️ [{src}] 최종 {count}건 < 임계치 {cfg['min_items']} (연속 {streak}회) → 구조 깨짐 의심")
        send_structure_alert(src, count, cfg["min_items"], streak, used_url)
        return []   # seen 미갱신 (오염 방지)

    log(f"[{src}] 최종 사용 소스: {used_url}")
    log(f"[{src}] 최종 파싱 {count}건")
    state["empty_streak"][src] = 0

    new_items = [it for it in items if it["uid"] not in seen]
    if not new_items:
        log(f"[{src}] 변화 없음")
        return []

    matched = []
    for it in new_items:
        kws = classify_item(it, src)
        if kws:
            it["keywords"] = kws
            matched.append(it)

    log(f"[{src}] 🆕 신규 {len(new_items)}건 (🔴 매칭 {len(matched)} / ⚪ 무관 {len(new_items)-len(matched)})")

    # seen 갱신 (매칭·무관 모두)
    for it in new_items:
        seen.add(it["uid"])
    state[src] = list(seen)
    return matched


def main() -> int:
    state = load_state()
    hits_by_source = {}

    for src, cfg in SOURCES.items():
        matched = process_source(src, cfg, state)
        hits_by_source[src] = matched

    total = sum(len(v) for v in hits_by_source.values())
    if total > 0:
        subject, body = build_email(hits_by_source)
        send_email(subject, body)
    else:
        log("동관 관련 신규 없음 — 메일 없음")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
