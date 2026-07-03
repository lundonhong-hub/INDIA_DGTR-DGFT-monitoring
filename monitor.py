#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
인도 무역규제 모니터 — DGTR + DGFT 통합
- DGTR: 반덤핑 조사 목록 (dgtr.gov.in, 공식 사이트 직접 접근)
- DGFT: Notification 표 (dgft.gov.in, 공식 사이트 직접 접근)
- 동관(copper) 키워드 매칭된 신규 항목만 메일 발송
- 메일 발송 시 DGTR/DGFT 중 한쪽이 0건이어도 "0건"으로 명시
- state.json을 [] 로 비우면 전체 재알림
"""

import os
import re
import sys
import json
import smtplib
import datetime
from html import escape
from urllib.parse import urljoin
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup

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
    "nfmims",           # 비철금속 수입모니터링
    "7407", "7408", "7409", "7410", "7411", "7412",  # 동관·봉·선·판 HS코드
]

# (B) 제도 키워드 — DGFT에서는 QCO/BIS 자체도 중요 신호로 감시
KEYWORDS_REGIME = [
    "qco",
    "quality control order",
    "bis requirement",
    "compulsory registration",
    "import monitoring",
]

# ─────────────────────────────────────────────
# 소스 설정
# ─────────────────────────────────────────────
SOURCES = {
    "DGTR": {
        "urls": ["https://www.dgtr.gov.in/en/anti-dumping-investigation-in-india"],
        "min_items": 10,
        "parser": "parse_dgtr",
    },
    "DGFT": {
        # 공식 DGFT Notification 페이지 직접 접근.
        # GitHub Actions에서 /CP/?opt=notification 이 502를 줄 수 있어 index.jsp를 1순위로 사용.
        "urls": [
            "https://www.dgft.gov.in/CP/index.jsp?opt=notification",
            "https://www.dgft.gov.in/CP/?opt=notification",
        ],
        # 공식 화면 기본 표시가 10건이므로 15로 두면 오탐 구조경보가 납니다.
        "min_items": 5,
        "parser": "parse_dgft",
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
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
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
            # state.json을 [] 로 비우면 전체 초기화
            if isinstance(data, list):
                log("state.json이 [] 형식 → 전체 초기화로 인식")
                return default
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
    last_error = None
    for url in urls:
        try:
            html = fetch(url)
            return html, url
        except Exception as e:
            last_error = e
            log(f"  ↳ 실패, 다음 소스 시도: {type(e).__name__}: {e}")
    if last_error:
        log(f"❌ 최종 수집 실패: {type(last_error).__name__}: {last_error}")
    return None, None


def normalize_dgft_attachment_url(href: str, source_url: str) -> str:
    """
    DGFT 공식 Notification 표의 Attachment 링크만 안전하게 정규화.
    예전 미러 사이트의 상대 slug가 메일에 들어가면 DNS 오류가 나므로 방어한다.
    """
    href = (href or "").strip()
    if not href:
        return ""

    low = href.lower()

    # 공식 PDF 링크: 가장 정상적인 케이스
    if low.startswith("https://content.dgft.gov.in/"):
        return href

    # 이미 절대 URL이면 사용하되, 이상한 상대 slug는 여기로 들어오지 않음
    if low.startswith("https://") or low.startswith("http://"):
        return href

    # 공식 사이트 내부 절대경로
    if href.startswith("/"):
        return urljoin("https://www.dgft.gov.in", href)

    # 그 외 "dgft-public-notice-....html" 같은 상대 slug는 버림
    return ""


# ─────────────────────────────────────────────
# 파서 — 소스별
# ─────────────────────────────────────────────
def parse_dgtr(html: str) -> list:
    """DGTR: /anti-dumping-cases/ 링크. 슬러그가 고유 ID."""
    soup = BeautifulSoup(html, "lxml")
    items, seen_local = [], set()

    for a in soup.select('a[href*="/anti-dumping-cases/"]'):
        href = a.get("href", "").strip()
        title = a.get_text(" ", strip=True)
        if not href or not title:
            continue

        slug = href.rstrip("/").split("/")[-1]
        if not slug or slug in seen_local:
            continue
        seen_local.add(slug)

        if href.startswith("/"):
            href = "https://www.dgtr.gov.in" + href

        items.append({
            "uid": f"DGTR:{slug}",
            "title": title,
            "url": href,
        })

    return items


def parse_dgft(html: str, source_url: str = "") -> list:
    """
    DGFT 공식 Notification 표 직접 파싱.
    대상 URL:
      https://www.dgft.gov.in/CP/index.jsp?opt=notification
    표 컬럼:
      Sl.No. / Number / Year / Description / Date / CRT DT / Attachment
    """
    soup = BeautifulSoup(html, "lxml")
    items, seen_local = [], set()

    table = soup.select_one("table#metaTable") or soup.find("table")
    if not table:
        return items

    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue

        number = tds[1].get_text(" ", strip=True)
        year = tds[2].get_text(" ", strip=True)
        desc = tds[3].get_text(" ", strip=True)
        date = tds[4].get_text(" ", strip=True)

        if not number or not desc:
            continue

        a = tr.select_one("a[href]")
        url = normalize_dgft_attachment_url(a.get("href", "") if a else "", source_url)

        uid = f"DGFT:Notification:{number}:{year}"
        if uid in seen_local:
            continue
        seen_local.add(uid)

        title = f"Notification {number} ({date}) – {desc}"
        items.append({
            "uid": uid,
            "title": title,
            "url": url,
            "number": number,
            "year": year,
            "date": date,
            "description": desc,
        })

    # 일부 HTML에서 tbody가 생략될 경우를 대비한 fallback
    if not items:
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            number = tds[1].get_text(" ", strip=True)
            year = tds[2].get_text(" ", strip=True)
            desc = tds[3].get_text(" ", strip=True)
            date = tds[4].get_text(" ", strip=True)
            if not number or not desc:
                continue
            a = tr.select_one("a[href]")
            url = normalize_dgft_attachment_url(a.get("href", "") if a else "", source_url)
            uid = f"DGFT:Notification:{number}:{year}"
            if uid in seen_local:
                continue
            seen_local.add(uid)
            items.append({
                "uid": uid,
                "title": f"Notification {number} ({date}) – {desc}",
                "url": url,
                "number": number,
                "year": year,
                "date": date,
                "description": desc,
            })

    return items


# ─────────────────────────────────────────────
# 분류
# ─────────────────────────────────────────────
def classify(title: str, source: str) -> list:
    """매칭 키워드 반환. DGFT는 제도 키워드도 보조 신호로 사용."""
    low = title.lower()

    hits = []
    for k in KEYWORDS_PRODUCT:
        if k in low and k not in hits:
            hits.append(k)

    if source == "DGFT":
        for k in KEYWORDS_REGIME:
            if k in low and k not in hits:
                hits.append(k)

    return hits


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

    label = {
        "DGTR": "DGTR 반덤핑 조사",
        "DGFT": "DGFT 공식 Notification",
    }

    parts = [
        "<h2>🔴 인도 무역규제 — 동관 관련 신규 감지</h2>",
        "<p>이번 실행에서 새로 감지된 동관 관련 항목 기준입니다.</p>",
    ]

    # 항상 DGTR, DGFT 순서로 표기. 한쪽이 0건이어도 0건이라고 명시.
    for src in ("DGTR", "DGFT"):
        matched = hits_by_source.get(src, [])
        parts.append(f"<h3>{escape(label.get(src, src))} — {len(matched)}건</h3>")

        if not matched:
            parts.append("<p>동관 관련 신규 감지 항목 없음</p>")
            continue

        parts.append("<ul>")
        for it in matched:
            title = escape(it.get("title", ""))
            url = it.get("url", "")
            kw = escape(", ".join(it.get("keywords", [])))

            parts.append(f"<li><b>{title}</b><br>")
            parts.append(f"매칭: <code>{kw}</code><br>")

            # URL이 안전하게 정규화된 경우에만 링크 출력.
            # 빈 URL이면 깨진 링크 대신 안내 문구를 출력.
            if url:
                safe_url = escape(url, quote=True)
                parts.append(f"<a href=\"{safe_url}\">{safe_url}</a>")
            else:
                parts.append("<i>첨부 링크 없음 또는 비정상 상대경로로 판단되어 링크 제외</i>")

            parts.append("<br><br></li>")
        parts.append("</ul>")

    parts.append(
        f"<hr><small>인도규제 모니터 (DGTR + DGFT 공식 Notification) · "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</small>"
    )

    return subject, "\n".join(parts)


def send_structure_alert(src: str, count: int, threshold: int, streak: int, url: str) -> None:
    subject = f"⚠️ [{src} 모니터] 구조 깨짐 의심 — {count}건 (연속 {streak}회)"
    safe_url = escape(url or "", quote=True)
    body = (
        f"<h2>⚠️ {escape(src)} 파서 이상</h2>"
        f"<p>파싱 {count}건 &lt; 임계치 {threshold}. 연속 {streak}회.</p>"
        f"<p>소스 구조/URL 변경 가능성. 점검 필요.</p>"
        f"<p><a href=\"{safe_url}\">{safe_url}</a></p>"
    )
    send_email(subject, body)


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def _run_parser(name: str, html: str, source_url: str) -> list:
    if name == "parse_dgtr":
        return parse_dgtr(html)
    if name == "parse_dgft":
        return parse_dgft(html, source_url)
    raise ValueError(f"Unknown parser: {name}")


def process_source(src: str, cfg: dict, state: dict) -> list:
    """한 소스 처리. 매칭된 신규 항목 리스트 반환."""
    seen = set(state.get(src, []))

    html, used_url = fetch_with_fallback(cfg["urls"])
    if html is None:
        log(f"❌ [{src}] 모든 소스 수집 실패")
        streak = state["empty_streak"].get(src, 0) + 1
        state["empty_streak"][src] = streak
        send_structure_alert(src, 0, cfg["min_items"], streak, cfg["urls"][0])
        return []

    items = _run_parser(cfg["parser"], html, used_url)
    count = len(items)

    log(f"[{src}] 파싱 {count}건")
    for it in items[:5]:
        log(f"    · {it['title'][:100]}")

    # 조용한 0건 방어
    if count < cfg["min_items"]:
        streak = state["empty_streak"].get(src, 0) + 1
        state["empty_streak"][src] = streak
        log(f"⚠️ [{src}] {count}건 < 임계치 {cfg['min_items']} (연속 {streak}회) → 구조 깨짐 의심")
        send_structure_alert(src, count, cfg["min_items"], streak, used_url)
        return []   # seen 미갱신

    state["empty_streak"][src] = 0

    new_items = [it for it in items if it["uid"] not in seen]
    if not new_items:
        log(f"[{src}] 변화 없음")
        return []

    matched = []
    for it in new_items:
        kws = classify(it["title"], src)
        if kws:
            it["keywords"] = kws
            matched.append(it)

    log(f"[{src}] 🆕 신규 {len(new_items)}건 (🔴 매칭 {len(matched)} / ⚪ 무관 {len(new_items) - len(matched)})")

    # 매칭/무관 모두 seen 갱신
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
