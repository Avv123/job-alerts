#!/usr/bin/env python3
"""Check company career pages for new openings matching keywords, email on new matches."""
import html
import json
import os
import re
import sys
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

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ALERT_TO = [e.strip() for e in (os.environ.get("ALERT_TO") or "").split(",") if e.strip()]
ALERT_FROM = os.environ.get("ALERT_FROM") or "onboarding@resend.dev"

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


CUSTOM_NOISE_PREFIXES = (
    "share ", "apply ", "apply for ", "apply now ", "save ",
    "read more about the job ", "learn more about ", "more info about ",
    "position, ",
)
JOB_ID_SUFFIX_RE = re.compile(r"\s*,?\s*job\s*id\s*(?:is)?\s*[:\-]?\s*[a-f0-9-]{6,}\s*$", re.I)

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


def lines_to_jobs(text, url):
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
        norm = JOB_ID_SUFFIX_RE.sub("", norm).strip()
        key = norm.lower()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        jid = hashlib.sha1(key.encode()).hexdigest()[:12]
        jobs.append({"id": jid, "title": norm, "url": url})
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
    visible text but keep the full string in a title/aria-label attribute)."""
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
    page.close()
    full_text = text + "\n" + "\n".join(attr_texts)
    return lines_to_jobs(full_text, url)


FETCHERS = {
    "greenhouse": lambda c: fetch_greenhouse(c["token"]),
    "lever": lambda c: fetch_lever(c["token"]),
    "smartrecruiters": lambda c: fetch_smartrecruiters(c["token"]),
    "ashby": lambda c: fetch_ashby(c["token"]),
    "workday": lambda c: fetch_workday(c["url"]),
    "custom": lambda c: fetch_custom(c["url"]),
}


def build_email_html(all_new):
    by_company = OrderedDict()
    for j in all_new:
        by_company.setdefault(j["company"], []).append(j)

    company_blocks = []
    for company, jobs in by_company.items():
        job_links = "".join(
            f'''<a href="{html.escape(j['url'], quote=True)}"
                  style="display:block;padding:12px 14px;margin-bottom:8px;background:#f9fafb;
                         border:1px solid #eef0f3;border-radius:8px;text-decoration:none;
                         color:#1d4ed8;font-size:14px;font-weight:500;line-height:1.4;">
                {html.escape(j['title'])}
                <span style="color:#9ca3af;font-weight:400;">&nbsp;&rarr;</span>
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
            {job_links}
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

    close_browser()

    if all_new:
        send_email(f"Job Alerts: {len(all_new)} new opening(s)", build_email_html(all_new))
    else:
        print("No new matching openings this run.")


if __name__ == "__main__":
    main()
