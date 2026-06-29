# 🤖 Etimad Agent (serving layer)

A [Cycls](https://cycls.com) chat agent + scheduled email digest that serves the
data the scraper ingests into Supabase. The scraper (`../scraper/`) **writes**; the
agent **reads** the `active_tenders` view.

## Files
| File | Role |
|------|------|
| `main.py` | Cycls agent: `tender_search` tool over Supabase, system prompt, `/cron/daily-brief` + `/email/unsubscribe` FastAPI routes |
| `prompt.py` | System prompt (onboarding, returning-user tender check, subscription flow) — built per turn so the date stays fresh |
| `daily_brief.py` | Batch job: for each subscribed user, search tenders, build an Arabic RTL HTML email, send via Resend |
| `requirements.txt` | Local dev/deploy deps (the deployed container builds its own from `image.pip(...)` in `main.py`) |

## Setup
```bash
cd agent
pip install -r requirements.txt
cp ../.env.example .env      # then fill in the real values (see root README)
```

## Run / deploy
```bash
cd agent
uv run cycls run main.py     # local
uv run cycls deploy main.py  # deploy
```
> Run these **from inside `agent/`** — `main.py` copies `prompt.py`, `daily_brief.py`,
> and `.env` into the image using paths relative to the working directory.

## Daily brief
- Sent by `.github/workflows/daily-brief.yml` (07:00 Riyadh) which `POST`s
  `${ETIMAD_URL}/cron/daily-brief` with the `X-Cron-Secret` header.
- Requires repo secrets **`ETIMAD_URL`** and **`CRON_SECRET`**.
- Manual `workflow_dispatch` supports `dry_run` (build but don't send) and
  `only_user` (restrict to one user ID) for testing.

## Storage
User profile / email subscription state lives in Cycls' per-user DB
(`memory/profile`, `memory/email`, `memory/index`) — **not** in Supabase.
Supabase is read-only from the agent's perspective.
