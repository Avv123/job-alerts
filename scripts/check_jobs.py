#!/usr/bin/env python3
"""Check company career pages for new openings matching keywords, email on new matches.

Phase 1 additions: per-company source health tracking (catches silent scraper
breakage), first_seen_at/last_seen_at tracking per job, location capture where
the source API provides it, a seniority exclude-filter, and a deterministic
relevance score used to tier ("Strong fit" / "Good fit" / "Stretch") the email.
"""
import html
import json
import os
import re
import sys
import time
import hashlib
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
COMPANIES_FILE = ROOT / "companies.json"
KEYWORDS_FILE = ROOT / "keywords.json"
STATE_DIR = ROOT / "state"
HEALTH_FILE = STATE_DIR / "_health.json"

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ALERT_TO = [e.strip() for e in (os.environ.get("ALERT_TO") or "").split(",") if e.strip()]
ALERT_FROM = os.environ.get("ALERT_FROM") or "onboarding@resend.dev"

session = requests.Session()
session.headers.update({"User-Agent": "job-alerts-bot/1.0"})


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2))


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def matches_keywords(title, keywords):
    t = title.lower()
    return any(k in t for k in keywords)


# --- Relevance scoring & filtering, tuned for a ~1.5yr Go/backend engineer ---

RELEVANCE_WEIGHTS = {
    "golang": 5, "go developer": 5, "go engineer": 5,
    "backend": 5, "back-end": 5, "back end": 5,
    "distributed systems": 4,
    "software development engineer": 4, "software developer engineer": 4,
    "software engineer": 4, "software developer": 4, "sde": 4,
    "kafka": 3, "redis": 3, "java": 3, "spring boot": 3, "springboot": 3, "spring": 2,
    "mongodb": 2, "postgresql": 2, "postgres": 2, "aws": 2, "python": 2, "clickhouse": 2,
    "docker": 1, "kubernetes": 1, "node.js": 1, "nodejs": 1, "react": 1,
    "senior": -2, "sr.": -2, "lead": -3,
    "intern": -6, "internship": -6,
}

SENIORITY_EXCLUDE_WORDS = ("staff", "principal", "director", "architect", "manager")
SENIORITY_EXCLUDE_PHRASES = ("vice president", "head of engineering", "technical leader")

# Catches "8+ years", "8+yrs", "10+ Years", "12-15 years", "12 to 16 yrs",
# en/em-dash ranges ("6 – 8 years"), and label-first phrasing
# ("Years of Experience: 6 - 8", "Experience: 5+ years").
YEARS_PLUS_RE = re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)\b", re.I)
YEARS_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I)
YEARS_LABEL_RE = re.compile(
    r"(?:years?\s+of\s+experience|experience)\s*:?\s*(\d{1,2})\s*(?:(?:-|–|—|to)\s*(\d{1,2}))?\s*\+?",
    re.I,
)
# The plain, most common phrasing: "5 years of experience", "5 years experience"
YEARS_PLAIN_RE = re.compile(r"(\d{1,2})\s*\+?\s*years?\s+(?:of\s+)?experience\b", re.I)

MAX_YEARS_EXPERIENCE = 3  # hard-exclude anything explicitly requiring more than this


def extract_min_years(text):
    """Minimum years-of-experience mentioned in text, or None if not found.
    Checked against title + (when available) the full job description —
    titles alone often hide this behind internal level codes like 'E5'/'L5'."""
    for pattern in (YEARS_PLUS_RE, YEARS_RANGE_RE, YEARS_LABEL_RE, YEARS_PLAIN_RE):
        m = pattern.search(text)
        if m:
            return int(m.group(1))
    return None


def exceeds_experience_cap(text):
    years = extract_min_years(text)
    return years is not None and years > MAX_YEARS_EXPERIENCE


def score_job(title):
    t = title.lower()
    return sum(w for kw, w in RELEVANCE_WEIGHTS.items() if kw in t)


def tier_for_score(score):
    if score >= 9:
        return "\U0001F525", "Strong fit"
    if score >= 4:
        return "\U0001F7E2", "Good fit"
    return "\U0001F7E1", "Stretch"


def is_excluded_seniority(title):
    t = title.lower()
    if any(p in t for p in SENIORITY_EXCLUDE_PHRASES):
        return True
    return any(re.search(rf"\b{re.escape(word)}\b", t) for word in SENIORITY_EXCLUDE_WORDS)


INDIA_CITIES = (
    "bengaluru", "bangalore", "gurugram", "gurgaon", "noida", "hyderabad",
    "delhi", "ncr", "mumbai", "pune", "chennai", "kolkata", "ahmedabad",
    "kochi", "jaipur", "indore", "chandigarh", "india",
)
# Deliberately broad — a blocklist of specific countries always has gaps
# (e.g. "Sweden (Remote)" slipped through with a short list), so this covers
# the realistic set of countries/regions that show up on global job boards.
# Ambiguous terms that legitimately include India (e.g. "APAC") are excluded
# from this list on purpose, since those should NOT be treated as non-India.
NON_INDIA_REMOTE_MARKERS = (
    "us", "usa", "u.s.", "united states", "uk", "u.k.", "united kingdom", "england",
    "scotland", "wales", "canada", "mexico", "brazil", "argentina", "chile", "colombia", "peru",
    "ireland", "germany", "france", "spain", "italy", "portugal", "netherlands", "belgium",
    "switzerland", "austria", "sweden", "norway", "denmark", "finland", "iceland",
    "poland", "czech", "romania", "hungary", "greece", "ukraine", "russia", "latam",
    "israel", "uae", "united arab emirates", "saudi arabia", "qatar", "turkey", "emea",
    "china", "japan", "korea", "taiwan", "singapore", "hong kong", "philippines",
    "indonesia", "vietnam", "thailand", "malaysia", "australia", "new zealand",
    "south africa", "nigeria", "kenya", "egypt", "europe", "americas",
)


def is_india_or_remote(location):
    """True/False if we can tell from the location string, None if unknown
    (e.g. custom-scraped sources with no structured location data) — unknown
    locations are NOT excluded, since we can't confirm either way."""
    if not location:
        return None
    loc = location.lower()
    if any(city in loc for city in INDIA_CITIES):
        return True
    if "remote" in loc:
        if any(re.search(rf"\b{re.escape(m)}\b", loc) for m in NON_INDIA_REMOTE_MARKERS):
            return False
        return True
    return False


# --- Per-company state (first_seen_at / last_seen_at / active) ---

def load_state(path):
    if not path.exists():
        return {"jobs": {}}
    data = json.loads(path.read_text())
    if isinstance(data, list):
        # migrate old flat-id-list format
        now = datetime.now(timezone.utc).isoformat()
        return {
            "jobs": {
                jid: {
                    "first_seen_at": now, "last_seen_at": now,
                    "title": "", "url": "", "location": "",
                    "active": True, "excluded": False,
                }
                for jid in data
            }
        }
    return data


# --- Fetch with retry/backoff, per-company isolation ---

def fetch_with_retry(fetcher, company, retries=2, backoff_base=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            return fetcher(company), None
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff_base * (attempt + 1))
    return None, last_err


def fetch_greenhouse(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    return [
        {
            "id": str(j["id"]),
            "title": j["title"],
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
        }
        for j in jobs
    ]


def fetch_lever(token):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    jobs = r.json()
    return [
        {
            "id": j["id"],
            "title": j["text"],
            "url": j.get("hostedUrl", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            # Lever's list response already includes full description text —
            # no extra request needed to check the real experience requirement.
            "_description": " ".join(filter(None, [
                j.get("descriptionPlain", ""), j.get("openingPlain", ""), j.get("additionalPlain", ""),
            ])),
        }
        for j in jobs
    ]


def fetch_smartrecruiters(token):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    jobs = r.json().get("content", [])
    result = []
    for j in jobs:
        loc = j.get("location") or {}
        location = ", ".join(filter(None, [loc.get("city"), loc.get("region")])) or loc.get("country", "")
        result.append({
            "id": j["id"],
            "title": j["name"],
            "url": f"https://jobs.smartrecruiters.com/{token}/{j['id']}",
            "location": location,
        })
    return result


def fetch_ashby(token):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    return [
        {
            "id": j.get("id", j.get("title")),
            "title": j.get("title", ""),
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "location": j.get("location", "") if isinstance(j.get("location"), str) else "",
            # Ashby's list response already includes the full description.
            "_description": j.get("descriptionPlain", ""),
        }
        for j in jobs
    ]


def fetch_workday(url):
    """url is the site's external career-site URL, e.g. https://tenant.wd5.myworkdayjobs.com/Site_Name"""
    parsed = urlparse(url)
    host = parsed.netloc
    tenant = host.split(".")[0]
    parts = [p for p in parsed.path.split("/") if p]
    site = parts[0] if parts else ""
    cxs_url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

    jobs = []
    offset = 0
    limit = 20
    while True:
        body = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
        r = session.post(cxs_url, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for p in postings:
            ext = p.get("externalPath", "")
            jobs.append({
                "id": ext or p.get("title", ""),
                "title": p.get("title", ""),
                "url": f"https://{host}/{site}{ext}",
                "location": p.get("locationsText", ""),
            })
        offset += limit
        if offset >= data.get("total", 0) or offset > 500:
            break
    return jobs


def strip_html(text):
    text = html.unescape(text or "")
    return re.sub(r"<[^>]+>", " ", text)


def fetch_greenhouse_description(token, job_id):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?content=true"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return strip_html(r.json().get("content", ""))


def fetch_smartrecruiters_description(token, job_id):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    sections = (r.json().get("jobAd") or {}).get("sections") or {}
    parts = [strip_html(s.get("text", "")) for s in sections.values() if isinstance(s, dict)]
    return " ".join(parts)


def fetch_workday_description(company_url, external_path):
    parsed = urlparse(company_url)
    host = parsed.netloc
    tenant = host.split(".")[0]
    parts = [p for p in parsed.path.split("/") if p]
    site = parts[0] if parts else ""
    detail_url = f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
    r = session.get(detail_url, timeout=30)
    r.raise_for_status()
    return strip_html((r.json().get("jobPostingInfo") or {}).get("jobDescription", ""))


def get_description(ctype, company, job):
    """Best-effort full job description for a newly-discovered job, used to
    check the real experience requirement (titles often hide this behind
    internal level codes like 'E5'). Returns '' on any failure — description
    fetch is a nice-to-have, never blocks the alert on its own."""
    try:
        if ctype in ("lever", "ashby"):
            return job.get("_description", "")
        if ctype == "greenhouse":
            return fetch_greenhouse_description(company["token"], job["id"])
        if ctype == "smartrecruiters":
            return fetch_smartrecruiters_description(company["token"], job["id"])
        if ctype == "workday":
            return fetch_workday_description(company["url"], job["id"])
        if ctype == "custom" and job.get("url") and job["url"] != company.get("url"):
            return fetch_custom_description(job["url"])
    except Exception as e:
        print(f"[{job.get('title','?')}] description fetch failed: {e}", file=sys.stderr)
    return ""


CUSTOM_NOISE_PREFIXES = (
    "share ", "apply ", "apply for ", "apply now ", "save ",
    "read more about the job ", "learn more about ", "more info about ",
    "position, ",
)
JOB_ID_SUFFIX_RE = re.compile(r"\s*,?\s*job\s*id\s*(?:is)?\s*[:\-]?\s*[a-f0-9-]{6,}\s*$", re.I)

# Page chrome / blog content that happens to contain a matching keyword but
# isn't an actual job listing — e.g. "125 jobs found for software engineer"
# (a search-results count) or "10 Essential Software Engineer Skills..." (a
# blog post link on the careers page).
NOISE_LINE_RE = re.compile(
    r"\b\d+\s+jobs?\s+found\b"
    r"|\bno\s+jobs?\s+found\b"
    r"|\bsearch\s+results\s+for\b"
    r"|\bshowing\s+.*\bresults\b"
    r"|\bresults\s+found\s+for\b"
    r"|\b\d+\s+(essential|useful|key|important|top)\s+\S+\s+(skills|tips|things|reasons)\b"
    r"|\bskills\s+to\s+succeed\b"
    r"|\bguide\s+to\b"
    r"|\btips\s+for\b"
    r"|\bhow\s+to\s+become\b"
    r"|\bcareer\s+path\b",
    re.I,
)

_browser = None
_playwright_ctx = None


def get_browser():
    global _browser, _playwright_ctx
    if _browser is None:
        _playwright_ctx = sync_playwright().start()
        _browser = _playwright_ctx.chromium.launch()
    return _browser


def close_browser():
    global _browser, _playwright_ctx
    if _browser is not None:
        _browser.close()
        _playwright_ctx.stop()
        _browser = None
        _playwright_ctx = None


def normalize_title(raw):
    norm = raw.strip()
    for prefix in CUSTOM_NOISE_PREFIXES:
        if norm.lower().startswith(prefix):
            norm = norm[len(prefix):].strip()
            break
    return JOB_ID_SUFFIX_RE.sub("", norm).strip()


def lines_to_jobs(text, url, link_map=None):
    link_map = link_map or {}
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if 4 < len(l) < 120]
    jobs = []
    seen = set()
    for l in lines:
        norm = normalize_title(l)
        key = norm.lower()
        if not key:
            continue
        if NOISE_LINE_RE.search(key):
            continue
        if key in seen:
            continue
        seen.add(key)
        jid = hashlib.sha1(key.encode()).hexdigest()[:12]
        jobs.append({"id": jid, "title": norm, "url": link_map.get(key, url), "location": ""})
    return jobs


SHOW_MORE_PATTERN = re.compile(r"show more|load more|view more|see more", re.I)


def expand_pagination(page, max_clicks=6):
    """Click 'Show More'-style buttons and scroll down repeatedly so
    infinite-scroll / paginated listings are fully loaded before we scrape."""
    for _ in range(max_clicks):
        clicked = False
        for el in page.locator("button, a").all():
            try:
                text = el.inner_text(timeout=1000)
            except Exception:
                continue
            if text and SHOW_MORE_PATTERN.search(text) and el.is_visible():
                try:
                    el.click(timeout=2000)
                    clicked = True
                    page.wait_for_timeout(1000)
                    break
                except Exception:
                    continue
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(500)
        if not clicked:
            break


def fetch_custom(url):
    """Best-effort: render the page with a real browser (so JS-built job lists
    actually appear), expand pagination, and pull visible text plus tooltip
    'title' attributes that look like job titles (sites often truncate the
    visible text but keep the full string in a title/aria-label attribute).
    Also captures each link's href so a specific job's own page (rather than
    the generic listings page) can be visited later for its full description."""
    browser = get_browser()
    page = browser.new_page(user_agent="Mozilla/5.0 (job-alerts-bot/1.0)")
    try:
        page.goto(url, timeout=45000, wait_until="networkidle")
    except Exception:
        pass  # partial content is still better than nothing
    try:
        expand_pagination(page)
    except Exception:
        pass
    text = page.inner_text("body")
    try:
        attr_texts = page.eval_on_selector_all(
            "[title], [aria-label]",
            "els => els.map(e => e.getAttribute('title') || e.getAttribute('aria-label')).filter(Boolean)",
        )
    except Exception:
        attr_texts = []
    link_map = {}
    try:
        anchors = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({text: (e.getAttribute('title') || e.innerText || '').trim(), href: e.href}))",
        )
        for a in anchors:
            # anchors often wrap a whole card (title + location + dept, newline-
            # separated) rather than just the title, so key on the first line only
            first_line = a["text"].split("\n")[0].strip() if a["text"] else ""
            if first_line and a["href"]:
                link_map[normalize_title(first_line).lower()] = a["href"]
    except Exception:
        pass
    page.close()
    full_text = text + "\n" + "\n".join(attr_texts)
    return lines_to_jobs(full_text, url, link_map)


def fetch_custom_description(job_url):
    browser = get_browser()
    page = browser.new_page(user_agent="Mozilla/5.0 (job-alerts-bot/1.0)")
    try:
        try:
            page.goto(job_url, timeout=30000, wait_until="domcontentloaded")
        except Exception:
            pass  # partial content is still better than nothing
        text = page.inner_text("body")
    finally:
        page.close()
    return text


FETCHERS = {
    "greenhouse": lambda c: fetch_greenhouse(c["token"]),
    "lever": lambda c: fetch_lever(c["token"]),
    "smartrecruiters": lambda c: fetch_smartrecruiters(c["token"]),
    "ashby": lambda c: fetch_ashby(c["token"]),
    "workday": lambda c: fetch_workday(c["url"]),
    "custom": lambda c: fetch_custom(c["url"]),
}


def format_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d, %H:%M UTC")
    except Exception:
        return ""


def build_email_html(all_new):
    by_company = OrderedDict()
    for j in sorted(all_new, key=lambda x: -x["score"]):
        by_company.setdefault(j["company"], []).append(j)
    # companies with the strongest single match float to the top
    companies_sorted = sorted(by_company.items(), key=lambda kv: -max(j["score"] for j in kv[1]))

    company_blocks = []
    for company, jobs in companies_sorted:
        job_cards = "".join(
            f'''<a href="{html.escape(j['url'], quote=True)}"
                  style="display:block;padding:12px 14px;margin-bottom:8px;background:#f9fafb;
                         border:1px solid #eef0f3;border-radius:8px;text-decoration:none;">
                <div style="color:#1d4ed8;font-size:14px;font-weight:600;line-height:1.4;">
                  {j['tier_emoji']} {html.escape(j['title'])}
                </div>
                <div style="color:#6b7280;font-size:12px;margin-top:4px;">
                  {html.escape(j['tier_label'])}{' &middot; ' + html.escape(j['location']) if j['location'] else ''}
                  &middot; first seen {format_time(j['first_seen_at'])}
                </div>
              </a>'''
            for j in jobs
        )
        company_blocks.append(f'''
          <div style="margin-bottom:22px;">
            <div style="font-size:15px;font-weight:700;color:#111827;
                        border-bottom:1px solid #e5e7eb;padding-bottom:6px;margin-bottom:10px;">
              {html.escape(company)}
              <span style="color:#6b7280;font-weight:400;">({len(jobs)})</span>
            </div>
            {job_cards}
          </div>''')

    n_companies = len(by_company)
    company_word = "company" if n_companies == 1 else "companies"
    opening_word = "opening" if len(all_new) == 1 else "openings"
    checked_at = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")

    return f'''<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="background:#f4f5f7;padding:32px 16px;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
      <tr><td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0"
          style="background:#ffffff;border-radius:12px;overflow:hidden;max-width:600px;width:100%;">
          <tr>
            <td style="background:#111827;padding:24px 32px;">
              <div style="color:#ffffff;font-size:20px;font-weight:700;">Job Alerts</div>
              <div style="color:#9ca3af;font-size:13px;margin-top:4px;">
                {len(all_new)} new {opening_word} across {n_companies} {company_word}
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 32px;">
              {''.join(company_blocks)}
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px;background:#f9fafb;color:#9ca3af;font-size:12px;text-align:center;">
              Checked {checked_at} &middot; runs every 6 hours
            </td>
          </tr>
        </table>
      </td></tr>
    </table>'''


def send_email(subject, html_body):
    if not RESEND_API_KEY or not ALERT_TO:
        print("RESEND_API_KEY or ALERT_TO not set, skipping email. Body:\n", html_body)
        return
    r = session.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": ALERT_FROM, "to": ALERT_TO, "subject": subject, "html": html_body},
        timeout=30,
    )
    if r.status_code >= 300:
        print(f"Resend error {r.status_code}: {r.text}", file=sys.stderr)
    else:
        print("Email sent.")


def main():
    companies = load_json(COMPANIES_FILE, [])
    keywords = [k.lower() for k in load_json(KEYWORDS_FILE, [])]
    STATE_DIR.mkdir(exist_ok=True)
    health = load_json(HEALTH_FILE, {})
    now = datetime.now(timezone.utc).isoformat()

    all_new = []

    for company in companies:
        name = company["name"]
        slug = company.get("slug") or slugify(name)
        ctype = company.get("type", "custom")
        fetcher = FETCHERS.get(ctype)
        if not fetcher:
            print(f"[{name}] unknown type {ctype}, skipping")
            continue

        state_file = STATE_DIR / f"{slug}.json"
        state = load_state(state_file)
        prev_health = health.get(name, {})

        jobs, error = fetch_with_retry(fetcher, company)

        company_health = {
            "source_type": ctype,
            "last_check_time": now,
            "last_successful_check": prev_health.get("last_successful_check"),
            "status": "healthy",
            "jobs_found": prev_health.get("jobs_found", 0),
            "error": None,
        }

        if error is not None:
            company_health["status"] = "broken"
            company_health["error"] = str(error)
            health[name] = company_health
            print(f"[{name}] fetch failed: {error}", file=sys.stderr)
            continue

        raw_count = len(jobs)
        prev_count = prev_health.get("jobs_found", 0)
        status = "healthy"
        if raw_count == 0 and prev_count >= 3:
            status = "suspicious"
        elif prev_count >= 5 and raw_count < prev_count * 0.3:
            status = "suspicious"

        company_health.update({
            "last_successful_check": now,
            "status": status,
            "jobs_found": raw_count,
            "error": None,
        })
        health[name] = company_health

        matched = [j for j in jobs if matches_keywords(j["title"], keywords)]
        matched_ids = set()
        new_count = 0

        for j in matched:
            jid = j["id"]
            matched_ids.add(jid)
            existing = state["jobs"].get(jid)

            if existing:
                existing["last_seen_at"] = now
                existing["title"] = j["title"]
                existing["url"] = j["url"]
                existing["location"] = j.get("location", "")
                existing["active"] = True
                continue

            location = j.get("location", "")
            excluded = is_excluded_seniority(j["title"]) or is_india_or_remote(location) is False
            if not excluded:
                # only worth the extra fetch if the job would otherwise be alerted on
                description = get_description(ctype, company, j)
                excluded = exceeds_experience_cap(f"{j['title']} {description}".lower())
            state["jobs"][jid] = {
                "first_seen_at": now,
                "last_seen_at": now,
                "title": j["title"],
                "url": j["url"],
                "location": j.get("location", ""),
                "active": True,
                "excluded": excluded,
            }
            if not excluded:
                score = score_job(j["title"])
                tier_emoji, tier_label = tier_for_score(score)
                new_count += 1
                all_new.append({
                    "company": name,
                    "title": j["title"],
                    "url": j["url"],
                    "location": j.get("location", ""),
                    "first_seen_at": now,
                    "score": score,
                    "tier_emoji": tier_emoji,
                    "tier_label": tier_label,
                })

        for jid, entry in state["jobs"].items():
            entry["active"] = jid in matched_ids

        save_json(state_file, state)

        if new_count:
            print(f"[{name}] {new_count} new matching opening(s)")

    close_browser()
    save_json(HEALTH_FILE, health)

    if all_new:
        send_email(f"Job Alerts: {len(all_new)} new opening(s)", build_email_html(all_new))
    else:
        print("No new matching openings this run.")


if __name__ == "__main__":
    main()
