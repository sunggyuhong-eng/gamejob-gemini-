"""
gamejob_tracker.py
게임잡(gamejob.co.kr) 채용공고 트래킹 스크립트

기능
----
1. 게임잡 전체 채용공고 리스트 수집
2. 직무별(모바일 상세 페이지 파싱 + 키워드 분류), 기업별 공고 수 집계
3. 기업 로고, 대표자, 설립연도, 대표게임, 홈페이지 등 기업 정보 자동 수집 및 캐싱
4. 이전 스냅샷과 비교하여 신규 등록 / 마감(삭제) 공고 파악
5. 대시보드(index.html) 연동용 index.json 및 월별 리포트/CSV 자동 생성

사용법
------
    # 1) 사이트 구조 및 크롤링 가능 여부 테스트
    python gamejob_tracker.py --debug

    # 2) 실제 수집 실행 (정기 실행/GitHub Actions용)
    python gamejob_tracker.py --run

설치
----
    pip install requests beautifulsoup4
"""

import argparse
import csv
import json
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# 기본 설정 및 경로
# ----------------------------------------------------------------------------

BASE = "https://www.gamejob.co.kr"
MAIN_LIST_URL = BASE + "/Recruit/joblist?menucode=searchdetail"

LIST_URL_CANDIDATES = [
    BASE + "/recruit/_GI_Job_List?Page={page}",
    BASE + "/Recruit/_GI_Job_List?Page={page}",
    BASE + "/recruit/_GI_Job_List?PageIndex={page}",
    BASE + "/recruit/_GI_Job_List?page={page}",
    BASE + "/recruit/_GI_Job_List?CurPage={page}",
    BASE + "/recruit/_GI_Job_List?GI_Page={page}",
    BASE + "/Recruit/joblist?menucode=searchall&Page={page}",
    BASE + "/Recruit/joblist?menucode=searchdetail&Page={page}",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": MAIN_LIST_URL,
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

DATA_DIR = Path("data")
SNAPSHOT_DIR = DATA_DIR / "snapshots"
DEBUG_DIR = DATA_DIR / "debug"
REPORT_DIR = DATA_DIR / "reports"
CATEGORY_CACHE_PATH = DATA_DIR / "job_categories_cache.csv"
COMPANY_INFO_CACHE_PATH = DATA_DIR / "company_info_cache.csv"

for d in (SNAPSHOT_DIR, DEBUG_DIR, REPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)

GI_NO_RE = re.compile(r"GI_Read/View\?GI_No=(\d+)")
COMPANY_RE = re.compile(r"Company/Detail\?tabcode=1&M=(\d+)")

JOB_FUNCTION_KEYWORDS = {
    "프로그래밍": ["프로그래머", "개발자", "클라이언트", "서버 개발", "엔진", "백엔드",
              "프론트엔드", "인프라", "DevOps", "데이터 엔지니어", "게임 클라이언트"],
    "게임기획": ["기획자", "기획", "밸런스", "레벨디자이너", "게임 PM", "사업 PM", "개발 PM"],
    "아트": ["아티스트", "디자이너", "원화", "모델러", "애니메이터", "이펙트",
           "UI/UX", "UI Designer", "그래픽", "스파인"],
    "사운드/영상": ["사운드", "작곡", "음향", "영상"],
    "QA": ["QA", "테스터", "품질"],
    "마케팅/CM": ["마케터", "마케팅", "퍼포먼스", "UA", "홍보", "CM", "커뮤니티 매니저"],
    "사업/전략": ["사업기획", "전략", "BD", "사업개발", "사업 PM"],
    "경영지원/HR": ["HR", "인사", "채용 담당자", "총무", "재무", "회계", "법무", "경영지원", "피플팀"],
    "데이터": ["데이터 사이언티스트", "데이터 분석", "데이터 엔지니어"],
}

FIELDNAMES = [
    "gi_no", "title", "company_name", "company_id", "career",
    "education", "employment_type", "deadline_raw", "job_function", "job_categories",
    "company_logo_url", "company_ceo", "company_founded_year",
    "company_flagship_games", "company_homepage"
]


# ----------------------------------------------------------------------------
# 네트워크 및 유틸리티 함수
# ----------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(MAIN_LIST_URL, timeout=15)
    except Exception:
        pass
    return s


def request_headers_for(url_tmpl: str) -> dict:
    if "_GI_Job_List" in url_tmpl:
        return {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": MAIN_LIST_URL,
        }
    return {}


def classify_job_function(title: str) -> str:
    for func, keywords in JOB_FUNCTION_KEYWORDS.items():
        for kw in keywords:
            if kw in title:
                return func
    return "기타/미분류"


# ----------------------------------------------------------------------------
# 상세 정보 캐싱 (직종 및 기업 정보)
# ----------------------------------------------------------------------------

MOBILE_DETAIL_URL = "https://m.gamejob.co.kr/Recruit?GI_No={gi_no}"
CATEGORY_FIELD_RE = re.compile(r"모집분야\s*(.+?)\s*(?:툴팁기능|접수안내|경력)", re.DOTALL)


def load_category_cache() -> dict:
    cache = {}
    if CATEGORY_CACHE_PATH.exists():
        with CATEGORY_CACHE_PATH.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                cache[row["gi_no"]] = row["categories"]
    return cache


def save_category_cache(cache: dict):
    with CATEGORY_CACHE_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["gi_no", "categories"])
        for gi_no, categories in cache.items():
            w.writerow([gi_no, categories])


COMPANY_INFO_FIELDS = ["logo_url", "ceo_name", "founded_year", "flagship_games", "homepage_url"]


def load_company_info_cache() -> dict:
    cache = {}
    if COMPANY_INFO_CACHE_PATH.exists():
        with COMPANY_INFO_CACHE_PATH.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                cache[row["company_id"]] = {k: row.get(k, "") for k in COMPANY_INFO_FIELDS}
    return cache


def save_company_info_cache(cache: dict):
    with COMPANY_INFO_CACHE_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["company_id"] + COMPANY_INFO_FIELDS)
        w.writeheader()
        for company_id, info in cache.items():
            row = {"company_id": company_id}
            row.update({k: info.get(k, "") for k in COMPANY_INFO_FIELDS})
            w.writerow(row)


COMPANY_DETAIL_URL = "https://www.gamejob.co.kr/Company/Detail?M={company_id}"

LOGO_URL_RE = re.compile(
    r'(?:https?:)?//file\.gamejob\.co\.kr/net/Corp/CoImage/LogoView\?FN=[^"\'\s<>\)]+',
    re.IGNORECASE,
)
CEO_NAME_RE = re.compile(r'대표자명\s*\n\s*([^\n]+)')
FOUNDED_YEAR_RE = re.compile(r'설립(?:년도|연도)\s*\n\s*([^\n]+)')
FLAGSHIP_GAMES_RE = re.compile(r'대표게임\s*\n\s*([^\n]+)')
HOMEPAGE_LABEL_RE = re.compile(r'홈페이지\s*\n\s*(https?://\S+)')
LOCATION_LINE_RE = re.compile(r'회사위치\s*\n\s*([^\n]+)')
URL_IN_TEXT_RE = re.compile(r'https?://[^\s\)>"\']+')


def _clean_field(val):
    if not val:
        return ""
    val = val.strip()
    for junk in ("더보기", "닫기"):
        if val.endswith(junk):
            val = val[:-len(junk)].strip()
    return val


def fetch_company_info(session: requests.Session, company_id: str):
    info = {k: "" for k in COMPANY_INFO_FIELDS}
    try:
        r = session.get(COMPANY_DETAIL_URL.format(company_id=company_id), timeout=15)
        if r.status_code != 200:
            return info

        m = LOGO_URL_RE.search(r.text)
        if m:
            url = m.group(0)
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            info["logo_url"] = url

        text = BeautifulSoup(r.text, "html.parser").get_text("\n", strip=True)

        ceo_m = CEO_NAME_RE.search(text)
        if ceo_m:
            info["ceo_name"] = _clean_field(ceo_m.group(1))

        year_m = FOUNDED_YEAR_RE.search(text)
        if year_m:
            info["founded_year"] = _clean_field(year_m.group(1))

        games_m = FLAGSHIP_GAMES_RE.search(text)
        if games_m:
            info["flagship_games"] = _clean_field(games_m.group(1))

        home_m = HOMEPAGE_LABEL_RE.search(text)
        if home_m:
            info["homepage_url"] = home_m.group(1).strip()
        else:
            loc_m = LOCATION_LINE_RE.search(text)
            if loc_m:
                url_m = URL_IN_TEXT_RE.search(loc_m.group(1))
                if url_m:
                    info["homepage_url"] = url_m.group(0)

        return info
    except Exception:
        return info


def backfill_company_logos(jobs, verbose=True):
    session = make_session()
    info_cache = load_company_info_cache()

    company_ids = sorted({j.get("company_id") for j in jobs if j.get("company_id")})
    missing = [cid for cid in company_ids if not info_cache.get(cid, {}).get("logo_url")]

    if verbose:
        print(f"[logo] 전체 회사 수: {len(company_ids)}곳 / 정보 없는 회사: {len(missing)}곳")

    for i, company_id in enumerate(missing, 1):
        info = fetch_company_info(session, company_id)
        if any(info.values()):
            info_cache[company_id] = info
        if verbose and i % 30 == 0:
            print(f"[logo] {i}/{len(missing)}곳 완료")
        time.sleep(0.2)

    save_company_info_cache(info_cache)

    for j in jobs:
        cid = j.get("company_id")
        info = info_cache.get(cid, {}) if cid else {}
        j["company_logo_url"] = info.get("logo_url", "")
        j["company_ceo"] = info.get("ceo_name", "")
        j["company_founded_year"] = info.get("founded_year", "")
        j["company_flagship_games"] = info.get("flagship_games", "")
        j["company_homepage"] = info.get("homepage_url", "")

    return jobs


def fetch_job_detail_info(session: requests.Session, gi_no: str):
    try:
        r = session.get(MOBILE_DETAIL_URL.format(gi_no=gi_no), timeout=15)
        if r.status_code != 200:
            return None, None
        soup = BeautifulSoup(r.text, "html.parser")

        text = soup.get_text("\n", strip=True)
        m = CATEGORY_FIELD_RE.search(text)
        cats = None
        if m:
            raw = m.group(1).strip()
            parsed = [c.strip() for c in raw.split(",") if c.strip()]
            cats = parsed if parsed else None

        logo_url = None
        m2 = LOGO_URL_RE.search(r.text)
        if m2:
            logo_url = m2.group(0)
            if logo_url.startswith("//"):
                logo_url = "https:" + logo_url
            elif logo_url.startswith("http://"):
                logo_url = "https://" + logo_url[len("http://"):]

        return cats, logo_url
    except Exception:
        return None, None


def enrich_with_categories(jobs, verbose=True):
    session = make_session()
    cat_cache = load_category_cache()

    to_fetch = [j["gi_no"] for j in jobs if j["gi_no"] not in cat_cache]
    already_cached = len(jobs) - len(to_fetch)
    if verbose:
        print(f"[category] 캐시 보유: {already_cached}건 / 새로 조회할 공고: {len(to_fetch)}건")

    for i, gi_no in enumerate(to_fetch, 1):
        cats, _logo_url = fetch_job_detail_info(session, gi_no)
        if cats is not None:
            cat_cache[gi_no] = ";".join(cats)
        if verbose and i % 50 == 0:
            print(f"[category] {i}/{len(to_fetch)}건 완료")
        time.sleep(0.15)

    save_category_cache(cat_cache)

    for j in jobs:
        raw = cat_cache.get(j["gi_no"], "")
        j["job_categories"] = raw.split(";") if raw else []
        j["job_function"] = j["job_categories"][0] if j["job_categories"] else classify_job_function(j["title"])

    return jobs


# ----------------------------------------------------------------------------
# 크롤링 파이프라인
# ----------------------------------------------------------------------------

def fetch(session: requests.Session, url: str, **kwargs) -> requests.Response:
    resp = session.get(url, timeout=15, **kwargs)
    resp.raise_for_status()
    return resp


def _extract_gi_nos(html: str):
    return set(GI_NO_RE.findall(html))


def debug_dump():
    session = make_session()
    print("[debug] 후보 URL들의 1페이지 vs 2페이지 비교 중...\n")
    working = []

    for i, url_tmpl in enumerate(LIST_URL_CANDIDATES):
        extra_headers = request_headers_for(url_tmpl)
        try:
            r1 = fetch(session, url_tmpl.format(page=1), headers=extra_headers)
            time.sleep(0.3)
            r2 = fetch(session, url_tmpl.format(page=2), headers=extra_headers)
        except Exception as e:
            print(f"  - 후보 {i} ({url_tmpl}) 요청 실패: {e}")
            continue

        ids1 = _extract_gi_nos(r1.text)
        ids2 = _extract_gi_nos(r2.text)

        out1 = DEBUG_DIR / f"list_candidate_{i}_page1.html"
        out2 = DEBUG_DIR / f"list_candidate_{i}_page2.html"
        out1.write_text(r1.text, encoding="utf-8")
        out2.write_text(r2.text, encoding="utf-8")

        if not ids1:
            print(f"  - 후보 {i} ({url_tmpl}): 1페이지에서 공고링크를 못 찾음")
            continue

        is_different = bool(ids2) and ids1 != ids2
        status = "정상 작동" if is_different else "실패 (2페이지 중복)"
        print(f"  - 후보 {i} ({url_tmpl}) -> {status}")

        if is_different:
            working.append(i)

    print()
    if working:
        print(f"[debug] 정상 작동 후보: {working} -> python gamejob_tracker.py --run 실행 가능")
    else:
        print("[debug] 모든 후보의 페이지네이션 검증 실패. debug/ 폴더 HTML을 확인해주세요.")


def parse_list_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen_rows = set()

    for a in soup.find_all("a", href=True):
        m = GI_NO_RE.search(a["href"])
        if not m:
            continue
        gi_no = m.group(1)

        row = a.find_parent("tr") or a.find_parent("li") or a.parent
        row_key = id(row)
        if row_key in seen_rows:
            continue
        seen_rows.add(row_key)

        row_text = row.get_text(" ", strip=True) if row else ""

        company_name, company_id = None, None
        comp_a = row.find("a", href=COMPANY_RE) if row else None
        if comp_a:
            company_name = comp_a.get_text(strip=True)
            cm = COMPANY_RE.search(comp_a["href"])
            company_id = cm.group(1) if cm else None

        title = a.get_text(strip=True)

        career_m = re.search(r"(경력\s*무관|신입|경력\s*\d+년\s*[↑~]?\s*\d*년?)", row_text)
        edu_m = re.search(r"(학력무관|고등학교졸업 이하|대학졸업\([^)]+\))", row_text)
        employ_m = re.search(r"(정규직|계약직|인턴직|아르바이트|프리랜서|병역특례|파견직|교육생|헤드헌팅)", row_text)
        deadline_m = re.search(r"(채용시|\d{2}/\d{2}\([가-힣]\)|~\d{2}/\d{2}|\d+일 전|\d+시간 전)", row_text)

        jobs.append({
            "gi_no": gi_no,
            "title": title,
            "company_name": company_name,
            "company_id": company_id,
            "career": career_m.group(1) if career_m else None,
            "education": edu_m.group(1) if edu_m else None,
            "employment_type": employ_m.group(1) if employ_m else None,
            "deadline_raw": deadline_m.group(1) if deadline_m else None,
            "job_function": classify_job_function(title),
            "job_categories": [],
            "company_logo_url": "",
            "company_ceo": "",
            "company_founded_year": "",
            "company_flagship_games": "",
            "company_homepage": "",
        })

    return jobs


def find_working_url_template(session: requests.Session):
    for url_tmpl in LIST_URL_CANDIDATES:
        extra_headers = request_headers_for(url_tmpl)
        try:
            r1 = fetch(session, url_tmpl.format(page=1), headers=extra_headers)
            ids1 = _extract_gi_nos(r1.text)
            if not ids1:
                continue
            time.sleep(0.3)
            r2 = fetch(session, url_tmpl.format(page=2), headers=extra_headers)
            ids2 = _extract_gi_nos(r2.text)
            if ids2 and ids1 != ids2:
                return url_tmpl
        except Exception:
            continue
    return None


TOTAL_COUNT_RE = re.compile(r"전체\s*\(\s*([\d,]+)\s*\)")


def get_total_count(session: requests.Session):
    try:
        r = fetch(session, MAIN_LIST_URL)
        text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        m = TOTAL_COUNT_RE.search(text)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


def crawl_all_listings(max_pages=200, sleep_sec=0.5, verbose=True):
    session = make_session()
    all_jobs = {}
    genuinely_empty_streak = 0

    if verbose:
        print("[crawl] 페이지네이션 방식 확인 중...")
    working_url_tmpl = find_working_url_template(session)

    if not working_url_tmpl:
        if verbose:
            print("[crawl] 작동하는 URL 패턴을 찾지 못했습니다.")
        return []

    if verbose:
        print(f"[crawl] 사용할 URL 패턴: {working_url_tmpl}")

    total_count = get_total_count(session)
    if verbose and total_count:
        print(f"[crawl] 전체 공고 예상 수: {total_count}건")

    extra_headers = request_headers_for(working_url_tmpl)
    hard_stop_page = max_pages
    if total_count:
        hard_stop_page = min(max_pages, (total_count // 40) + 15)

    for page in range(1, hard_stop_page + 1):
        html = None
        try:
            r = fetch(session, working_url_tmpl.format(page=page), headers=extra_headers)
            if GI_NO_RE.search(r.text):
                html = r.text
        except Exception:
            pass

        if not html:
            genuinely_empty_streak += 1
            if genuinely_empty_streak >= 3:
                if verbose:
                    print("[crawl] 3페이지 연속 빈 데이터 -> 수집 완료")
                break
            time.sleep(sleep_sec)
            continue

        genuinely_empty_streak = 0
        jobs = parse_list_page(html)
        new_count = 0
        for j in jobs:
            if j["gi_no"] not in all_jobs:
                all_jobs[j["gi_no"]] = j
                new_count += 1

        if verbose:
            print(f"[crawl] page {page}: {len(jobs)}건 파싱 / 신규 {new_count}건 (누적 {len(all_jobs)}건)")

        if total_count and len(all_jobs) >= total_count:
            if verbose:
                print(f"[crawl] 목표 수({total_count}건) 도달 -> 종료")
            break

        time.sleep(sleep_sec)

    return list(all_jobs.values())


# ----------------------------------------------------------------------------
# 스냅샷 저장 및 리포트 생성
# ----------------------------------------------------------------------------

def save_snapshot(jobs, run_date: str) -> Path:
    path = SNAPSHOT_DIR / f"{run_date}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for j in jobs:
            row = {k: j.get(k) for k in FIELDNAMES}
            row["job_categories"] = ";".join(j.get("job_categories") or [])
            w.writerow(row)
    print(f"[save] 스냅샷 저장 완료: {path} ({len(jobs)}건)")
    update_snapshot_manifest()
    return path


def update_snapshot_manifest():
    files = sorted(p.name for p in SNAPSHOT_DIR.glob("*.csv"))
    manifest_path = SNAPSHOT_DIR / "index.json"
    manifest_path.write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[save] 매니페스트(index.json) 갱신 완료 ({len(files)}개 파일)")


def load_latest_previous_snapshot(before_date: str):
    files = sorted(SNAPSHOT_DIR.glob("*.csv"))
    files = [f for f in files if f.stem < before_date]
    if not files:
        return None
    latest = files[-1]
    with latest.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return latest.stem, rows


def _write_csv(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for r in rows:
            row = {k: r.get(k) for k in FIELDNAMES}
            if isinstance(row.get("job_categories"), list):
                row["job_categories"] = ";".join(row["job_categories"])
            w.writerow(row)


def generate_report(jobs, run_date: str):
    prev = load_latest_previous_snapshot(run_date)
    current_ids = {j["gi_no"] for j in jobs}

    lines = [f"# 게임잡 채용공고 리포트 ({run_date})\n\n",
             f"- 현재 전체 공고 수: **{len(jobs)}건**\n"]

    if prev:
        prev_date, prev_rows = prev
        prev_ids = {r["gi_no"] for r in prev_rows}
        new_ids = current_ids - prev_ids
        removed_ids = prev_ids - current_ids

        lines.append(f"- 비교 대상 이전 스냅샷: {prev_date}\n")
        lines.append(f"- 신규 등록 공고: **{len(new_ids)}건**\n")
        lines.append(f"- 삭제/마감 처리된 공고: **{len(removed_ids)}건**\n")

        new_jobs = [j for j in jobs if j["gi_no"] in new_ids]
        removed_jobs = [r for r in prev_rows if r["gi_no"] in removed_ids]

        _write_csv(REPORT_DIR / f"{run_date}_new_jobs.csv", new_jobs)
        _write_csv(REPORT_DIR / f"{run_date}_removed_jobs.csv", removed_jobs)
    else:
        lines.append("- 이전 스냅샷 없음 (최초 실행). 다음 스냅샷부터 비교 가능합니다.\n")

    func_counter = Counter()
    for j in jobs:
        cats = j.get("job_categories") or [j["job_function"]]
        for c in cats:
            func_counter[c] += 1

    lines.append("\n## 직무별 공고 수\n")
    for func, cnt in func_counter.most_common():
        lines.append(f"- {func}: {cnt}건\n")

    company_counter = Counter(j["company_name"] for j in jobs if j["company_name"])
    lines.append("\n## 공고 상위 기업 TOP 10\n")
    for name, cnt in company_counter.most_common(10):
        lines.append(f"- {name}: {cnt}건\n")

    report_path = REPORT_DIR / f"{run_date}_report.md"
    report_path.write_text("".join(lines), encoding="utf-8")
    print(f"[report] 마크다운 리포트 생성 완료: {report_path}")


# ----------------------------------------------------------------------------
# 엔트리포인트
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="게임잡 채용공고 트래킹 스크립트")
    parser.add_argument("--debug", action="store_true", help="크롤링 테스트 및 HTML 디버그")
    parser.add_argument("--run", action="store_true", help="실제 크롤링 + 리포트 생성 실행")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--skip-categories", action="store_true", help="모바일 상세 직종 수집 건너뛰기")
    args = parser.parse_args()

    if args.debug:
        debug_dump()
        return

    if args.run:
        run_date = datetime.now().strftime("%Y-%m-%d")
        print(f"[run] {run_date} 트래킹 파이프라인 시작...")
        jobs = crawl_all_listings(max_pages=args.max_pages)
        if not jobs:
            print("[run] 공고를 하나도 모으지 못했습니다.")
            return

        if not args.skip_categories:
            jobs = enrich_with_categories(jobs)

        jobs = backfill_company_logos(jobs)

        save_snapshot(jobs, run_date)
        generate_report(jobs, run_date)
        return

    parser.print_help()


if __name__ == "__main__":
    main()