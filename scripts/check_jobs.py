#!/usr/bin/env python3
"""Check company career pages for new openings matching keywords, email on new matches."""
import json
import os
import re
import sys
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
COMPANIES_FILE = ROOT / "companies.json"
KEYWORDS_FILE = ROOT / "keywords.json"
STATE_DIR = ROOT / "state"

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ALERT_TO = os.environ.get("ALERT_TO")
ALERT_FROM = os.environ.get("ALERT_FROM", "onboarding@resend.dev")

session = requests.Session()
session.headers.update({"User-Agent": "job-alerts-bot/1.0"})


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def matches_keywords(title, keywords):
    t = title.lower()
    return any(k in t for k in keywords)


def fetch_greenhouse(token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    return [{"id": str(j["id"]), "title": j["title"], "url": j.get("absolute_url", "")} for j in jobs]


def fetch_lever(token):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    jobs = r.json()
    return [{"id": j["id"], "title": j["text"], "url": j.get("hostedUrl", "")} for j in jobs]


def fetch_smartrecruiters(token):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    jobs = r.json().get("content", [])
    return [
        {"id": j["id"], "title": j["name"], "url": f"https://jobs.smartrecruiters.com/{token}/{j['id']}"}
        for j in jobs
    ]


def fetch_ashby(token):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    return [
        {"id": j.get("id", j.get("title")), "title": j.get("title", ""), "url": j.get("jobUrl") or j.get("applyUrl") or ""}
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
            jobs.append({"id": ext or p.get("title", ""), "title": p.get("title", ""), "url": f"https://{host}/{site}{ext}"})
        offset += limit
        if offset >= data.get("total", 0) or offset > 500:
            break
    return jobs


CUSTOM_NOISE_PREFIXES = ("share ", "apply ", "apply for ", "apply now ")


def fetch_custom(url):
    """Best-effort: fetch a page and pull visible lines that look like job titles."""
    r = session.get(url, timeout=30)
    r.raise_for_status()
    text = re.sub(r"<[^>]+>", "\n", r.text)
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if 4 < len(l) < 120]
    jobs = []
    seen = set()
    for l in lines:
        norm = l
        for prefix in CUSTOM_NOISE_PREFIXES:
            if norm.lower().startswith(prefix):
                norm = norm[len(prefix):].strip()
                break
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        jid = hashlib.sha1(key.encode()).hexdigest()[:12]
        jobs.append({"id": jid, "title": norm, "url": url})
    return jobs


FETCHERS = {
    "greenhouse": lambda c: fetch_greenhouse(c["token"]),
    "lever": lambda c: fetch_lever(c["token"]),
    "smartrecruiters": lambda c: fetch_smartrecruiters(c["token"]),
    "ashby": lambda c: fetch_ashby(c["token"]),
    "workday": lambda c: fetch_workday(c["url"]),
    "custom": lambda c: fetch_custom(c["url"]),
}


def send_email(subject, html_body):
    if not RESEND_API_KEY or not ALERT_TO:
        print("RESEND_API_KEY or ALERT_TO not set, skipping email. Body:\n", html_body)
        return
    r = session.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": ALERT_FROM, "to": [ALERT_TO], "subject": subject, "html": html_body},
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
        seen_ids = set(load_json(state_file, []))

        try:
            jobs = fetcher(company)
        except Exception as e:
            print(f"[{name}] fetch failed: {e}", file=sys.stderr)
            continue

        matched = [j for j in jobs if matches_keywords(j["title"], keywords)]
        new_jobs = [j for j in matched if j["id"] not in seen_ids]

        if new_jobs:
            print(f"[{name}] {len(new_jobs)} new matching opening(s)")
            for j in new_jobs:
                all_new.append({"company": name, **j})

        all_matched_ids = {j["id"] for j in matched}
        state_file.write_text(json.dumps(sorted(all_matched_ids), indent=2))

    if all_new:
        rows = "".join(
            f"<li><b>{j['company']}</b> — <a href='{j['url']}'>{j['title']}</a></li>" for j in all_new
        )
        html = f"<p>{len(all_new)} new matching opening(s) found:</p><ul>{rows}</ul>"
        send_email(f"Job Alerts: {len(all_new)} new opening(s)", html)
    else:
        print("No new matching openings this run.")


if __name__ == "__main__":
    main()
