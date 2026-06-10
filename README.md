# 🏛️ Etimad Tenders → Supabase

Scrapes Etimad tenders (`tenders.etimad.sa`) and stores them in Supabase: active tenders daily,
a full historical backfill (~283k tenders), and award data (bidders + winners) for closed tenders.

## 📁 Project structure

```
etimad_tender/
├─ README.md                 ← this file
├─ requirements.txt          ← Python dependencies
├─ run_backfill.py           ← backfill orchestrator (awards → details)
├─ db/
│  └─ schema.sql             ← Supabase tables (run once in the SQL Editor)
├─ scrapers/
│  ├─ daily.py               ← daily scrape: active set + new tenders + details
│  ├─ parallel.py            ← parallel historical backfill + awards scrape (MODE=awards)
│  └─ probe.py               ← probe tool to discover the pagination cap (diagnostic)
├─ data/
│  ├─ activities.json        ← activity tree (used for sharding in parallel.py)
│  ├─ activity_totals.json
│  └─ sub_activity_totals.json
├─ tools/
│  └─ scrape_details_in_browser.js   ← scrape details from the browser console (fallback; contains the key)
└─ .github/workflows/
   ├─ scrape.yml             ← daily 6am Saudi time (scrapers/daily.py)
   ├─ awards-scrape.yml      ← weekly on Monday (scrapers/parallel.py MODE=awards)
   ├─ parallel-scrape.yml    ← manual: parallel backfill
   └─ probe-pagination-cap.yml ← manual: probe
```

## 🚀 Setup

### 1) Supabase project
- Create a project at [supabase.com](https://supabase.com).
- From `Settings → API` copy the `Project URL` and the `service_role` key (not the anon key).
- ⚠️ The service_role key bypasses RLS — keep it in secrets/`.env` only, and never commit it.

### 2) Tables
- Dashboard → SQL Editor → New Query → paste the contents of `db/schema.sql` → Run.
- Creates `tenders`, `scrape_runs`, `tender_awards`, and the `active_tenders` view.

### 3) Environment
Put these two in `.env` (locally) and in the GitHub repo secrets (for Actions):
```
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...
```

```bash
pip install -r requirements.txt
```

## ▶️ Running

**Daily scrape** (active set + new tenders + details, and deactivates anything that disappeared):
```bash
python scrapers/daily.py
```
Runs automatically every day via `scrape.yml` (cron, 6am Saudi time).

**Historical backfill** (awards first, then details — resumable from the DB):
```bash
USE_PROXY=false python run_backfill.py > backfill.log 2>&1
```
The orchestrator runs `scrapers/parallel.py` in stages and restarts it on a crash.
Tunables: `AWARD_CONCURRENCY`, `DETAIL_CONCURRENCY`, `DETAIL_BATCH`, `SKIP_AWARDS`, `SKIP_DETAILS`.

**Awards only** (bidders + winners for tenders in the "award approved" state):
```bash
MODE=awards python scrapers/parallel.py
```
Runs automatically every week via `awards-scrape.yml`. It auto-targets new tenders
(`get_ids_needing_awards`), and `awards_last_checked` prevents re-checking ones that came back empty.

> **DNS:** locally on Windows you may see intermittent `getaddrinfo failed` under load — set a
> fixed DNS (`1.1.1.1`). Every stage is resumable, so no progress is lost.

## 📊 Using the data

```sql
-- active tenders
select * from active_tenders where last_offer_date > now()
order by last_offer_date asc limit 50;

-- Arabic full-text search (trigram); e.g. 'صيانة' = "maintenance"
select tender_name, agency_name, last_offer_date from tenders
where tender_name % 'صيانة' and is_active = true
order by similarity(tender_name, 'صيانة') desc limit 20;

-- bidders and winners for a tender
select bidder_name, offer_value, tech_evaluation, award_value, role
from tender_awards where etimad_tender_id = '<id>' order by role, offer_value;

-- track scrapes
select * from scrape_runs order by started_at desc limit 10;
```

## 🔧 Common issues

| Issue | Fix |
|-------|-----|
| `HTTP 429 / Too Many Requests` | The IP hit the rate limit. The scripts have a rate-limiter + retries; lower the concurrency or wait |
| `HTTP 403` / redirect to login | Some endpoints are auth-walled for visitors — expected, the code handles it |
| `getaddrinfo failed` | Local DNS dropping out — set `1.1.1.1`. Resumable, so nothing is lost |
| Empty `place` on many rows | The relations endpoint 429s under concurrency; the daily scraper (sequential) fills it in for active tenders |

## 📝 Notes

- **Attachments:** the visitor view does not expose download links to non-registered suppliers. For
  official, sustainable access, apply for the official API: `apiportal.etimad.sa`.
- **`tender_purpose`** comes from the details page (reliable), **`place`** from the relations endpoint
  (rate-limited during backfill — higher coverage in the daily scraper).
- The Arabic field labels in `DETAIL_FIELD_LABELS` (in `scrapers/parallel.py` and `scrapers/daily.py`)
  may need tweaking if Etimad changes their HTML.

## 🛣️ Next steps

1. **Notifications** — alert the client (email/WhatsApp) when a tender in their sector is published.
2. **Semantic search** — `pgvector` on Supabase + embeddings.
3. **Dashboard** — Next.js + Supabase client.
4. **Move to the official API** — `apiportal.etimad.sa`.
