import os
import re
import json
import time
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from urllib.parse import urlparse, urlunparse, parse_qs

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---------------- CONFIG ----------------
YOUR_EMAIL = os.environ["EMAIL_ADDRESS"]
YOUR_GMAIL_APP_PASSWORD = os.environ["EMAIL_PASSWORD"]
assert len(YOUR_GMAIL_APP_PASSWORD) == 16, f"App password length is {len(YOUR_GMAIL_APP_PASSWORD)}; expected 16."

KEYWORDS = [
    r"product\b", r"product\s*owner", r"product\s*operations?",
    r"director\b", r"manager\b",
    r"platform\b", r"assessment", r"testing", r"measurement",
    r"assessment\s*platform"
]

REQUEST_TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 (JobAlerts/2.0)"
SEEN_FILE = "seen_jobs.json"

SOURCES = [
    {"company": "Edmentum",               "type": "greenhouse",   "board": "edmentum"},
    {"company": "College Board",          "type": "greenhouse",   "board": "collegeboard"},
    {"company": "IXL Learning",           "type": "greenhouse",   "board": "ixllearning"},
    {"company": "Instructure",            "type": "greenhouse",   "board": "instructure"},
    {"company": "Renaissance Learning",   "type": "greenhouse",   "board": "renaissancelearning"},
    {"company": "Amplify",                "type": "greenhouse",   "board": "amplify"},
    {"company": "Udemy",                  "type": "greenhouse",   "board": "udemy"},
    {"company": "Coursera",               "type": "greenhouse",   "board": "coursera"},
    {"company": "GoGuardian",             "type": "greenhouse",   "board": "goguardian"},
    {"company": "Age of Learning (ABCmouse)", "type": "lever",    "lever_company": "aofl"},
    # Playwright sites
    {"company": "Riverside Insights",     "type": "playwright",   "url": "https://apply.workable.com/riverside-insights/"},
    {"company": "Houghton Mifflin Harcourt", "type": "playwright","url": "https://careers.hmhco.com/search"},
    {"company": "Discovery Education",    "type": "playwright",   "url": "https://jobs.dayforcehcm.com/en-US/discoveryed/CANDIDATEPORTAL"},
    {"company": "McGraw Hill Education",  "type": "playwright",   "url": "https://careers.mheducation.com/jobs"},
    {"company": "Cambium Learning Group", "type": "playwright",   "url": "https://jobs.cambiumlearning.com/?size=n_5_n"},
    {"company": "Curriculum Associates",  "type": "playwright",   "url": "https://curriculumassociates.wd5.myworkdayjobs.com/External"},
    # Other
    {"company": "ACT",                        "type": "autodetect", "page_url": "https://www.act.org/content/act/en/careers-at-act.html"},
    {"company": "ClassDojo",                  "type": "autodetect", "page_url": "https://www.classdojo.com/jobs/#open-roles"},
    {"company": "PowerSchool",                "type": "autodetect", "page_url": "https://www.powerschool.com/company/careers/#jobs-form-12"},
    {"company": "Schoology",                  "type": "autodetect", "page_url": "https://www.powerschool.com/company/careers/#jobs-form-12"},
]

# ---------------- HTTP ----------------
session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close",
})

def http_get(url, **kwargs):
    timeout = kwargs.pop("timeout", REQUEST_TIMEOUT)
    for _ in range(2):
        try:
            return session.get(url, timeout=timeout, **kwargs)
        except requests.RequestException:
            time.sleep(1)
    return session.get(url, timeout=timeout, **kwargs)

# ---------------- UTIL ----------------
def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(list(seen)), f)

def title_matches(title):
    t = (title or "").lower()
    return any(re.search(k, t, re.IGNORECASE) for k in KEYWORDS)

def norm(s): return re.sub(r"\s+", " ", (s or "").strip())

def url_host(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower().replace("www.", "") if m else ""

# --- Canonical URL dedupe
def normalize_url(u: str) -> str:
    if not u: return ""
    try:
        p = urlparse(u)
        host = p.netloc.lower().replace("www.", "")
        path = p.path
        query = parse_qs(p.query)

        if "greenhouse.io" in host:
            m = re.search(r"/jobs/(\d+)", path)
            if m: return f"https://{host}{path.split('/jobs/')[0]}/jobs/{m.group(1)}"
        if "lever.co" in host:
            parts = [seg for seg in path.split("/") if seg]
            if len(parts) >= 2: return f"https://{host}/{parts[-2]}/{parts[-1]}"
        if "myworkdayjobs.com" in host:
            m = re.search(r"(?:JR|R|REQ)[-_]?(\d+)", u, re.I)
            if m: return f"https://{host}/id/{m.group(1)}"
        if "dayforcehcm.com" in host:
            m = re.search(r"/Posting/View/(\d+)", path, re.I)
            if m: return f"https://{host}/Posting/View/{m.group(1)}"
        if "workable.com" in host:
            m = re.search(r"/j/([A-Za-z0-9]+)", path)
            if m: return f"https://apply.workable.com/j/{m.group(1)}"

        return urlunparse((p.scheme, host, path.rstrip("/"), "", "", ""))
    except Exception:
        return u.strip().lower()

# ---------------- FETCHERS ----------------
def fetch_greenhouse(board, company):
    out = []
    try:
        data = http_get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs").json()
        for j in data.get("jobs", []):
            title = j.get("title", "")
            if not title_matches(title): continue
            out.append({"company": company, "title": title, "location": j.get("location", {}).get("name", ""), "url": j.get("absolute_url")})
    except Exception as e:
        print(f"[greenhouse:{board}] {e}")
    return out

def fetch_lever(slug, company):
    out = []
    try:
        data = http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json").json()
        for j in data:
            title = j.get("text", "")
            if not title_matches(title): continue
            out.append({"company": company, "title": title, "location": (j.get("categories") or {}).get("location", ""), "url": j.get("hostedUrl")})
    except Exception as e:
        print(f"[lever:{slug}] {e}")
    return out

def fetch_from_page(page_url, company):
    out = []
    try:
        soup = BeautifulSoup(http_get(page_url).text, "html.parser")
        for a in soup.find_all("a", href=True):
            title = norm(a.get_text())
            if not title or not title_matches(title): continue
            href = a["href"]
            url = href if href.startswith("http") else f"https://{url_host(page_url)}{href}"
            out.append({"company": company, "title": title, "location": "", "url": url})
    except Exception as e:
        print(f"[html:{company}] {e}")
    return out

def fetch_playwright(url, company):
    out = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=60000)
            page.wait_for_timeout(5000)
            html = page.content()
            browser.close()
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            title = norm(a.get_text())
            if not title or not title_matches(title): continue
            href = a["href"]
            full = href if href.startswith("http") else f"https://{url_host(url)}{href}"
            out.append({"company": company, "title": title, "location": "", "url": full})
    except Exception as e:
        print(f"[playwright:{company}] {e}")
    return out

# ---------------- MASTER FETCH ----------------
def fetch_all_jobs():
    all_jobs = []
    for s in SOURCES:
        t, c = s["type"], s["company"]
        try:
            if t == "greenhouse": all_jobs += fetch_greenhouse(s["board"], c)
            elif t == "lever":    all_jobs += fetch_lever(s["lever_company"], c)
            elif t in ("autodetect", "workday_page"): all_jobs += fetch_from_page(s["page_url"], c)
            elif t == "playwright": all_jobs += fetch_playwright(s["url"], c)
        except Exception as e:
            print(f"[{t}:{c}] {e}")
        time.sleep(0.4)

    # Dedupe using canonicalized URLs
    clean, seen_urls = [], set()
    for j in all_jobs:
        u = normalize_url(j.get("url"))
        if not u or u in seen_urls: continue
        seen_urls.add(u)
        j["url"] = u
        j["title"] = norm(j.get("title", ""))
        j["company"] = norm(j.get("company", ""))
        j["location"] = norm(j.get("location", ""))
        clean.append(j)

    print("Matches by company:", ", ".join(f"{k}:{v}" for k,v in sorted({j['company']:0 for j in clean}.items())))
    return clean

# ---------------- EMAIL ----------------
def send_email(jobs):
    if not jobs: return
    lines = []
    for j in jobs:
        line = f"{j['company']}: {j['title']}"
        if j['location']: line += f" ({j['location']})"
        line += f"\n{j['url']}"
        lines.append(line)
    msg = MIMEText("\n\n".join(lines))
    msg["Subject"] = f"New EdTech Jobs — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg["From"] = YOUR_EMAIL
    msg["To"] = YOUR_EMAIL
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(YOUR_EMAIL, YOUR_GMAIL_APP_PASSWORD)
        s.send_message(msg)

# ---------------- MAIN ----------------
def main():
    seen = load_seen()
    jobs = fetch_all_jobs()
    first_run = not os.path.exists(SEEN_FILE)
    new_jobs = jobs if first_run else [j for j in jobs if j["url"] not in seen]

    if new_jobs:
        send_email(new_jobs)
        seen.update(j["url"] for j in new_jobs)
        save_seen(seen)
        print(f"Sent {len(new_jobs)} jobs.")
    else:
        print("No new jobs found.")

if __name__ == "__main__":
    main()
