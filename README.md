# job-alerts

A free, self-hosted job-opening monitor. Every 6 hours (via GitHub Actions,
no server or laptop required) it checks a list of companies' career pages,
filters what it finds against your keywords, seniority, experience, and
location preferences, and emails you only the genuinely new openings —
grouped by company, tiered by how well they fit.

This isn't a hosted service — it's a script you fork and run under your own
GitHub account, with your own email destination. Nothing about your setup is
shared with anyone else's copy of this repo, and nothing here costs money.

## Setup — step by step

This should take about 10 minutes. Follow it in order; the two most common
snags (Actions being disabled, and the state-commit push failing) are called
out where they happen, not buried in a troubleshooting section at the end.

1. **Fork the repo** (button top-right on GitHub).

2. **Enable Actions on your fork.** GitHub disables Actions by default on
   forks. Go to the **Actions** tab on your fork — if you see a button
   saying *"I understand my workflows, go ahead and enable them"*, click it.

3. **Check workflow permissions.** Go to **Settings → Actions → General →
   Workflow permissions**, and make sure **"Read and write permissions"**
   is selected, then Save. (The workflow needs this to commit its own
   "seen job" tracking back to your repo after every run — without it, runs
   will succeed but fail at the last step with a permission error.)

4. **Get a free email-sending API key** from [resend.com](https://resend.com)
   (100 emails/day free, no credit card). On the free tier you can only send
   **to the email address you signed up with**, unless you verify your own
   domain — so sign up using the inbox you actually want alerts in.

5. **Add repo secrets** — **Settings → Secrets and variables → Actions →
   New repository secret**:
   - `RESEND_API_KEY` — the key from step 4
   - `ALERT_TO` — the email address to receive alerts (comma-separate for
     multiple recipients, but the free-Resend restriction above still
     applies per recipient)
   - `ALERT_FROM` — optional, defaults to `onboarding@resend.dev` if unset

6. **Edit `companies.json`** to your own target companies (see "Adding a
   company" below).

7. **Edit `keywords.json`** to the roles you're looking for.

8. **Tune the scoring and filters to your own profile** — see "Making it
   yours" below. This step matters more than it looks: the shipped defaults
   assume a specific experience level and location, and will silently
   filter out jobs that don't match those assumptions if left unedited.

9. **Push your changes to `main`.**

10. **Trigger a test run manually** — Actions tab → "Job Alerts" workflow →
    "Run workflow" button. Don't just wait for the 6-hour schedule for your
    first run; watch this one to confirm it actually works. It takes a few
    minutes (installing a headless browser + checking every company). Check
    for a green checkmark when it finishes, and click into the run's logs if
    it fails — the two most common failures are a missing/wrong secret
    (shows up as a `Resend error` in the "Check job openings" step) or the
    permissions issue from step 3 (shows up as a failure in the final
    "Commit updated state" step).

From here on, it runs automatically every 6 hours with no further action.

## How it works

- Companies on a known ATS — **Greenhouse, Lever, SmartRecruiters, Ashby,
  Workday** — are checked via that platform's public JSON API. Fast and
  reliable, no scraping involved.
- Companies with no public ATS are checked via a real headless browser
  (Playwright), which renders the page like a normal visitor would and reads
  the job titles off it. Slower and best-effort, since every custom career
  page is laid out differently.
- Every newly-discovered job is filtered:
  - **Keyword match** required (`keywords.json`)
  - **Seniority exclude** — Staff/Principal/Director/Architect/Manager-type
    titles are dropped by default
  - **Experience cap** — anything whose title *or full description*
    mentions more than N years required gets dropped (default: 3 years)
  - **Location filter** — only India-based or Remote roles pass by default
  - Everything that survives gets a **relevance score** and a tier label
    (🔥 Strong fit / 🟢 Good fit / 🟡 Stretch) from a weighted keyword list
- A job is only ever emailed once — state is tracked per company in `state/`
  and committed back to the repo after every run, so nothing repeats.
- Every company's fetch health (success/failure, and whether its job count
  looks suspiciously low compared to its usual baseline) is tracked in
  `state/_health.json`, so a silently-broken scraper doesn't go unnoticed.

## Making it yours: what to customize

Everything below lives in `scripts/check_jobs.py`. It's plain Python — no
config-file indirection, just edit the constants directly.

- **`RELEVANCE_WEIGHTS`** (~line 59) — the weighted keyword list that
  produces each job's score/tier. Shipped tuned for a Go/backend engineer;
  replace with weights matching your own stack (e.g. bump `java`/`spring` if
  that's your focus, add `react`/`typescript` for frontend, etc.)
- **`SENIORITY_EXCLUDE_WORDS`** / **`SENIORITY_EXCLUDE_PHRASES`** (~line 72) —
  titles containing these are dropped entirely, regardless of score. Adjust
  if you *want* Staff/Principal-level roles to show up.
- **`MAX_YEARS_EXPERIENCE`** (~line 87) — jobs requiring more years than this
  (detected from the title or the full job description) are hard-excluded.
  Set higher if you have more experience than the default of 3.
- **`tier_for_score`** — the score thresholds for 🔥/🟢/🟡. Adjust if your
  weights make everything cluster into one tier.
- **`INDIA_CITIES`** / **`NON_INDIA_REMOTE_MARKERS`** — the location
  allowlist. If you're not targeting India specifically, replace this with
  your own country/city list, or remove the location-filter line in
  `main()` entirely to disable location filtering.

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

To find a company's ATS type, check their careers page URL:
`boards.greenhouse.io` / `job-boards.greenhouse.io` → `greenhouse`;
`jobs.lever.co` → `lever`; `jobs.smartrecruiters.com` → `smartrecruiters`;
`jobs.ashbyhq.com` → `ashby`; `*.myworkdayjobs.com` → `workday`. If none of
those match, use `custom` with the direct URL to their job **listings** page
(not just their homepage) — it'll fall back to browser-based scraping, which
is noisier and may need iteration to work well for a given site.

**A real gotcha to know about, discovered the hard way:** a company's own
branded careers page (e.g. `careers.company.com`) and its underlying ATS
aren't always the same data. Always sanity-check that a company's `workday`/
ATS-sourced results roughly match what you see browsing their actual site —
enterprise HRIS backends can occasionally list requisitions (on hold,
internal-only, stale) that never appear on the public-facing page a real
referrer would check.

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
- As noted above, ATS-API data can occasionally drift from what's shown on
  a company's own public career page — treat a match as a strong signal to
  go check the real listing, not as a guarantee it's currently live.
- This is a single-tenant design — state is committed to *your* repo. There's
  no shared hosted version; everyone runs their own fork.

## License

MIT — see [LICENSE](LICENSE). Fork it, change it, use it however you like.
