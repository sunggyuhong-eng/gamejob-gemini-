"""
gamejob_tracker.py
게임잡 채용공고 트래킹 + 회사 로고 수집 + Claude 3.5 Sonnet AI 딥 분석 리포트 자동 생성
"""

import argparse
import csv
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
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

GI_NO_RE = re.compile(r"GI_Read/View\?GI_No=(\d+)", re.IGNORECASE)
COMPANY_RE = re.compile(r"Company/Detail\?.*?\bM=(\d+)", re.IGNORECASE)

JOB_FUNCTION_KEYWORDS = {
    "프로그래밍": ["프로그래머", "개발자", "클라이언트", "서버 개발", "엔진", "백엔드", "프론트엔드", "인프라", "DevOps", "데이터 엔지니어"],
    "게임기획": ["기획자", "기획", "밸런스", "레벨디자이너", "게임 PM", "사업 PM"],
    "아트": ["아티스트", "디자이너", "원화", "모델러", "애니메이터", "이펙트", "UI/UX", "그래픽"],
    "사운드/영상": ["사운드", "작곡", "음향", "영상"],
    "QA": ["QA", "테스터", "품질"],
    "마케팅/CM": ["마케터", "마케팅", "퍼포먼스", "UA", "홍보", "CM"],
    "사업/전략": ["사업기획", "전략", "BD", "사업개발"],
    "경영지원/HR": ["HR", "인사", "채용", "총무", "재무", "회계", "경영지원"],
    "데이터": ["데이터 사이언티스트", "데이터 분석"],
}

FIELDNAMES = [
    "gi_no", "title", "company_name", "company_id", "career",
    "education", "employment_type", "deadline_raw", "job_function", "job_categories",
    "company_logo_url", "company_ceo", "company_founded_year",
    "company_flagship_games", "company_homepage"
]

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    try: s.get(MAIN_LIST_URL, timeout=15)
    except Exception: pass
    return s

def request_headers_for(url_tmpl: str) -> dict:
    if "_GI_Job_List" in url_tmpl: return {"X-Requested-With": "XMLHttpRequest", "Referer": MAIN_LIST_URL}
    return {}

def classify_job_function(title: str) -> str:
    for func, keywords in JOB_FUNCTION_KEYWORDS.items():
        for kw in keywords:
            if kw in title: return func
    return "기타/미분류"

def fetch_google_news_rss(query: str, limit=6) -> list:
    encoded_q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_q}&hl=ko&gl=KR&ceid=KR:ko"
    headlines = []
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            for item in root.findall(".//item"):
                t = item.find("title")
                if t is not None and t.text:
                    ct = t.text.split(" - ")[0].strip()
                    if ct and ct not in headlines: headlines.append(ct)
                    if len(headlines) >= limit: break
    except Exception as e:
        print(f"[news] 구글 뉴스 수집 실패 ({query}): {e}")
    return headlines

def fetch_gamejob_news(session: requests.Session, limit=8) -> list:
    url = "https://www.gamejob.co.kr/Community/news?Comm_Stat=0"
    headlines = []
    try:
        r = session.get(url, timeout=15)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                if "/Community/news/detail" in a["href"] or "news" in a["href"]:
                    t = a.get_text(strip=True)
                    if t and len(t) > 6 and t not in headlines and "뉴스" not in t:
                        headlines.append(t)
                        if len(headlines) >= limit: break
    except Exception as e:
        print(f"[news] 게임잡 뉴스 크롤링 실패: {e}")
    return headlines

def fetch_dc_gamemeca_news(session: requests.Session, limit=8) -> list:
    url = "https://gall.dcinside.com/board/lists/?id=gamemeca"
    headlines = []
    try:
        r = session.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=15)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for tr in soup.find_all("tr", class_="ub-content"):
                td = tr.find("td", class_="gall_tit")
                if td and td.find("a"):
                    t = td.find("a").get_text(strip=True)
                    if t and len(t) > 5 and t not in headlines:
                        headlines.append(t)
                        if len(headlines) >= limit: break
    except Exception as e:
        print(f"[news] DC 게임메카 크롤링 실패: {e}")
    return headlines

MOBILE_DETAIL_URL = "https://m.gamejob.co.kr/Recruit?GI_No={gi_no}"
CATEGORY_FIELD_RE = re.compile(r"모집분야\s*(.+?)\s*(?:툴팁기능|접수안내|경력)", re.DOTALL)

def load_category_cache() -> dict:
    cache = {}
    if CATEGORY_CACHE_PATH.exists():
        with CATEGORY_CACHE_PATH.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f): cache[row["gi_no"]] = row["categories"]
    return cache

def save_category_cache(cache: dict):
    with CATEGORY_CACHE_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["gi_no", "categories"])
        for gi_no, categories in cache.items(): w.writerow([gi_no, categories])

COMPANY_INFO_FIELDS = ["logo_url", "ceo_name", "founded_year", "flagship_games", "homepage_url"]

def load_company_info_cache() -> dict:
    cache = {}
    if COMPANY_INFO_CACHE_PATH.exists():
        with COMPANY_INFO_CACHE_PATH.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f): cache[row["company_id"]] = {k: row.get(k, "") for k in COMPANY_INFO_FIELDS}
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
LOGO_URL_RE = re.compile(r'(?:https?:)?//(?:file|i)\.gamejob\.co\.kr/net/Corp/CoImage/LogoView\?FN=[^"\'\s<>\)]+', re.IGNORECASE)

def fetch_company_info(session: requests.Session, company_id: str):
    info = {k: "" for k in COMPANY_INFO_FIELDS}
    try:
        r = session.get(COMPANY_DETAIL_URL.format(company_id=company_id), timeout=15)
        if r.status_code != 200: return info
        m = LOGO_URL_RE.search(r.text)
        if m:
            u = m.group(0)
            info["logo_url"] = "https:" + u if u.startswith("//") else u
        return info
    except Exception: return info

def backfill_company_logos(jobs):
    session = make_session()
    info_cache = load_company_info_cache()
    missing = [cid for cid in sorted({j.get("company_id") for j in jobs if j.get("company_id")}) if not info_cache.get(cid, {}).get("logo_url")]
    for company_id in missing:
        info = fetch_company_info(session, company_id)
        if any(info.values()): info_cache[company_id] = info
        time.sleep(0.1)
    save_company_info_cache(info_cache)
    for j in jobs:
        cid = j.get("company_id")
        info = info_cache.get(cid, {}) if cid else {}
        j["company_logo_url"] = info.get("logo_url", "")
    return jobs

def fetch_job_detail_info(session: requests.Session, gi_no: str):
    try:
        r = session.get(MOBILE_DETAIL_URL.format(gi_no=gi_no), timeout=15)
        if r.status_code != 200: return None, None
        soup = BeautifulSoup(r.text, "html.parser")
        m = CATEGORY_FIELD_RE.search(soup.get_text("\n", strip=True))
        cats = [c.strip() for c in m.group(1).split(",") if c.strip()] if m else None
        return cats, None
    except Exception: return None, None

def enrich_with_categories(jobs):
    session = make_session()
    cat_cache = load_category_cache()
    to_fetch = [j["gi_no"] for j in jobs if j["gi_no"] not in cat_cache]
    for gi_no in to_fetch:
        cats, _ = fetch_job_detail_info(session, gi_no)
        if cats: cat_cache[gi_no] = ";".join(cats)
        time.sleep(0.1)
    save_category_cache(cat_cache)
    for j in jobs:
        raw = cat_cache.get(j["gi_no"], "")
        j["job_categories"] = raw.split(";") if raw else []
        j["job_function"] = j["job_categories"][0] if j["job_categories"] else classify_job_function(j["title"])
    return jobs

def fetch(session: requests.Session, url: str, **kwargs) -> requests.Response:
    resp = session.get(url, timeout=15, **kwargs)
    resp.raise_for_status()
    return resp

def parse_list_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    jobs, seen_rows = [], set()
    for a in soup.find_all("a", href=True):
        m = GI_NO_RE.search(a["href"])
        if not m: continue
        gi_no = m.group(1)
        row = a.find_parent("tr") or a.find_parent("li") or a.parent
        row_key = id(row)
        if row_key in seen_rows: continue
        seen_rows.add(row_key)
        comp_a = row.find("a", href=COMPANY_RE) if row else None
        company_name, company_id = None, None
        if comp_a:
            company_name = comp_a.get_text(strip=True)
            cm = COMPANY_RE.search(comp_a["href"])
            company_id = cm.group(1) if cm else None
        title = a.get_text(strip=True)
        jobs.append({
            "gi_no": gi_no, "title": title, "company_name": company_name, "company_id": company_id,
            "employment_type": "정규직", "deadline_raw": "", "job_function": classify_job_function(title), "job_categories": []
        })
    return jobs

def crawl_all_listings():
    session = make_session()
    all_jobs = {}
    for page in range(1, 150):
        try:
            r = fetch(session, f"{BASE}/recruit/_GI_Job_List?Page={page}", headers={"X-Requested-With": "XMLHttpRequest", "Referer": MAIN_LIST_URL})
            jobs = parse_list_page(r.text)
            if not jobs: break
            for j in jobs:
                if j["gi_no"] not in all_jobs: all_jobs[j["gi_no"]] = j
            time.sleep(0.3)
        except Exception: break
    return list(all_jobs.values())

def load_latest_previous_snapshot(before_date: str):
    files = [f for f in sorted(SNAPSHOT_DIR.glob("*.csv")) if f.stem < before_date]
    if not files: return None
    with files[-1].open(encoding="utf-8-sig") as f:
        return files[-1].stem, list(csv.DictReader(f))

# Claude 3.5 Sonnet 리포트 생성 함수
def generate_ai_monthly_report(jobs, run_date: str):
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not api_key:
        print("[ai] ANTHROPIC_API_KEY 환경변수가 없습니다. 리포트 생성을 건너끕니다.")
        return

    prev = load_latest_previous_snapshot(run_date)
    if not prev: return
    prev_date, prev_rows = prev
    session = make_session()

    print("[ai] 뉴스 데이터 크롤링 중...")
    gnews = fetch_google_news_rss("게임 업계 채용")
    gj_news = fetch_gamejob_news(session)
    dc_news = fetch_dc_gamemeca_news(session)

    cur_comp = Counter(j["company_name"] for j in jobs if j.get("company_name"))
    prev_comp = Counter(r["company_name"] for r in prev_rows if r.get("company_name"))
    comp_deltas = sorted([(c, cur_comp.get(c, 0) - prev_comp.get(c, 0)) for c in set(cur_comp.keys()) | set(prev_comp.keys())], key=lambda x: x[1], reverse=True)
    
    top_inc = comp_deltas[0] if comp_deltas else ("없음", 0)
    top_dec = comp_deltas[-1] if comp_deltas else ("없음", 0)

    prompt = f"""너는 게임 산업 전문 수석 애널리스트(Senior Industry Analyst)야.
아래 실시간 수집된 게임 업계 채용 데이터 및 최신 뉴스 제목들을 바탕으로 전문적인 월간 채용 트렌드 리포트를 작성해줘.

[데이터 기준: {run_date} (전월: {prev_date})]
- 전체 공고 수: 이번달 {len(jobs)}건 (전월 대비 {len(jobs) - len(prev_rows):+}건)
- 채용 최다 증가 기업: {top_inc[0]} ({top_inc[1]:+}건)
- 채용 최다 감소 기업: {top_dec[0]} ({top_dec[1]:+}건)

[실시간 수집 뉴스 & 이슈]
- 구글 뉴스: {', '.join(gnews[:5])}
- 게임잡 뉴스: {', '.join(gj_news[:5])}
- 커뮤니티 이슈: {', '.join(dc_news[:5])}

[작성 요구사항]
### 📊 1. 게임 시장 거시 동향 & 직군별 수요 원인 분석
### 📰 2. 주요 기업 변동 원인 & 프로젝트 딥다이브
### 💡 3. 채용 시장 시사점 & 기술 스택 전망

위 3개 섹션 구조로 명확하게 작성해줘. 가독성을 위해 핵심 키워드는 **볼드체**로 강조해줘.
"""

    print("[ai] Claude 3.5 Sonnet API 호출 중...")
    endpoint = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}]
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=120)
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("content", [{}])[0].get("text", "")
            if text:
                report_path = REPORT_DIR / f"{run_date}_ai_analysis.md"
                report_path.write_text(text, encoding="utf-8")
                print(f"[ai] Claude AI 리포트 생성 완료! ({report_path})")
        else:
            print(f"[ERROR] Claude API 실패 ({resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"[ai] Claude API 요청 오류: {e}")

def save_snapshot(jobs, run_date: str) -> Path:
    path = SNAPSHOT_DIR / f"{run_date}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for j in jobs:
            row = {k: j.get(k) for k in FIELDNAMES}
            if isinstance(row.get("job_categories"), list): row["job_categories"] = ";".join(row["job_categories"])
            w.writerow(row)
    files = sorted(p.name for p in SNAPSHOT_DIR.glob("*.csv"))
    (SNAPSHOT_DIR / "index.json").write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.run:
        run_date = datetime.now().strftime("%Y-%m-%d")
        print(f"[run] {run_date} 크롤링 시작...")
        jobs = crawl_all_listings()
        if not jobs: return
        jobs = enrich_with_categories(jobs)
        jobs = backfill_company_logos(jobs)
        save_snapshot(jobs, run_date)
        generate_ai_monthly_report(jobs, run_date)

if __name__ == "__main__":
    main()
