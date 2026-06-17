"""
Web Board Archiver
웹 게시판 HTML 아카이버
"""
from __future__ import annotations

# ─── 의존성 자동 설치 ─────────────────────────────────────────────────────────
import sys
import os
import subprocess

# 스크립트 파일 위치를 기준으로 작업 경로 고정 (더블클릭 실행 시 CWD 오류 방지)
os.chdir(os.path.dirname(os.path.abspath(__file__)))

REQUIRED = ["requests", "beautifulsoup4", "lxml"]

def _install_deps():
    missing = []
    pkg_map = {"beautifulsoup4": "bs4"}
    for pkg in REQUIRED:
        import_name = pkg_map.get(pkg, pkg)
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return

    print("=" * 60)
    print("  필요한 패키지가 설치되어 있지 않습니다.")
    print(f"  설치 대상: {', '.join(missing)}")
    print("  자동 설치를 시작합니다...")
    print("=" * 60)

    for pkg in missing:
        print(f"\n  [설치 중] {pkg} ...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"  [완료] {pkg} 설치 성공")
        else:
            print(f"  [오류] {pkg} 설치 실패:")
            print(result.stderr)
            print()
            print("  ─────────────────────────────────────────────────")
            print("  Python이 설치되어 있지 않거나 pip가 동작하지 않을 수 있습니다.")
            print()
            print("  Python 설치 방법:")
            print("  1. https://www.python.org/downloads/ 에 접속")
            print("  2. 'Download Python 3.x.x' 버튼 클릭 후 설치")
            print("  3. 설치 시 반드시 'Add Python to PATH' 체크!")
            print("  4. 설치 후 이 스크립트를 다시 실행하세요.")
            print("  ─────────────────────────────────────────────────")
            input("\n  아무 키나 누르면 종료합니다...")
            sys.exit(1)

    print("\n  모든 패키지 설치 완료. 아카이버를 시작합니다...\n")

_install_deps()

# ─── 임포트 ──────────────────────────────────────────────────────────────────

import argparse
import hashlib
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urljoin

import requests
from bs4 import BeautifulSoup

# ─── 상수 ────────────────────────────────────────────────────────────────────

WORKERS     = 3
MAX_RETRIES = 4

PAGE_DELAY_MIN = 0.2
PAGE_DELAY_MAX = 0.5

IDS_CACHE_FILE = Path("archiver_ids_cache.txt")
PROGRESS_FILE  = Path("archiver_progress.txt")
FAIL_FILE      = Path("archiver_fail.txt")
LOG_FILE       = Path("archiver.log")

# 속도 레벨 1~10 → (딜레이 최솟값, 딜레이 최댓값)
SPEED_LEVEL_MAP = {
    1:  (4.0, 6.0),
    2:  (3.0, 4.5),
    3:  (2.0, 3.0),
    4:  (1.5, 2.5),
    5:  (0.9, 1.8),
    6:  (0.5, 1.0),
    7:  (0.3, 0.6),
    8:  (0.1, 0.3),
    9:  (0.05, 0.1),
    10: (0.0, 0.05),
}

SPEED_LEVEL_DESC = {
    1:  "매우 안전  | 초당 약 0.2개  | 일시 차단 위험: 거의 없음",
    2:  "안전       | 초당 약 0.3개  | 일시 차단 위험: 낮음",
    3:  "안전       | 초당 약 0.4개  | 일시 차단 위험: 낮음",
    4:  "보통       | 초당 약 0.5개  | 일시 차단 위험: 보통",
    5:  "보통       | 초당 약 0.8개  | 일시 차단 위험: 보통       ← 요청 제한 사이트 기본 권장",
    6:  "약간 빠름  | 초당 약 1.3개  | 일시 차단 위험: 약간 높음",
    7:  "빠름       | 초당 약 2.2개  | 일시 차단 위험: 높음",
    8:  "매우 빠름  | 초당 약 5개    | 일시 차단 위험: 매우 높음",
    9:  "초고속     | 초당 약 15개   | 일시 차단 시 약 30~40분간 접근 불가",
    10: "최속(위험) | 초당 약 30개+  | 요청 제한 사이트에서 수분 내 일시 차단 확실",
}

# ─── 전역 설정 ───────────────────────────────────────────────────────────────

SITE_CONFIG:  dict  = {}
DELAY_MIN:    float = 0.9
DELAY_MAX:    float = 1.8
RATE_LIMITED: bool  = False

# ─── 로그 ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

progress_lock = threading.Lock()
counter_lock  = threading.Lock()
fail_lock     = threading.Lock()

# ─── 사용 설명서 ─────────────────────────────────────────────────────────────

MANUAL = """
╔══════════════════════════════════════════════════════════════════════════════╗
║                     Web Board Archiver — 사용 설명서                        ║
╚══════════════════════════════════════════════════════════════════════════════╝

【개요】
  웹 게시판의 게시물 HTML을 게시물 제목으로 로컬에 저장하는 아카이버입니다.
  DCInside 갤러리 및 기타 웹 게시판을 지원합니다.

【기본 실행 방법】
  python web_board_archiver.py <게시판_URL>

  예시 (DCInside 마이너 갤러리):
    python web_board_archiver.py "https://gall.dcinside.com/mgallery/board/lists/?id=galleryname"

  예시 (다른 사이트):
    python web_board_archiver.py "https://other-site.com/board/" --link-pattern "/board/view/\?no=\d+"

【저장 파일명】
  게시물 번호 + 제목으로 저장됩니다.
  예: 123456_오늘 점심 뭐먹지.html

  제목을 가져올 수 없는 경우 게시물 번호만 사용합니다.
  예: 123456.html

【옵션】
  --link-pattern REGEX   게시물 링크 추출용 정규식 (DCInside 외 사이트에서 필요)
  --page-param   NAME    페이지네이션 파라미터명 (기본: page)
  --save-dir     PATH    저장 경로 직접 지정 (기본: ./posts/<사이트>/<갤러리ID>/)

【속도 레벨 (1~10)】
  레벨  요청 간격       초당 처리량   일시 차단 위험
  ────  ────────────    ──────────    ──────────────────────────────────
   1    4.0 ~ 6.0초     약 0.2개      거의 없음 (가장 안전)
   2    3.0 ~ 4.5초     약 0.3개      낮음
   3    2.0 ~ 3.0초     약 0.4개      낮음
   4    1.5 ~ 2.5초     약 0.5개      보통
   5    0.9 ~ 1.8초     약 0.8개      보통  ← 요청 제한 사이트 기본 권장
   6    0.5 ~ 1.0초     약 1.3개      약간 높음
   7    0.3 ~ 0.6초     약 2.2개      높음
   8    0.1 ~ 0.3초     약 5개        매우 높음
   9    0.05 ~ 0.1초    약 15개       일시 차단 시 약 30~40분간 접근 불가
  10    0.0 ~ 0.05초    약 30개+      요청 제한 사이트에서 수분 내 일시 차단 확실

【재실행 / 이어받기】
  중단된 작업은 같은 명령어로 재실행하면 자동으로 이어받습니다.
  처음부터 재시작:
    archiver_ids_cache.txt, archiver_progress.txt, archiver_fail.txt 삭제 후 재실행

╔══════════════════════════════════════════════════════════════════════════════╗
║  이 도구는 공개된 웹 페이지의 HTML을 수집합니다.                             ║
║  사이트의 이용약관과 robots.txt를 확인 후 사용하세요.                        ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

# ─── 인터랙티브 설정 ─────────────────────────────────────────────────────────

def ask(prompt: str, valid: list[str] | None = None) -> str:
    while True:
        ans = input(prompt).strip().lower()
        if valid is None or ans in valid:
            return ans
        print(f"  → 다시 입력해주세요. 가능한 값: {', '.join(valid)}")

def interactive_setup() -> tuple[bool, int]:
    print()
    print("─" * 62)

    ans = ask("  사용 설명서를 보시겠습니까? (y/n): ", ["y", "n"])
    if ans == "y":
        print(MANUAL)
        input("  [Enter] 를 누르면 계속합니다...")
        print()

    print("─" * 62)
    print()
    print("  【요청 제한 여부 확인】")
    print()
    print("  수집하려는 사이트가 짧은 시간에 요청이 많으면")
    print("  자동으로 접근을 막는 사이트입니까?")
    print()
    print("  예(y) — 요청 제한 사이트:")
    print("          요청 간격을 두고 안전하게 수집합니다.")
    print("          속도 레벨 5 이하를 권장합니다.")
    print()
    print("  아니오(n) — 일반 사이트:")
    print("          빠르게 수집합니다.")
    print("          단, 실제로 제한에 걸리면 약 30~40분간 일시적으로")
    print("          해당 사이트에 접근이 차단될 수 있습니다.")
    print()

    rate_limited = ask("  요청 제한 사이트입니까? (y/n): ", ["y", "n"]) == "y"

    print()
    print("─" * 62)
    print()
    print("  【속도 레벨 선택】  1(느림/안전) ~ 10(빠름/위험)")
    print()
    for lvl, desc in SPEED_LEVEL_DESC.items():
        print(f"   {lvl:2d}  {desc}")
    print()

    if rate_limited:
        print("  ※ 요청 제한 사이트이므로 레벨 5 이하를 강력 권장합니다.")
        print("     레벨 6 이상은 수집 중 약 30~40분간 일시 차단될 수 있습니다.")
    else:
        print("  ※ 일반 사이트는 레벨 8~10도 무방하지만,")
        print("     제한에 걸리면 약 30~40분간 일시 차단 후 재실행하세요.")
    print()

    default_level = 5 if rate_limited else 8
    while True:
        raw = input(f"  속도 레벨 입력 (1~10, 기본값 {default_level}): ").strip()
        if raw == "":
            speed_level = default_level
            break
        if raw.isdigit() and 1 <= int(raw) <= 10:
            speed_level = int(raw)
            break
        print("  → 1에서 10 사이의 숫자를 입력해주세요.")

    d_min, d_max = SPEED_LEVEL_MAP[speed_level]
    print()
    print(f"  선택: 레벨 {speed_level} — {SPEED_LEVEL_DESC[speed_level]}")
    print(f"  요청 딜레이: {d_min}~{d_max}초")
    print()
    print("─" * 62)
    print()

    return rate_limited, speed_level

# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Web Board Archiver — 웹 게시판 HTML 아카이버"
    )
    parser.add_argument("url", nargs="?", default=None,
                        help="수집할 게시판 목록 페이지 URL")
    parser.add_argument("--link-pattern", default=None, metavar="REGEX",
                        help="게시물 링크 추출용 정규식 (DCInside 외 사이트용)")
    parser.add_argument("--page-param", default="page", metavar="NAME",
                        help="페이지네이션 파라미터명 (기본: page)")
    parser.add_argument("--save-dir", default=None, metavar="PATH",
                        help="저장 경로 (기본: 자동 생성)")
    return parser.parse_args()

# ─── 사이트 설정 ─────────────────────────────────────────────────────────────

def build_site_config(url: str, link_pattern: str | None, page_param: str, save_dir_override: str | None) -> dict:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()

    if "dcinside.com" in netloc:
        qs = parse_qs(parsed.query)
        gallery_id = qs.get("id", [None])[0]
        if not gallery_id:
            print("  오류: DCInside URL에서 갤러리 ID(id=...)를 찾을 수 없습니다.")
            sys.exit(1)
        is_minor = "mgallery" in parsed.path
        base   = f"{parsed.scheme}://{parsed.netloc}"
        prefix = "/mgallery" if is_minor else ""
        save_dir = Path(save_dir_override) if save_dir_override else Path(f"posts/dcinside/{gallery_id}")
        return {
            "type": "dcinside",
            "gallery_id": gallery_id,
            "base_url": base,
            "list_url": f"{base}{prefix}/board/lists/",
            "post_url": f"{base}{prefix}/board/view/",
            "referer": base + "/",
            "page_param": "page",
            "save_dir": save_dir,
            "link_pattern": None,
        }
    else:
        if not link_pattern:
            print("  경고: DCInside 외 사이트에서는 --link-pattern 옵션을 지정하는 것을 권장합니다.")
        host_slug = re.sub(r"[^\w]", "_", netloc)
        save_dir  = Path(save_dir_override) if save_dir_override else Path(f"posts/{host_slug}")
        return {
            "type": "generic",
            "gallery_id": None,
            "base_url": f"{parsed.scheme}://{parsed.netloc}",
            "list_url": url,
            "post_url": None,
            "referer": f"{parsed.scheme}://{parsed.netloc}/",
            "page_param": page_param,
            "save_dir": save_dir,
            "link_pattern": link_pattern,
        }

# ─── Headers ─────────────────────────────────────────────────────────────────

HEADERS_POOL = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.8,en-US;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    },
]

# ─── 세션 ────────────────────────────────────────────────────────────────────

_local = threading.local()

def get_session() -> requests.Session:
    if not hasattr(_local, "session") or _local.session is None:
        s = requests.Session()
        headers = dict(random.choice(HEADERS_POOL))
        headers["Referer"] = SITE_CONFIG["referer"]
        s.headers.update(headers)
        try:
            s.get(SITE_CONFIG["list_url"], timeout=10)
            time.sleep(random.uniform(0.2, 0.5))
        except Exception:
            pass
        _local.session   = s
        _local.req_count = 0
    return _local.session

def refresh_session_if_needed():
    _local.req_count = getattr(_local, "req_count", 0) + 1
    if _local.req_count % 150 == 0:
        _local.session = None

# ─── 요청 ────────────────────────────────────────────────────────────────────

def fetch(url: str, params: dict = None) -> str | None:
    session = get_session()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                refresh_session_if_needed()
                return resp.text
            elif resp.status_code == 404:
                log.warning("404 — 삭제된 게시물")
                return None
            elif resp.status_code == 403:
                log.warning(f"403 — 헤더 교체 ({attempt}/{MAX_RETRIES})")
                session.headers.update(random.choice(HEADERS_POOL))
                time.sleep(random.uniform(5, 10))
            elif resp.status_code == 429:
                wait = 30 * attempt
                log.warning(f"429 Rate limit — {wait}초 대기")
                time.sleep(wait)
            else:
                log.warning(f"HTTP {resp.status_code} ({attempt}/{MAX_RETRIES})")
                time.sleep(random.uniform(2, 4))
        except requests.RequestException as e:
            log.error(f"요청 오류 ({attempt}/{MAX_RETRIES}): {e}")
            time.sleep(random.uniform(3, 6))
    return None

# ─── 딜레이 ──────────────────────────────────────────────────────────────────

def request_delay():
    d = random.uniform(DELAY_MIN, DELAY_MAX)
    if d > 0:
        time.sleep(d)

# ─── 제목 추출 / 파일명 변환 ─────────────────────────────────────────────────

def extract_title(html: str) -> str | None:
    """HTML에서 게시물 제목 추출. 실패 시 None."""
    soup = BeautifulSoup(html, "lxml")

    if SITE_CONFIG["type"] == "dcinside":
        # DCInside 게시물 제목 위치
        for selector in (".title_subject", "h3.title", ".view_content_wrap h3", "h2.tit"):
            el = soup.select_one(selector)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)

    # 범용 fallback: <title> 태그 (사이트명 제거 시도)
    title_tag = soup.find("title")
    if title_tag:
        raw = title_tag.get_text(strip=True)
        # " - 사이트명" 또는 "| 사이트명" 패턴 제거
        raw = re.split(r"\s*[-|]\s*", raw)[0].strip()
        if raw:
            return raw

    return None


def sanitize_filename(text: str, max_len: int = 80) -> str:
    """파일명에 사용할 수 없는 문자 제거 및 길이 제한."""
    # Windows/Linux 공통 금지 문자
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", text)
    name = re.sub(r"\s+", " ", name).strip()
    name = name[:max_len].rstrip(". ")
    return name


def make_filename(post_id: str, html: str) -> str:
    """게시물 번호 + 제목으로 파일명 생성. 제목 추출 실패 시 번호만 사용."""
    title = extract_title(html)
    if title:
        safe_title = sanitize_filename(title)
        if safe_title:
            return f"{post_id}_{safe_title}"
    return post_id


def url_to_id(url: str) -> str:
    """generic 사이트 URL → 고유 ID 추출."""
    for pattern in [
        r'[?&/](?:no|idx|num|post_id|article_id|seq)=?(\d+)',
        r'/(\d{3,})(?:[/?#]|$)',
    ]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return hashlib.md5(url.encode()).hexdigest()[:12]

# ─── 목록 수집 ───────────────────────────────────────────────────────────────

def get_post_ids_dcinside() -> list[str]:
    gallery_id = SITE_CONFIG["gallery_id"]
    list_url   = SITE_CONFIG["list_url"]
    all_ids    = []
    page       = 1

    while True:
        html = fetch(list_url, {"id": gallery_id, "page": page})
        if not html:
            log.error(f"페이지 {page} 실패")
            break

        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("tr.ub-content")
        if not rows:
            log.info(f"페이지 {page}: 게시물 없음 → 완료")
            break

        page_ids = []
        for row in rows:
            link = row.select_one("a[href*='no=']")
            if not link:
                continue
            m = re.search(r"no=(\d+)", link.get("href", ""))
            if m:
                page_ids.append(m.group(1))

        if not page_ids:
            break

        all_ids.extend(page_ids)
        log.info(f"페이지 {page}: {len(page_ids)}개 (누적 {len(all_ids)}개)")

        next_btn = soup.select_one("a.page_next")
        if not next_btn or "disabled" in next_btn.get("class", []):
            break

        page += 1
        time.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))

    return all_ids


def get_post_urls_generic() -> list[str]:
    list_url     = SITE_CONFIG["list_url"]
    page_param   = SITE_CONFIG["page_param"]
    base_url     = SITE_CONFIG["base_url"]
    link_pattern = SITE_CONFIG.get("link_pattern")
    all_urls     = []
    seen         = set()
    page         = 1

    while True:
        sep = "&" if "?" in list_url else "?"
        paged_url = f"{list_url}{sep}{page_param}={page}" if page > 1 else list_url
        html = fetch(paged_url)
        if not html:
            log.error(f"페이지 {page} 실패")
            break

        soup  = BeautifulSoup(html, "lxml")
        links = soup.find_all("a", href=True)
        page_urls = []

        for a in links:
            href = a["href"]
            if link_pattern and not re.search(link_pattern, href):
                continue
            full = href if href.startswith("http") else urljoin(base_url, href)
            if full not in seen:
                seen.add(full)
                page_urls.append(full)

        if not page_urls:
            log.info(f"페이지 {page}: 새 게시물 없음 → 완료")
            break

        all_urls.extend(page_urls)
        log.info(f"페이지 {page}: {len(page_urls)}개 (누적 {len(all_urls)}개)")
        page += 1
        time.sleep(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX))

    return all_urls


def get_post_ids() -> list[str]:
    if IDS_CACHE_FILE.exists():
        ids = [i for i in IDS_CACHE_FILE.read_text(encoding="utf-8").splitlines() if i.strip()]
        log.info(f"[캐시] {len(ids)}개 로드 — 목록 수집 생략")
        return ids

    log.info("캐시 없음 → 목록 수집 시작")
    all_ids = get_post_ids_dcinside() if SITE_CONFIG["type"] == "dcinside" else get_post_urls_generic()
    IDS_CACHE_FILE.write_text("\n".join(all_ids), encoding="utf-8")
    log.info(f"캐시 저장: {len(all_ids)}개")
    return all_ids

# ─── 완료 / 실패 관리 ────────────────────────────────────────────────────────

def load_done(save_dir: Path) -> set[str]:
    """완료된 post_id 집합 반환. 파일명 앞부분(번호)으로 판별."""
    done = set()
    if PROGRESS_FILE.exists():
        done |= set(PROGRESS_FILE.read_text(encoding="utf-8").splitlines())
    # 저장된 HTML 파일명에서 번호(언더스코어 앞) 추출
    for p in save_dir.glob("*.html"):
        done.add(p.stem.split("_")[0])
    return done

def load_fail() -> set[str]:
    if FAIL_FILE.exists():
        return set(f for f in FAIL_FILE.read_text(encoding="utf-8").splitlines() if f.strip())
    return set()

def mark_done(post_id: str):
    with progress_lock:
        with PROGRESS_FILE.open("a", encoding="utf-8") as f:
            f.write(post_id + "\n")

def mark_fail(post_id: str):
    with fail_lock:
        with FAIL_FILE.open("a", encoding="utf-8") as f:
            f.write(post_id + "\n")

def remove_from_fail(resolved: set[str]):
    if not FAIL_FILE.exists() or not resolved:
        return
    remaining = [
        l for l in FAIL_FILE.read_text(encoding="utf-8").splitlines()
        if l.strip() and l.strip() not in resolved
    ]
    FAIL_FILE.write_text("\n".join(remaining), encoding="utf-8")

# ─── 진행률 ──────────────────────────────────────────────────────────────────

def format_eta(seconds: float) -> str:
    if seconds < 0:
        return "계산 중"
    td = timedelta(seconds=int(seconds))
    h, rem = divmod(td.seconds, 3600)
    m, s   = divmod(rem, 60)
    h += td.days * 24
    return f"{h}시간 {m}분 {s}초"

def print_progress(done_count: int, total: int, start_time: float, success: int, fail: int):
    pct       = done_count / total * 100 if total else 0
    elapsed   = time.time() - start_time
    speed     = done_count / elapsed if elapsed > 0 else 0
    remaining = (total - done_count) / speed if speed > 0 else -1
    bar_len   = 30
    filled    = int(bar_len * pct / 100)
    bar       = "█" * filled + "░" * (bar_len - filled)
    log.info(
        f"[{bar}] {pct:.1f}% | {done_count}/{total} | "
        f"성공 {success} / 실패 {fail} | "
        f"속도 {speed:.2f}개/초 | 잔여 {format_eta(remaining)}"
    )

# ─── 게시물 저장 ─────────────────────────────────────────────────────────────

def save_post(post_id: str, save_dir: Path) -> bool:
    # 이미 같은 번호로 시작하는 파일이 있으면 건너뜀
    existing = list(save_dir.glob(f"{post_id}_*.html")) + list(save_dir.glob(f"{post_id}.html"))
    if existing:
        return True

    request_delay()

    if SITE_CONFIG["type"] == "dcinside":
        html = fetch(SITE_CONFIG["post_url"], {"id": SITE_CONFIG["gallery_id"], "no": post_id})
    else:
        html = fetch(post_id)   # generic: post_id가 전체 URL

    if not html:
        log.error(f"[FAIL] {post_id}")
        mark_fail(post_id)
        return False

    # 중복 재확인
    existing = list(save_dir.glob(f"{post_id}_*.html")) + list(save_dir.glob(f"{post_id}.html"))
    if existing:
        return True

    # generic 사이트는 URL → 번호 변환 후 제목 추출
    if SITE_CONFIG["type"] == "generic":
        numeric_id = url_to_id(post_id)
        fname = make_filename(numeric_id, html)
    else:
        fname = make_filename(post_id, html)

    out_path = save_dir / f"{fname}.html"
    out_path.write_text(html, encoding="utf-8")
    mark_done(post_id if SITE_CONFIG["type"] == "dcinside" else url_to_id(post_id))
    log.info(f"[SAVE] {out_path.name}")
    return True

# ─── 병렬 실행 ───────────────────────────────────────────────────────────────

def run_batch(todo: list[str], save_dir: Path, label: str):
    success, fail = 0, 0
    completed     = 0
    start_time    = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(save_post, pid, save_dir): pid for pid in todo}
        for future in as_completed(futures):
            ok = future.result()
            with counter_lock:
                if ok:
                    success += 1
                else:
                    fail += 1
                completed += 1
                if completed % 20 == 0:
                    print_progress(completed, len(todo), start_time, success, fail)

    elapsed = time.time() - start_time
    log.info(
        f"=== {label} 완료 | 성공: {success} / 실패: {fail} / "
        f"소요: {format_eta(elapsed)} ==="
    )
    return success, fail

# ─── 메인 ────────────────────────────────────────────────────────────────────

def main():
    global SITE_CONFIG, DELAY_MIN, DELAY_MAX, RATE_LIMITED

    args = parse_args()

    url = args.url
    if not url:
        print()
        print("─" * 62)
        print("  Web Board Archiver — 웹 게시판 HTML 아카이버")
        print("─" * 62)
        url = input("  수집할 게시판 URL을 입력하세요: ").strip()
        if not url:
            print("  URL이 입력되지 않았습니다. 종료합니다.")
            sys.exit(1)

    RATE_LIMITED, speed_level = interactive_setup()
    DELAY_MIN, DELAY_MAX = SPEED_LEVEL_MAP[speed_level]

    SITE_CONFIG = build_site_config(url, args.link_pattern, args.page_param, args.save_dir)
    save_dir: Path = SITE_CONFIG["save_dir"]
    save_dir.mkdir(parents=True, exist_ok=True)

    # 저장 경로 확인
    print("─" * 62)
    print()
    print(f"  수집된 게시물은 아래 경로에 저장됩니다:")
    print()
    print(f"    {save_dir.resolve()}")
    print()
    while True:
        confirm = input("  확인했으면 OK 를 입력하세요: ").strip()
        if confirm.upper() == "OK":
            break
        print("  → OK 를 입력해주세요.")
    print()
    print("─" * 62)
    print()

    log.info("Web Board Archiver 시작")
    log.info(f"사이트 타입: {SITE_CONFIG['type']}")
    log.info(f"저장 경로: {save_dir}")
    log.info(f"워커: {WORKERS}개 / 속도 레벨: {speed_level} / 딜레이: {DELAY_MIN}~{DELAY_MAX}초 / 최대 재시도: {MAX_RETRIES}회")
    log.info(f"요청 제한 사이트: {'예' if RATE_LIMITED else '아니오'}")

    all_ids   = get_post_ids()
    total_all = len(all_ids)
    log.info(f"전체 게시물: {total_all}개")

    if not all_ids:
        log.error("게시물 없음. 종료.")
        return

    done_ids     = load_done(save_dir)
    already_done = sum(1 for pid in all_ids if pid in done_ids)
    todo         = [pid for pid in all_ids if pid not in done_ids]

    log.info(f"이미 완료: {already_done}개 ({already_done/total_all*100:.1f}%)")
    log.info(f"남은 작업: {len(todo)}개")

    prev_fails = load_fail() - done_ids
    if prev_fails:
        log.info(f"이전 실패 목록 {len(prev_fails)}개 추가")
        todo = list(set(todo) | prev_fails)

    if not todo:
        log.info("모든 게시물 저장 완료.")
        return

    log.info("=== 게시물 저장 시작 ===")
    run_batch(todo, save_dir, "본 수집")

    retry_list = list(load_fail() - load_done(save_dir))
    if retry_list:
        log.info(f"=== 실패 항목 재시도: {len(retry_list)}개 ===")
        time.sleep(random.uniform(3, 6))
        run_batch(retry_list, save_dir, "재시도")

        resolved = {p.stem.split("_")[0] for p in save_dir.glob("*.html")} & set(retry_list)
        remove_from_fail(resolved)
        remaining_fails = len(load_fail() - load_done(save_dir))
        if remaining_fails:
            log.info(f"최종 실패(삭제된 게시물 가능성): {remaining_fails}개 → archiver_fail.txt 확인")
        else:
            log.info("재시도 후 실패 없음.")

    log.info(f"저장 위치: {save_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  사용자에 의해 중단되었습니다.")
    except Exception as e:
        print("\n" + "=" * 62)
        print("  [오류] 예기치 않은 문제가 발생했습니다:")
        print(f"  {type(e).__name__}: {e}")
        print()
        print("  archiver.log 파일을 확인하거나, 아래 오류 내용을")
        print("  개발자에게 전달해 주세요.")
        print("=" * 62)
        import traceback
        traceback.print_exc()
    finally:
        input("\n  아무 키나 누르면 창을 닫습니다...")
