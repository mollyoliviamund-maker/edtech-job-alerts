import os
import re
import json
import time
import smtplib
from datetime import datetime
from email.mime.text import MIMEText  # ✅ correct import

import requests
from bs4 import BeautifulSoup

# -------------------- CONFIG --------------------
# These are read from GitHub Secrets in Actions, but we also set sane defaults for local tests.
YOUR_EMAIL = os.getenv("ALERT_EMAIL", "mollyoliviamund@gmail.com")
YOUR_GMAIL_APP_PASSWORD = os.getenv("jvbi fgwh cwam exuj")  # set in repo Secrets

KEYWORDS = [r"product", r"director", r"manager", r"assessment"]  # case-insensitive
REQUEST_TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 (compatible; EdTechJobChecker/1.4)"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(BASE_DIR, "seen_jobs.json")

# type = greenhouse | lever | workable | workday | workday_page | dayforce | autodetect
SOURCES = [
    # Direct APIs (fast/robust)
    {"company": "Edmentum",               "type": "greenhouse", "board": "edmentum"},
    {"company": "College Board",          "type": "greenhouse", "board": "collegeboard"},
    {"company": "IXL Learning",           "type": "greenhouse", "board": "ixllearning"},
    {"company": "Instructure",            "type": "greenhouse", "board": "instructure"},
    {"company": "Renaissance Learning",   "type": "greenhouse", "board": "renaissancelearning"},
    {"company": "Amplify",                "type": "greenhouse", "board": "amplify"},
    {"company": "Udemy",                  "type": "greenhouse", "board": "udemy"},
    {"company": "Coursera",               "type": "greenhouse", "board": "coursera"},
    {"company": "GoGuardian",             "type": "greenhouse", "board": "goguardian"},
    {"company": "Age of Learning (ABCmouse)", "type": "lever",  "lever_company": "aofl"},
    {"company": "Riverside Insights",     "type": "workable",   "account": "riverside-insights"},

    # Known Workday site
    {"company": "Curriculum Associates",  "type": "workday", "site_url": "https://curriculumassociates.wd5.myworkdayjobs.com/External"},

    # Landing/marketing pages → auto-detect backend and fetch
    {"company": "Houghton Mifflin Harcourt", "type": "workday_page", "page_url": "https://careers.hmhco.com/search"},
    {"company": "McGraw Hill Education",     "type": "workday_page", "page_url": "https://careers.mheducation.com/jobs"},
    {"company": "ACT",                        "type": "workday_page", "page_url": "https://www.act.org/content/act/en/careers-at-act.html"},
    {"company": "Discovery Education",        "type": "dayforce",     "url": "https://jobs.dayforcehcm.com/en-US/discoveryed/CANDIDATEPORTAL"},
    {"company": "Cambium Learning Group",     "type": "workday_page", "page_url": "https://jobs.cambiumlearning.com/?size=n_5_n"},
    {"company": "ClassDojo",                  "type": "autodetect",   "page_url": "https://www.classdojo.com/jobs/#open-roles"},
    {"company": "PowerSchool",                "type": "autodetect",   "page_url": "https://www.powerschool.com/company/careers/#jobs-form-12"},
    {"company": "Schoology",                  "type": "autodetect",   "page_url": "https://www.powerschool.com/company/careers/#jobs-form-12"},
]

# -------------------- HTTP --------------------
session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close",
})

def http_get(url, **kwargs):
    timeout = kwargs.pop("timeout", REQUEST_TIMEOUT)
    for attempt in range(2):
        try:
            return session.get(url, timeout=timeout, **kwargs)
        except requests.RequestException:
            if attempt == 1:
                raise
            time.sleep(1)

def http_post(url, **kwargs):
    timeout = kwargs.pop("timeout", REQUEST_TIMEOUT)
    headers = {"Content-Type": "application/json", "Accept": "application/json", **kwargs.pop("headers", {})}
    for attempt in range(2):
        try:
            return session.post(url, timeout=timeout, headers=headers, **kwargs)
        except requests.RequestException:
            if attempt == 1:
                raise
            time.sleep(1)

# -------------------- UTIL --------------------
def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(sorted(list(seen)), f)
    except Exception as e:
        print(f"[save_seen] {e}")

def title_matches(title: str) -> bool:
    t = (title or "").lower()
    return any(re.search(k, t, re.IGNORECASE) for k in KEYWORDS)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def url_host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else ""

def first(pattern, text):
    m = re.search(pattern, text or "", re.IGNORECASE)
    return m.group(1) if m else None

# -------------------- FETCHERS --------------------
def fetch_greenhouse(board: str, company: str):
    out = []
    try:
        data = http_get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs").json()
        for j in data.get("jobs", []):
            title = j.get("title", "")
            if not title_matches(title):
                continue
            loc = j.get("location", {}).get("name", "") if isinstance(j.get("location"), dict) else j.get("location", "")
            out.append({"company": company, "title": title, "location": loc, "url": j.get("absolute_url") or j.get("url", "")})
    except Exception as e:
        print(f"[greenhouse:{board}] {e}")
    return out

def fetch_lever(slug: str, company: str):
    out = []
    try:
        data = http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json").json()
        for j in data:
            title = j.get("text", "") or j.get("title", "")
            if not title_matches(title):
                continue
            out.append({"company": company, "title": title, "location": (j.get("categories") or {}).get("location", ""), "url": j.get("hostedUrl") or j.get("applyUrl", "")})
    except Exception as e:
        print(f"[lever:{slug}] {e}")
    return out

def fetch_workable(acct: str, company: str):
    out = []
    try:
        data = http_get(f"https://apply.workable.com/api/v3/accounts/{acct}/jobs?state=published").json()
        for j in data.get("results", []):
            title = j.get("title", "")
            if not title_matches(title):
                continue
            out.append({"company": company, "title": title, "location": j.get("location", ""), "url": j.get("url", "")})
    except Exception as e:
        print(f"[workable:{acct}] {e}")
    return out

def workday_cxs(site_url: str):
    # Accepts: https://tenant.wdX.myworkdayjobs.com/External or .../en-US/Careers → returns CXS endpoint
    m = re.match(r"https?://([^/]+)/(.+)$", site_url.strip())
    if not m:
        return None
    host, path = m.group(1), m.group(2).strip("/")
    site = path.split("/")[-1]
    tenant = host.split(".")[0]
    return f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

def fetch_workday(site_url: str, company: str):
    out = []
    cxs = workday_cxs(site_url)
    if not cxs:
        return out
    try:
        offset, limit = 0, 100
        while True:
            data = http_post(cxs, json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}).json()
            posts = data.get("jobPostings", [])
            if not posts:
                break
            for p in posts:
                title = p.get("title", "")
                if not title_matches(title):
                    continue
                ext = p.get("externalPath", "")
                url = f"https://{url_host(cxs)}{ext}" if ext else f"https://{url_host(cxs)}"
                out.append({"company": company, "title": title, "location": p.get("locationsText", "") or p.get("locations", ""), "url": url})
            offset += limit
            if offset >= 500:
                break
    except Exception as e:
        print(f"[workday:{company}] {e}")
    return out

def autodetect_backend(page_url: str):
    try:
        html = http_get(page_url).text
        gh = first(r"https?://(?:boards|job-boards)\.greenhouse\.io/([a-z0-9\-]+)", html)
        if gh: return {"type": "greenhouse", "board": gh}
        lv = first(r"https?://jobs\.lever\.co/([A-Za-z0-9\-]+)", html)
        if lv: return {"type": "lever", "lever_company": lv}
        wd = first(r"(https?://[a-z0-9\-]+\.wd[0-9]+\.myworkdayjobs\.com/[A-Za-z0-9\-_\/]+)", html)
        if wd: return {"type": "workday", "site_url": wd}
        df = first(r"(https?://jobs\.dayforcehcm\.com/[^\s\"'>]+)", html)
        if df: return {"type": "dayforce", "url": df}
    except Exception as e:
        print(f"[autodetect:{page_url}] {e}")
    return None

def fetch_dayforce(portal_url: str, company: str):
    out = []
    try:
        soup = BeautifulSoup(http_get(portal_url).text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/Posting/" not in href:
                continue
            title = norm(a.get_text())
            if not title or not title_matches(title):
                continue
            url = href if href.startswith("http") else f"https://{url_host(portal_url)}{href}"
            out.append({"company": company, "title": title, "location": "", "url": url})
    except Exception as e:
        print(f"[dayforce:{company}] {e}")
    return out

def fetch_from_page(page_url: str, company: str):
    det = autodetect_backend(page_url)
    if det:
        if det["type"] == "greenhouse": return fetch_greenhouse(det["board"], company)
        if det["type"] == "lever":      return fetch_lever(det["lever_company"], company)
        if det["type"] == "workday":    return fetch_workday(det["site_url"], company)
        if det["type"] == "dayforce":   return fetch_dayforce(det["url"], company)

    # Fallback: scan the page for anchor text that LOOKS like a job title
    out = []
    try:
        soup = BeautifulSoup(http_get(page_url).text, "html.parser")
        for a in soup.find_all("a", href=True):
            title = norm(a.get_text())
            if not title or not title_matches(title):
                continue
            href = a["href"]
            url = href if href.startswith("http") else f"https://{url_host(page_url)}{href}"
            out.append({"company": company, "title": title, "location": "", "url": url})
    except Exception as e:
        print(f"[fallback:{company}] {e}")
    return out

# -------------------- MASTER FETCH --------------------
def fetch_all_jobs():
    all_jobs = []
    for s in SOURCES:
        t, c = s["type"], s["company"]
        try:
            if t == "greenhouse":      all_jobs += fetch_greenhouse(s["board"], c)
            elif t == "lever":         all_jobs += fetch_lever(s["lever_company"], c)
            elif t == "workable":      all_jobs += fetch_workable(s["account"], c)
            elif t == "workday":       all_jobs += fetch_workday(s["site_url"], c)
            elif t == "workday_page":  all_jobs += fetch_from_page(s["page_url"], c)
            elif t == "dayforce":      all_jobs += fetch_dayforce(s["url"], c)
            elif t == "autodetect":    all_jobs += fetch_from_page(s["page_url"], c)
        except Exception as e:
            print(f"[{t}:{c}] {e}")
        time.sleep(0.5)  # be polite

    # Normalize + dedupe by URL
    clean, seen_urls = [], set()
    for j in all_jobs:
        url = (j.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        j["title"] = norm(j.get("title", ""))
        j["company"] = norm(j.get("company", ""))
        j["location"] = norm(j.get("location", ""))
        clean.append(j)
    return clean

# -------------------- EMAIL --------------------
def send_email(new_jobs):
    # If the secret isn't set, don't crash the workflow—just log and skip email.
    if not YOUR_GMAIL_APP_PASSWORD:
        print("No GMAIL_APP_PASSWORD provided; skipping email.")
        return
    if not new_jobs:
        return

    lines = []
    for j in new_jobs:
        line = f"{j['company']}: {j['title']}"
        if j['location']:
            line += f" ({j['location']})"
        line += f"\n{j['url']}"
        lines.append(line)

    msg = MIMEText("\n\n".join(lines))
    msg["Subject"] = f"New EdTech Jobs — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg["From"] = YOUR_EMAIL
    msg["To"] = YOUR_EMAIL

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(YOUR_EMAIL, YOUR_GMAIL_APP_PASSWORD)
        server.send_message(msg)

# -------------------- MAIN --------------------
def main():
    seen = load_seen()
    jobs = fetch_all_jobs()
    new_jobs = [j for j in jobs if j["url"] not in seen]

    if new_jobs:
        send_email(new_jobs)
        seen.update(j["url"] for j in new_jobs)
        save_seen(seen)
        print(f"Sent {len(new_jobs)} new jobs.")
    else:
        print("No new jobs found.")

if __name__ == "__main__":
    main()
