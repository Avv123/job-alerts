# job-alerts

A free, self-hosted job-opening monitor. Every 6 hours (via GitHub Actions,
no server or laptop required) it checks a list of companies' career pages,
filters what it finds against your keywords, seniority, experience, and
location preferences, and emails you only the genuinely new openings —
grouped by company, tiered by how well they fit.

This isn't a hosted service — it's a script you fork and run under your own
GitHub account, with your own email destination. Nothing about your setup is
shared with anyone else's copy of this repo.

## How it works

- **37+ companies** (adjust as you add your own) are checked via each ATS's
  public JSON API — Greenhouse, Lever, SmartRecruiters, Ashby, Workday. Fast
  and reliable, no scraping involved.
- Companies with no public ATS are checked via a real headless browser
  (Playwright), which renders the page like a normal visitor would and reads
  the job titles off it. Slower and best-effort, since every custom career
  page is laid out differently.
- Every newly-discovered job is filtered:
  - **Keyword match** required (`keywords.json`)
  - **Seniority exclude** — Staff/Principal/Director/Architect/Manager-type
    titles are dropped by default
  - **Experience cap** — anything whose title *or full description* mentions
    more than N years required gets dropped (default cap: 3 years)
  - **Location filter** — only India-based or Remote roles pass, by default
  - Everything that survives gets a **relevance score** and a tier label
    (🔥 Strong fit / 🟢 Good fit / 🟡 Stretch) based on a weighted keyword list
- A job is only ever emailed once — state is tracked per company in `state/`
  and committed back to the repo after every run, so nothing repeats.
- Every company's fetch health (success/failure, and whether its job count
  looks suspiciously low compared to its usual baseline) is tracked in
  `state/_health.json`, so a silently-broken scraper doesn't go unnoticed.

## Setup (forking this for yourself)

1. **Fork the repo.**
2. **Get a free email-sending API key** from [resend.com](https://resend.com)
   (100 emails/day free). On the free tier you can only send to the email
   address you signed up with, unless you verify your own domain.
3. **Add repo secrets** — Settings → Secrets and variables → Actions:
   - `RESEND_API_KEY` — your Resend API key
   - `ALERT_TO` — the email address to receive alerts (comma-separate for
     multiple recipients — note the free-Resend caveat above still applies
     per recipient)
   - `ALERT_FROM` — optional, defaults to `onboarding@resend.dev`
4. **Edit `companies.json`** to your own target companies (see below).
5. **Edit `keywords.json`** to the roles you're looking for.
6. **Tune the scoring/filters to your own profile** — see next section.
7. Push to `main`. The workflow (`.github/workflows/job-alerts.yml`) runs
   automatically every 6 hours from then on — no further action needed. You
   can also trigger a run manually from the Actions tab any time.

## Making it yours: what to customize

Everything below lives in `scripts/check_jobs.py`. It's plain Python — no
config file indirection, just edit the constants directly.

- **`RELEVANCE_WEIGHTS`** (~line 59) — the weighted keyword list that
  produces each job's score/tier. Shipped tuned for a Go/backend engineer;
  replace with weights matching your own stack (e.g. bump `java`/`spring` if
  that's your focus, add `react`/`typescript` for frontend, etc.)
- **`SENIORITY_EXCLUDE_WORDS`** / **`SENIORITY_EXCLUDE_PHRASES`** (~line 72) —
  titles containing these are dropped entirely, regardless of score. Adjust
  if you *want* Staff/Principal-level roles to show up.
- **`MAX_YEARS_EXPERIENCE`** (~line 87) — jobs requiring more years than this
  (detected from the title or the full job description) are hard-excluded.
  Set higher if you have more experience than the current default of 3.
- **`tier_for_score`** (~line 111) — the score thresholds for
  🔥/🟢/🟡. Adjust if your weights make everything cluster into one tier.
- **`INDIA_CITIES`** / **`NON_INDIA_REMOTE_MARKERS`** (~line 126) — the
  location allowlist. If you're not targeting India specifically, replace
  this with your own country/city list, or delete the location-filter line
  in `main()` entirely to disable location filtering.

## Adding a company

Edit `companies.json`. Each entry is one of:

```json
{ "name": "Example", "type": "greenhouse", "token": "example" }
{ "name": "Example", "type": "lever", "token": "example" }
{ "name": "Example", "type": "smartrecruiters", "token": "example" }
{ "name": "Example", "type": "ashby", "token": "example" }
{ "name": "Example", "type": "workday", "url": "https://tenant.wdN.myworkdayjobs.com/Site_Name" }
{ "name": "Example", "type": "custom", "url": "https://example.com/careers" }
```

To find a company's ATS type: check their careers page URL and network
requests. `boards.greenhouse.io` / `job-boards.greenhouse.io` → `greenhouse`;
`jobs.lever.co` → `lever`; `jobs.smartrecruiters.com` → `smartrecruiters`;
`jobs.ashbyhq.com` → `ashby`; `*.myworkdayjobs.com` → `workday`. If none of
those match, use `custom` with the direct URL to their job listings page (not
just their homepage) — it'll fall back to browser-based scraping, which is
noisier and may need iteration to work well for a given site.

## Run locally

```bash
pip install -r requirements.txt
playwright install chromium
RESEND_API_KEY=... ALERT_TO=you@example.com python scripts/check_jobs.py
```

Without `RESEND_API_KEY`/`ALERT_TO` set, it prints what it would have emailed
instead of sending — useful for testing changes before they go live.

## Known limitations

- Companies with strong bot-protection (Cloudflare-style challenges) may
  fail to fetch even with a real browser — these show up as `broken` in
  `state/_health.json` rather than silently vanishing.
- `custom`-scraped companies depend on each site's own page structure and
  may need per-site tweaks (see `fetch_custom` / `lines_to_jobs` in the
  script) if a particular site's listings aren't being read correctly.
- This is a single-tenant design — state is committed to *your* repo. There's
  no shared hosted version; everyone runs their own fork.

## License

MIT — see [LICENSE](LICENSE). Fork it, change it, use it however you like.
