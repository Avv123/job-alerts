# job-alerts

Checks career pages of tracked companies every 6 hours (via GitHub Actions) and emails
new openings that match `keywords.json`, using the Resend API.

## Setup

Repo secrets required (Settings -> Secrets and variables -> Actions):

- `RESEND_API_KEY` — API key from resend.com
- `ALERT_TO` — email address to receive alerts
- `ALERT_FROM` — verified sender address (defaults to `onboarding@resend.dev` if unset)

## Adding a company

Edit `companies.json`. Each entry is one of:

```json
{ "name": "Example", "type": "greenhouse", "token": "example" }
{ "name": "Example", "type": "lever", "token": "example" }
{ "name": "Example", "type": "smartrecruiters", "token": "example" }
{ "name": "Example", "type": "custom", "url": "https://example.com/careers" }
```

`greenhouse`/`lever`/`smartrecruiters` use each ATS's public JSON API (reliable).
`custom` does a best-effort HTML text scrape — noisier, used when a company has no
public ATS API or uses a JS-rendered career site.

## Run locally

```bash
pip install -r requirements.txt
RESEND_API_KEY=... ALERT_TO=you@example.com python scripts/check_jobs.py
```

## Editing keywords

Edit `keywords.json` — case-insensitive substring match against job titles.
