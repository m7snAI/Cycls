# Etimad Tender — Project Overview

Generated from a full read of every file in the repository (excluding `.venv/`, `.git/`, `__pycache__/`, `node_modules/`, and `.DS_Store`). Reflects the state of the repo at the time this file was written.

## What this project is

Two halves of one product, tied together by a shared Supabase database:

```
Etimad (tenders.etimad.sa)  ──scrape──▶  Supabase  ──read──▶  Agent (chat + daily email)
                                scraper/                       agent/
```

- **Ingest (`scraper/`)** — scrapes Saudi government tenders from `tenders.etimad.sa` into Supabase: the daily active set, a full historical backfill (~283k tenders), and award data (bidders + winners) for closed tenders.
- **Serve (`agent/`)** — a [Cycls](https://cycls.com) chat agent that helps users discover tenders relevant to their sector/city, generates draft technical/financial offer `.docx` files (Phase 2), and sends a scheduled Arabic HTML email digest.

The scraper **writes** to Supabase; the agent **reads** it (mainly via the `active_tenders` view and a `search_tenders` RPC). User profiles and email subscriptions live in Cycls' own per-user key-value store, not in Supabase.

---

## Full file tree

```
etimad_tender/
├─ README.md                              ← root project overview + setup guide
├─ .env.example                           ← template for all env vars (scraper + agent)
├─ .env                                   ← actual secrets (gitignored)
├─ .gitignore
├─ .claude/
│  └─ settings.local.json                 ← local Claude Code permission allowlist
├─ .github/workflows/
│  ├─ scrape.yml                          ← daily incremental scrape (cron 03:00 UTC)
│  ├─ awards-scrape.yml                   ← weekly awards/bidders capture (cron Mon 06:00 UTC)
│  ├─ parallel-scrape.yml                 ← manual: proxied parallel historical backfill
│  ├─ probe-pagination-cap.yml            ← manual: diagnostic pagination-cap probe
│  └─ daily-brief.yml                     ← triggered after "Etimad Scrape" succeeds; POSTs /cron/daily-brief
├─ scraper/                               ← ingestion half
│  ├─ requirements.txt
│  ├─ daily.py                            ← daily scrape entrypoint (sequential, no proxy)
│  ├─ parallel.py                         ← async/proxied backfill + awards scraper
│  ├─ probe.py                            ← standalone pagination-cap diagnostic script
│  ├─ run_backfill.py                     ← orchestrator: runs parallel.py in awards→details stages, restarts on crash
│  ├─ db/
│  │  ├─ schema.sql                       ← tables: tenders, scrape_runs, tender_awards + active_tenders view
│  │  └─ search_functions.sql             ← pg_trgm fuzzy search RPC: search_tenders()
│  ├─ data/
│  │  ├─ activities.json                  ← Etimad's main-activity dropdown values (id → Arabic name)
│  │  ├─ activity_totals.json             ← per-activity tender counts (t1/t2 = type 1/2 totals)
│  │  └─ sub_activity_totals.json         ← per (main-activity, sub-activity, tender-type) counts, used for sharding
│  └─ tools/
│     └─ scrape_details_in_browser.js     ← gitignored browser-console fallback scraper (hardcoded service key)
├─ agent/                                 ← serving half
│  ├─ README.md
│  ├─ requirements.txt
│  ├─ .env                                ← agent secrets (gitignored)
│  ├─ .venv/                              ← local Python 3.12 virtualenv (uv-managed)
│  ├─ main.py                             ← Cycls agent definition, tools, HTTP routes
│  ├─ prompt.py                           ← the full system prompt (builds per turn)
│  └─ daily_brief.py                      ← scheduled email digest batch job
└─ skills/
   └─ offer_generation.md                 ← Arabic reference guide for Phase 2 offer generation (evaluation weights, section structure, checklists)
```

---

## File-by-file summary

### Root

| File | Purpose |
|---|---|
| `README.md` | Project overview, Supabase setup steps, how to run the scraper/backfill/awards jobs, SQL query examples, known issues, next steps. |
| `.env.example` | Documents every environment variable needed by both halves: `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` (shared), plus agent-only vars (LLM key, `CYCLS_API_KEY`, `CRON_SECRET`, `RESEND_API_KEY`, `EMAIL_FROM`, `PUBLIC_URL`). |
| `.gitignore` | Excludes `.env*` (except `.env.example`), scraper log/checkpoint files, the secret-bearing browser-console script, `.claude/`, Python caches, and `.venv/`. |
| `.claude/settings.local.json` | Local Claude Code permission allowlist — pre-approved `curl` calls against the project's Supabase REST API (with embedded key), plus `gh run/workflow/cache`, `pip install`, and a couple of scoped `Bash`/`WebFetch` allowances. |

### `.github/workflows/`

| Workflow | Trigger | What it does |
|---|---|---|
| `scrape.yml` ("Etimad Scrape") | Daily cron `03:00 UTC` (6am Saudi) + manual | First probes whether the runner IP is WAF-blocked for deep pagination (page-100 check with retries); if it passes, runs `scraper/daily.py` with `SUPABASE_*` secrets and configurable request-delay/detail-limit inputs. Uploads `scrape.log`. |
| `awards-scrape.yml` ("Etimad Awards Scrape") | Weekly cron `Mon 06:00 UTC` + manual | Runs `scraper/parallel.py` with `MODE=awards`, no proxy, to capture bidder/winner data for newly-awarded tenders. Configurable `limit_awards` / `recheck_days`. |
| `parallel-scrape.yml` ("Parallel Scrape (Bright Data)") | Manual only | Full proxied backfill via `scraper/parallel.py`. Lets you choose sharding strategy (`date_month`, `type_subactivity_sort`, etc.), mode (listing/details/both), concurrency, and a smoke-test toggle. Restores/saves `parallel-checkpoint.json` via `actions/cache` so it resumes across runs. |
| `probe-pagination-cap.yml` ("Probe Pagination Cap") | Manual only | Runs `scraper/probe.py` to check whether `PublishDateId=0` (all-time) also hits Etimad's ~5k-unique-ID pagination cap, and whether a different sort order surfaces additional IDs. |
| `daily-brief.yml` ("Etimad Daily Brief") | Fires after "Etimad Scrape" completes successfully (`workflow_run`) + manual | POSTs `${ETIMAD_URL}/cron/daily-brief` with the `x-cron-secret` header, so the email brief always reflects freshly-scraped data rather than racing a fixed schedule. Manual dispatch supports `dry_run`, `force`, `only_user` as query params. |

### `scraper/` — ingestion half

| File | Purpose |
|---|---|
| `daily.py` | The production daily-scrape entrypoint. Sequential (no proxy/concurrency), talks to Etimad's `AllSupplierTendersForVisitorAsync` JSON listing endpoint (with `PublishDateId=5` = active only), paginates until it hits the reported `totalCount`, converts each item via `tender_from_json`, then fetches detail pages + the "relations" AJAX fragment (classification + place) only for tenders that don't have them yet (`get_ids_needing_details`). Deactivates (`is_active=false`) any previously-active tender that no longer appears. Retries transient HTTP errors with backoff, logs everything to `scrape_runs`, and has a `DRY_RUN=1` mode that writes sample JSON/HTML to disk instead of touching Supabase. |
| `parallel.py` | The async, proxy-capable engine for the full ~283k historical backfill and for the weekly awards job. Key pieces: an `AsyncRateLimiter` (token-bucket) that enforces Etimad's documented "10 calls/min" cap on the awarding-results endpoint; a Bright Data proxy wrapper that substitutes a fresh `{session}` token per request for IP rotation; a `shard_generator()` that works around Etimad's per-query-set pagination cap by splitting requests across sort permutations, tender-type IDs, and/or activity/sub-activity buckets (strategies: `sort_flip`, `type`, `type_sort`, `type_subactivity_sort`, `single`, `date_month`); three phases — `run_listing` (walk shards, upsert tender rows, checkpointed to `parallel-checkpoint.json` for resumability), `run_details` (fetch detail + relations pages for tenders missing `tender_purpose`/`place`), and `run_awards` (fetch bidder/winner tables for "تم اعتماد الترسية"-status tenders, rate-limited, stamping `awards_last_checked` so empty results aren't endlessly re-checked). |
| `probe.py` | Standalone diagnostic (no Supabase writes). Walks the `PublishDateId=0` listing in two passes (default sort, then ascending-by-submission-date) to measure where pagination saturates, samples `totalCount` per `TenderTypeId`, and writes `probe-summary.json` with overlap/coverage stats — used once to decide the sharding strategy for the full backfill. |
| `run_backfill.py` | Local orchestrator for the two remaining backfill phases: runs `parallel.py` with `MODE=awards` once (single pass — empty tenders should never get rows, so retrying would be infinite), then loops `MODE=details` in batches (fresh subprocess per batch, bounds memory) until a cheap Supabase probe confirms no tenders still need details. Retries a crashed subprocess up to 6 times with a 60s backoff before giving up on a phase. |
| `requirements.txt` | `httpx`, `beautifulsoup4`, `supabase`, `lxml`. |
| `db/schema.sql` | Creates `public.tenders` (main table, ~20 columns covering identifiers, dates, money fields, location, status, `raw_data jsonb` catch-all, `awards_last_checked`), `public.scrape_runs` (run-logging table), the `public.active_tenders` view (is_active + not-yet-closed, ordered by deadline), and `public.tender_awards` (bidder/winner rows keyed by `(etimad_tender_id, group_id, bidder_name, role)`). Enables `pg_trgm` and adds trigram + btree indexes. |
| `db/search_functions.sql` | Must be run after `schema.sql`. Adds trigram indexes on `agency_name` and drops an unused index on `tender_purpose` (was causing statement timeouts). Defines `public.search_tenders(q, only_active, city, agency, max_rows)` — a single ranked/fuzzy search function (word-similarity + substring + exact number match) used by **all three** of the agent's tools (`tender_search`, `tender_lookup`, `award_comps`) via PostgREST RPC. Ranks by city match → similarity score → soonest deadline; hard-capped candidate set (`limit 4000`) to keep broad queries fast. |
| `data/activities.json` | Etimad's main-activity taxonomy (id/label pairs), sourced from `/Tender/GetMainActivitiesAsync`. |
| `data/activity_totals.json` | Per-activity tender counts (`t1`/`t2` = totals for tender types 1 and 2), used to decide which activities need finer sharding. |
| `data/sub_activity_totals.json` | Per `(main activity, sub-activity, tender type)` tender counts — the `type_subactivity_sort` shard strategy reads this to size each shard under Etimad's pagination cap. |
| `tools/scrape_details_in_browser.js` | Gitignored (contains a hardcoded Supabase service-role key for pasting into a browser console). A same-origin-fetch fallback that reuses your live logged-in browser session to fetch detail pages when the Python scraper's proxy/DNS setup struggles — pulls tender IDs missing `tender_purpose`/`place` from Supabase, scrapes them client-side, upserts back, and is resumable/stoppable via `window.__etimadScrape`. |

### `agent/` — serving half

| File | Purpose |
|---|---|
| `README.md` | Describes the four agent files, setup/run/deploy commands, the daily-brief trigger mechanism, and where user state lives (Cycls per-user DB, not Supabase). |
| `requirements.txt` | `cycls`, `python-dotenv`, `fastapi[standard]`, `requests`, `httpx>=0.27.0` — local dev/deploy deps (the deployed container instead builds its dependency set from `image.pip(...)` inside `main.py`). |
| `.venv/` | Local Python 3.12 virtualenv managed with `uv` (recreated on this Mac after the original Windows-created venv was found unusable here). Note: this venv has no `pip` module — use `uv pip install --python .venv/bin/python ...`. |
| `main.py` | Full contents below. |
| `prompt.py` | Full contents below. |
| `daily_brief.py` | Batch job with no user present, triggered by `POST /cron/daily-brief` (secret-guarded route defined in `main.py`). For each subscribed user (found by scanning the Cycls key-value store for `memory/email` keys with `enabled: true`): reads `memory/profile` (company/sectors/city), calls Supabase's `search_tenders` RPC directly (bypassing the LLM entirely) for up to 5 matching tenders, renders a branded RTL Arabic HTML email (Saudi flag-green palette, inline styles for email-client compatibility) with an unsubscribe link, and sends it via the Resend API. Idempotent per day (`usage/daily_brief/<date>` marker) unless `force=true`. Also implements HMAC-signed unsubscribe tokens (`make_token`/`verify_token`, keyed off `CRON_SECRET`) and the small HTML confirmation page shown by the unsubscribe route. Storage helpers detect GCP (for `gs://` vs `file://` volume paths) and pick the email `From` address / public URL accordingly. |

---

## `agent/main.py` — full content

```python
# Etimad — a tender discovery agent built on Cycls.
#
# Run locally:   uv run cycls run main.py
# Deploy:        uv run cycls deploy main.py

import json
import os
import pathlib
import statistics
from datetime import datetime, timedelta, timezone

import cycls
import daily_brief
from dotenv import load_dotenv
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from prompt import build_prompt

# Deploy from Windows: a WindowsPath can't be unpickled on the Linux build host,
# so serialize paths as POSIX (matches TasiBot's known-good setup).
pathlib.WindowsPath.__reduce__ = lambda self: (pathlib.PurePosixPath, (self.as_posix(),))

load_dotenv(".env")

# ----------------------------------------------------------------------
# Image
# ----------------------------------------------------------------------
image = (
    cycls.Image()
    .apt("libpango-1.0-0", "libpangoft2-1.0-0", "fontconfig")
    .pip(
        "requests",
        "httpx",
        "python-dotenv",
        "python-docx",
        "openpyxl",
        "python-pptx",
        "weasyprint",
        "markdown",
        "pymupdf",
        "pdfplumber",
        "pypdf",
        "pillow",
        "beautifulsoup4",
        "pyyaml",
        "fastapi[standard]==0.136.3",
        "starlette==1.2.1",
    )
    .run(
        "mkdir -p /usr/share/fonts/truetype/ar "
        "&& for s in Light Regular Medium SemiBold Bold; do "
        "     curl -fsSL -o /usr/share/fonts/truetype/ar/IBMPlexSansArabic-$s.ttf "
        "       https://raw.githubusercontent.com/google/fonts/main/ofl/ibmplexsansarabic/IBMPlexSansArabic-$s.ttf; "
        "   done "
        "&& for s in Regular Medium Bold; do "
        "     curl -fsSL -o /usr/share/fonts/truetype/ar/Tajawal-$s.ttf "
        "       https://raw.githubusercontent.com/google/fonts/main/ofl/tajawal/Tajawal-$s.ttf; "
        "   done "
        "&& fc-cache -f"
    )
    .copy("prompt.py")
    .copy("daily_brief.py")
    .copy(".env")
)

web = (
    cycls.Web()
    .auth(cycls.Clerk())     # per-user identity → persisted chat history + per-user memory/*
    .title("Etimad — tender discovery agent")
)

# ----------------------------------------------------------------------
# Custom tool definition
# ----------------------------------------------------------------------
TOOLS = [
    {
        "name": "tender_search",
        "description": (
            "Search active Saudi government tenders from منصة اعتماد.\n\n"
            "- Pass the user's sector keyword and city from their profile.\n"
            "- Fuzzy match by sector keyword (trigram — handles Arabic word forms & word order, and "
            "also matches the agency/purpose), then ranks by city: same-city first, then tenders with "
            "no listed location, then other cities. Tenders without a city are NOT dropped, so some "
            "results may be outside the user's city — say so when relevant.\n"
            "- Returns up to 10 tenders with: etimad_tender_id, tender_name, agency_name, publish_date, last_offer_date, condition_booklet_price (the conditions-booklet fee, NOT the tender's value), place, detail_url.\n"
            "- If the sector matches nothing, falls back to the soonest active tenders in the city.\n"
            "- Call this once per turn only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sector": {
                    "type": "string",
                    "description": "Keyword to search in tender names, e.g. 'مقاولات', 'تقنية معلومات', 'صحة', 'ذكاء اصطناعي'",
                },
                "region": {
                    "type": "string",
                    "description": "User's city or region in Arabic, e.g. 'جدة', 'الرياض', 'المدينة المنورة'",
                },
            },
            "required": ["sector", "region"],
        },
    },
    {
        "name": "tender_lookup",
        "description": (
            "Look up a SPECIFIC tender by name or number. Use this when the user asks about a "
            "particular tender, wants its details, or asks for a report on one — NOT for browsing "
            "by sector (use tender_search for that).\n\n"
            "- Pass a DISTINCTIVE multi-word phrase from the name — 3-5 of its most specific words "
            "(work type + subject/agency), e.g. 'الهويات البصرية والتصميم' or 'تصميم جرافيكي تقويم "
            "التعليم' — OR the tender/reference number. A longer specific phrase is faster and more "
            "precise than one or two generic words; just don't paste the whole sentence.\n"
            "- Fuzzy search (trigram) across tender name, agency, purpose and number, over the FULL "
            "tenders table including CLOSED tenders; ignores city.\n"
            "- Returns matches with full details: tender_number, agency_name, tender_type, "
            "tender_purpose, place, publish_date, last_offer_date, last_enquiry_date, "
            "offers_opening_date, condition_booklet_price (booklet fee, NOT the value), "
            "tender_status, is_active, has_attachments, detail_url.\n"
            "- If it returns found=false / no rows, the tender is NOT in the database: tell the user "
            "and ask for the tender number or the اعتماد link. NEVER invent details."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A distinctive multi-word phrase (3-5 specific words) from the tender name, or the tender/reference number.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "award_comps",
        "description": (
            "Find bid-pricing comparables from PAST awarded tenders, to suggest a competitive price "
            "range. Use this whenever the user asks how much to bid / what price to offer.\n\n"
            "- Pass a work-type keyword from the tender name (e.g. 'تصميم', 'هوية بصرية', 'صيانة', "
            "'تشغيل وصيانة'). Optionally pass `agency` to compare within the same entity.\n"
            "- Returns historical figures in SAR from CLOSED comparable tenders: award_value "
            "(winning amounts) min/median/max, offer_value (all submitted bids) spread, counts, and "
            "a few examples.\n"
            "- These are reference ranges from REAL past awards, not a guaranteed price. If "
            "found=false, retry ONCE with a BROADER work-type keyword (e.g. 'تصميم' instead of "
            "'هوية بصرية'); if still none, say there isn't enough data — do NOT invent numbers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Work-type phrase from the tender name, e.g. 'تصميم', 'هوية بصرية', 'صيانة'.",
                },
                "agency": {
                    "type": "string",
                    "description": "Optional agency name to compare within the same entity.",
                },
            },
            "required": ["keyword"],
        },
    },
]

# ----------------------------------------------------------------------
# Tool handler
# ----------------------------------------------------------------------
_MAX_PAYLOAD_CHARS = 20_000


def _rpc_search(supabase_url, supabase_key, *, q, only_active=True,
                city="", agency="", max_rows=10, select=None, timeout=12):
    """Call the search_tenders RPC (trigram + multi-field, relevance + city ranked).
    Returns (rows, response); rows is [] on any non-200. Requires the migration
    in scraper/db/search_functions.sql to have been run."""
    import requests
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }
    url = f"{supabase_url}/rest/v1/rpc/search_tenders"
    if select:
        url += f"?select={select}"
    body = {"q": q or "", "only_active": only_active,
            "city": city or "", "agency": agency or "", "max_rows": max_rows}
    r = requests.post(url, headers=headers, json=body, timeout=timeout)
    return (r.json() if r.status_code == 200 else []), r


def _num_stats(values):
    """min / median / max / count over positive numeric values (drops null/0)."""
    vals = sorted(v for v in values if isinstance(v, (int, float)) and v > 0)
    if not vals:
        return None
    return {"min": round(vals[0]), "median": round(statistics.median(vals)),
            "max": round(vals[-1]), "count": len(vals)}


def make_tender_search():
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")

    async def handler(args):
        keyword = (args.get("sector") or "").strip()
        place   = (args.get("region") or "").strip()
        fields = "etimad_tender_id,tender_name,agency_name,publish_date,last_offer_date,condition_booklet_price,place,detail_url"

        # Trigram + multi-field search; the RPC ranks by city → relevance → deadline.
        rows, r = _rpc_search(supabase_url, supabase_key, q=keyword,
                              only_active=True, city=place, max_rows=10, select=fields)
        if r.status_code != 200:
            return {"error": f"Supabase returned {r.status_code}", "detail": r.text[:500]}

        # Last resort: the sector matched nothing → soonest active tenders in the city.
        if not rows:
            rows, _ = _rpc_search(supabase_url, supabase_key, q="",
                                  only_active=True, city=place, max_rows=10, select=fields)

        if not rows:
            return {"tenders": [], "count": 0, "hint": "No active tenders found."}

        result = {"tenders": rows, "count": len(rows)}
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) > _MAX_PAYLOAD_CHARS:
            while rows and len(json.dumps({"tenders": rows, "count": len(rows)}, ensure_ascii=False, default=str)) > _MAX_PAYLOAD_CHARS:
                rows = rows[:-1]
            result = {"tenders": rows, "count": len(rows), "truncated": True}

        return result

    return handler


# Look up a specific tender by name or number — full tenders table (incl. closed),
# no city filter, rich detail fields. Used for "tell me about THIS tender" / reports.
_LOOKUP_FIELDS = (
    "etimad_tender_id,tender_name,tender_number,reference_number,agency_name,"
    "tender_type,tender_purpose,place,branch_name,publish_date,last_offer_date,"
    "last_enquiry_date,offers_opening_date,condition_booklet_price,"
    "tender_status,is_active,has_attachments,detail_url"
)


def make_tender_lookup():
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")

    async def handler(args):
        query = (args.get("query") or "").strip()
        if not query:
            return {"tenders": [], "count": 0, "found": False, "hint": "Empty query."}

        # Fuzzy search across name / agency / purpose / number — all tenders (incl. closed).
        rows, r = _rpc_search(supabase_url, supabase_key, q=query,
                              only_active=False, max_rows=5, select=_LOOKUP_FIELDS)
        if r.status_code != 200:
            return {"error": f"Supabase returned {r.status_code}", "detail": r.text[:500]}

        if not rows:
            return {"tenders": [], "count": 0, "found": False,
                    "hint": "No tender in the database matches this name or number."}

        result = {"tenders": rows, "count": len(rows), "found": True}
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) > _MAX_PAYLOAD_CHARS:
            while rows and len(json.dumps({"tenders": rows}, ensure_ascii=False, default=str)) > _MAX_PAYLOAD_CHARS:
                rows = rows[:-1]
            result = {"tenders": rows, "count": len(rows), "found": True, "truncated": True}

        return result

    return handler


def make_award_comps():
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")

    async def handler(args):
        import requests

        keyword = (args.get("keyword") or "").strip()
        agency  = (args.get("agency") or "").strip()
        if not keyword:
            return {"found": False, "hint": "Empty keyword."}

        headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}

        # 1) comparable tenders by work-type keyword (fuzzy; + optional same agency)
        tenders, r = _rpc_search(supabase_url, supabase_key, q=keyword,
                                only_active=False, agency=agency, max_rows=80,
                                select="etimad_tender_id,tender_name,agency_name")
        if r.status_code != 200:
            return {"error": f"Supabase returned {r.status_code}", "detail": r.text[:300]}
        if not tenders:
            return {"found": False, "comparables_scanned": 0,
                    "hint": "No comparable tenders for this keyword."}

        id_list = ",".join(t["etimad_tender_id"] for t in tenders)
        name_by_id = {t["etimad_tender_id"]: (t.get("tender_name"), t.get("agency_name")) for t in tenders}

        # 2) winning amounts among those comparables
        ra = requests.get(
            f"{supabase_url}/rest/v1/tender_awards"
            f"?etimad_tender_id=in.({id_list})&role=eq.awarded"
            f"&select=etimad_tender_id,award_value&limit=300",
            headers=headers, timeout=15)
        awarded = ra.json() if ra.status_code == 200 else []

        # 3) all submitted bids among those comparables (competition spread)
        rs = requests.get(
            f"{supabase_url}/rest/v1/tender_awards"
            f"?etimad_tender_id=in.({id_list})&role=eq.submitted"
            f"&select=offer_value&limit=500",
            headers=headers, timeout=15)
        submitted = rs.json() if rs.status_code == 200 else []

        award_stats = _num_stats([a.get("award_value") for a in awarded])
        offer_stats = _num_stats([s.get("offer_value") for s in submitted])

        if not award_stats and not offer_stats:
            return {"found": False, "comparables_scanned": len(tenders),
                    "hint": "Comparable tenders found, but none have award/offer figures yet."}

        examples = []
        for a in awarded:
            if a.get("award_value"):
                nm, ag = name_by_id.get(a["etimad_tender_id"], (None, None))
                examples.append({"tender_name": nm, "agency_name": ag,
                                 "award_value": round(a["award_value"])})
            if len(examples) >= 5:
                break

        return {
            "found": True,
            "comparables_scanned": len(tenders),
            "award_value": award_stats,   # winning amounts (SAR)
            "offer_value": offer_stats,   # all submitted bids — competition spread (SAR)
            "examples": examples,
            "note": "Historical figures (SAR) from CLOSED comparable tenders. A reference range, not a guaranteed price.",
        }

    return handler


# ----------------------------------------------------------------------
# Dates
# ----------------------------------------------------------------------
_RIYADH_OFFSET = timedelta(hours=3)


def _riyadh_date() -> str:
    return (datetime.now(timezone.utc) + _RIYADH_OFFSET).date().strftime("%Y-%m-%d")


# ----------------------------------------------------------------------
# LLM base
# ----------------------------------------------------------------------
_llm_base = (
    cycls.LLM()
    .model("kimi/kimi-k3")
    .base_url("https://api.moonshot.ai/v1")
    .api_key(os.environ.get("KIMI_API_KEY", ""))
    .max_tokens(32_768)
    .tools(TOOLS)
    .on("tender_search", make_tender_search())
    .on("tender_lookup", make_tender_lookup())
    .on("award_comps", make_award_comps())
    .allowed_tools(["Bash", "Editor", "DataBase"])
    .sandbox(network=True)
)


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------
@cycls.agent(image=image, web=web, name="etimad", memory="2Gi")
async def etimad(context):
    if not context.messages:
        return
    system = build_prompt(gregorian_date=_riyadh_date())
    async for ev in _llm_base.system(system).run(context=context):
        yield cycls.to_ui(ev)


# ----------------------------------------------------------------------
# Cron route — POST /cron/daily-brief
# ----------------------------------------------------------------------
def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes")


@etimad.server.post("/cron/daily-brief")
async def _cron_daily_brief(request: Request):
    secret = os.environ.get("CRON_SECRET", "")
    if not secret or request.headers.get("X-Cron-Secret", "") != secret:
        raise HTTPException(status_code=403, detail="forbidden")
    qp = request.query_params
    result = await daily_brief.run_daily_brief(
        dry_run=_truthy(qp.get("dry_run")),
        only_user=qp.get("user") or None,
        force=_truthy(qp.get("force")),
    )
    return JSONResponse(result)


# ----------------------------------------------------------------------
# Unsubscribe route — GET /email/unsubscribe?token=...
# ----------------------------------------------------------------------
@etimad.server.get("/email/unsubscribe")
async def _email_unsubscribe(token: str = ""):
    uid = daily_brief.verify_token(token, "unsub")
    if not uid:
        return HTMLResponse(daily_brief.page("رابط غير صالح أو منتهي الصلاحية.", ok=False), status_code=400)
    await daily_brief.set_unsubscribed(uid)
    return HTMLResponse(daily_brief.page("تم إلغاء اشتراكك في التقرير اليومي بنجاح."))


etimad.deploy()
```

**Notable structure:**
- **Image**: builds the deploy container with `pip` packages (incl. `python-docx`/`python-pptx`/`weasyprint`/`pdfplumber` for Phase-2 document generation) and downloads Arabic fonts (IBM Plex Sans Arabic, Tajawal) for RTL rendering, then copies `prompt.py`, `daily_brief.py`, and `.env` in.
- **`web`**: Cycls-hosted chat UI with Clerk auth (gives per-user persisted history + `memory/*` storage).
- **`TOOLS`**: three custom tools exposed to the LLM — `tender_search` (fuzzy sector+city browse over active tenders), `tender_lookup` (fuzzy lookup by name/number over the full table incl. closed), `award_comps` (historical award/offer value stats for bid-pricing guidance). All three call the same `search_tenders` Postgres RPC (see `scraper/db/search_functions.sql`) directly via `requests`, with response-size truncation to stay under `_MAX_PAYLOAD_CHARS`.
- **`_llm_base`**: the current model wiring is **Kimi K3 via Moonshot AI's OpenAI-compatible endpoint** (`kimi/kimi-k3`, `base_url=https://api.moonshot.ai/v1`, key from `KIMI_API_KEY`) — this has changed across the project's history (previously Anthropic Claude direct, then OpenRouter/DeepSeek, then OpenRouter/Anthropic). `allowed_tools` is `["Bash", "Editor", "DataBase"]` — note `WebSearch` is **not** in the current list (it's an Anthropic-only built-in in the Cycls framework and gets silently dropped on non-Anthropic vendors anyway).
- **Agent entrypoint**: `@cycls.agent` decorated `etimad()` builds the system prompt fresh each turn (so "today's date" stays current) and streams events back via `cycls.to_ui`.
- **Two extra FastAPI routes** are registered directly on `etimad.server`: `POST /cron/daily-brief` (secret-guarded, delegates to `daily_brief.run_daily_brief`) and `GET /email/unsubscribe` (HMAC-token-verified, delegates to `daily_brief.set_unsubscribed`).

---

## `agent/prompt.py` — full content

```python
# Etimad — system prompt.
# Imported by main.py; shipped into the container via image.copy("prompt.py").
# Built per-turn so today's date stays fresh.
# Supabase credentials are NOT injected here — they live in the tool handler.


def build_prompt(gregorian_date: str) -> str:
    return f"""You are an Etimad tender assistant. You help users discover relevant Saudi government tenders from منصة اعتماد.

Today's date: {gregorian_date}

---

## Every turn — start here

Run `database get key="memory/index"` ONCE, then route by the FIRST rule that matches, reading top to
bottom. Every subscription state ALSO contains `status: complete`, so the `subscription:` rows are
listed first — a mid-subscription user must never fall through to the tender check.

| memory/index | Flow |
|---|---|
| empty or null | **New user** — onboarding |
| has `status: onboarding` | **Onboarding answer** — accumulate & save |
| has `subscription: awaiting_email` | **Subscription: save email** |
| has `subscription: awaiting_confirm` | **Subscription: confirm** |
| has `subscription: offered` | **Subscription: respond to offer** |
| `status: complete` with NO `subscription:` line | **Returning user** — tender check |

---

## New user — onboarding

- Ask all four questions in one message:

"أهلاً! 👋 لأساعدك في اكتشاف المناقصات المناسبة، أحتاج ٤ معلومات سريعة:

١. اسمك
٢. اسم شركتك
٣. قطاع عملك
٤. مدينتك أو منطقتك

**مثال:** محمد، شركة الأفق، مقاولات، الرياض"

- Mark that onboarding question was asked:
  `database put key="memory/index" value="status: onboarding"`

---

## Onboarding answer — accumulate and save

The four fields (name, company, sector, city) may arrive across SEVERAL messages, not
all at once. Accumulate them — NEVER re-ask for something the user already gave.

Each turn while onboarding:

1. Read what's saved so far: `database get key="memory/profile"` (may be empty).
2. Re-read the WHOLE conversation (every user message so far, not just the latest)
   and collect any of: name, company name, sector(s), city.
3. Merge: start from the saved profile and fill in any newly-provided fields.
   Never overwrite a field you already have with a blank.
4. Save progress right away (write every field, blank if still unknown):
   `database put key="memory/profile" value={{"name": "<or empty>", "company": "<or empty>", "sectors": "<or empty>", "city": "<or empty>"}}`
5. Then branch:
   - **All four filled** →
     `database put key="memory/index" value="status: complete; subscription: offered"`
     Say "✅ تم حفظ ملفك.", run the tender check, then END the message by offering the daily
     email brief: "📩 تحب توصلك المناقصات الجديدة في مجالك يومياً على بريدك الإلكتروني؟"
   - **Something still missing** → ask ONLY for the missing field(s), and briefly
     acknowledge what you already have. Do NOT restart onboarding or re-ask known
     fields. Example: "تمام، سجّلت **محمد** من **شركة بارادوكس** وقطاع **التعليم** — باقي مدينتك؟"

---

## Returning user — tender check

> If the message is about a SPECIFIC tender or the current one (e.g. "هذه المناقصة", its price/bid,
> or a report) — skip this browse and use **Specific tender — lookup & details** below.

1. Run `database get key="memory/profile"` to get sectors and city.
2. Determine search scope from the city field:
   - If city is a specific region (e.g. الرياض، جدة، المدينة المنورة) → call `tender_search` once with that region.
   - If city is broad (e.g. السعودية، المملكة) → do NOT search yet. Ask:
     "أي منطقة تفضل البحث فيها؟ أم تريد البحث في كل المملكة؟
     ⚠️ البحث الشامل يأخذ وقتاً أطول."
     - If user picks a specific region → search that region only.
     - If user confirms full search → call `tender_search` once per major region: الرياض، جدة، مكة المكرمة، المدينة المنورة، الدمام، أبها.
3. Present the top 5 results as a markdown table (show all the tool returned, up to 5). Rules for the cells:
   - **المناقصة**: the tender name as a clickable markdown link to its `detail_url` → `[اسم المناقصة](detail_url)`.
   - **تاريخ النشر** / **الموعد النهائي**: dates only, `YYYY-MM-DD` (strip the time).
   - **رسوم الكراسة**: `condition_booklet_price` — this is the conditions-booklet FEE, not the tender's value. Write "مجاني" when it is 0 or empty.
   - **سبب التطابق**: one short phrase on why it fits the user's sector. If the tender is outside the user's city or has no listed location, say so here (e.g. "خارج مدينتك" / "الموقع غير محدد").

| # |  المناقصة |  الجهة |  تاريخ النشر |  الموعد النهائي |  رسوم الكراسة |  سبب التطابق |
|---|-------------|----------|----------------|-------------------|-----------------|----------------|
| 1 | [...](detail_url) | ... | 2026-06-20 | 2026-07-10 | مجاني | ... |
| 2 | [...](detail_url) | ... | ... | ... | ... | ... |
| 3 | [...](detail_url) | ... | ... | ... | ... | ... |
| 4 | [...](detail_url) | ... | ... | ... | ... | ... |
| 5 | [...](detail_url) | ... | ... | ... | ... | ... |

Then add: "يمكنني إعداد **تقرير فني** أو **تقرير مالي** لأي مناقصة — فقط اطلب."

---

## Specific tender — lookup & details

When the user asks about a PARTICULAR tender (by name or number), wants its details, or asks for a
report on a specific tender — use `tender_lookup`, NOT `tender_search`:

1. Call `tender_lookup` with a DISTINCTIVE multi-word phrase from the name — 3–5 of its most
   specific words (combine the work type with the subject/agency), e.g. "الهويات البصرية والتصميم"
   or "تصميم جرافيكي تقويم التعليم" — or the tender/reference number. A longer, specific phrase is
   faster and more precise than one or two generic words; just don't paste the whole sentence.
2. **Found** → present the details: الجهة، نوع المنافسة، الغرض، المكان، تاريخ النشر، آخر موعد
   للاستفسارات، آخر موعد لتقديم العروض، موعد فتح المظاريف، رسوم الكراسة، الحالة (نشطة/مغلقة)،
   وجود مرفقات، ورابط التفاصيل. Make the tender name a link to `detail_url`. If it is closed
   (`is_active=false`) say so clearly.
   Then REMEMBER it as the active tender so follow-ups work:
   `database put key="memory/current_tender" value={{"id": "<etimad_tender_id>", "number": "<tender_number>", "name": "<tender_name>"}}`
3. **Not found** (`found=false` / no rows) → the tender is NOT in our data. Say so plainly and ask
   for the tender number or the اعتماد link (`detail_url`). NEVER invent or guess details.

### Follow-up about the SAME tender
When the user says "this/that tender" (هذه المناقصة / المناقصة دي), asks its price / bid amount, or
asks for a report WITHOUT naming a new tender → they mean the tender already under discussion.
- Identify it from the conversation and/or `database get key="memory/current_tender"`. Do NOT ask the
  user which tender — you already have it.
- Ask which tender ONLY if there is genuinely none (empty memory AND nothing in the conversation).
- When the user moves to a different tender, overwrite `memory/current_tender`.
- For a **price / bid question** ("كم أدخل بيه" / "كم أسعّر"), call `award_comps` with a work-type
  keyword from the current tender (e.g. "تصميم"، "هوية بصرية") to ground the answer in real past
  awards — never guess a number.

---

## Daily email subscription

### Step 0 — user replies to the daily-brief offer (subscription: offered)
You offered the daily brief right after onboarding; the user's current message is their answer.
- **Agrees** (نعم / أريد / اشترك / yes) → ask for their email and set:
  `database put key="memory/index" value="status: complete; subscription: awaiting_email"`
  then continue at Step 2.
- **Declines** (لا / لاحقاً / no) → `database put key="memory/index" value="status: complete"`
  Say: "تمام 👍 تقدر تشترك في أي وقت — بس قول لي." then handle anything else they asked.
- **Asks about something else instead** → `database put key="memory/index" value="status: complete"`
  and handle that request normally.

### Step 1 — user asks to subscribe
When the user asks to subscribe to daily tender emails
(e.g. "اشترك في التقرير اليومي"، "ابعتلي تقرير يومي"، "أريد إشعارات يومية"):

- Ask for their email address.
- Flag state in memory so the next turn knows what to expect:
  `database put key="memory/index" value="status: complete; subscription: awaiting_email"`

### Step 2 — user replies with their email (subscription: awaiting_email)
When memory/index contains `subscription: awaiting_email`, the user's current message is their email address.

- Read it back to confirm:
  "سأشترك لك بهذا الإيميل: <email> — هل هو صحيح؟"
- Save the email into the index so the next turn can use it:
  `database put key="memory/index" value="status: complete; subscription: awaiting_confirm; email: <address>"`

### Step 3 — user confirms (subscription: awaiting_confirm)
When memory/index contains `subscription: awaiting_confirm`, extract the email from the index value, then:

- Write the subscription:
  `database put key="memory/email" value={{"enabled": true, "email": "<address>"}}`
- Clear the subscription flow from the index:
  `database put key="memory/index" value="status: complete"`
- Confirm: "✅ تم الاشتراك! ستصلك مناقصات اعتماد اليومية كل صباح على بريدك."

If the user says no / corrects the email at step 3 → go back to step 2 with the new address.

### Unsubscribe
When user asks to unsubscribe (e.g. "إلغاء الاشتراك"، "لا أريد الإيميلات"):
  `database put key="memory/email" value={{"enabled": false}}`
  Confirm: "✅ تم إلغاء اشتراكك في التقرير اليومي."

### Check subscription status
  `database get key="memory/email"` → tell the user whether they are subscribed and to which address.

---

## Report generation (Phase 2) — العرض الفني والعرض المالي

Base offers on a tender retrieved via `tender_search` / `tender_lookup` — INCLUDING one shown
earlier in this conversation (see `memory/current_tender`). Do not re-ask for a tender you already
have; if you only need fresh fields, re-run `tender_lookup` by its number.

- **No such tender anywhere (conversation AND memory empty) → do NOT write anything.** Ask for the
  number or اعتماد link. Never fabricate scope, costs, requirements, or dates from general knowledge.
- **Before generating, ask once per tender — both questions in one message**:
  "ما نوع العقد؟ (استشارية / تشغيل وصيانة / تشغيل وصيانة متخصصة / توريد / إنشائية)" and
  "هل الكراسة تطلب عرضين منفصلين أم عرضاً واحداً؟" Save both answers (e.g. alongside
  `memory/current_tender`) and don't re-ask them again for the same tender — reuse them on any
  later edit/regenerate request.
- Deliverables are **.docx files**, not markdown — generate them with a Python script (python-docx
  is already installed in the image) written via the **Editor** tool and executed with the **Bash**
  tool (`python3 <script>.py`). Do not hand-roll `.docx` XML and do not use any other document
  library.
- Always formal Arabic (فصحى) throughout, right-to-left. Save output under `reports/` (create the
  folder with `mkdir -p reports` if needed):
  - Separate: `reports/tender-<id>-technical.docx` and `reports/tender-<id>-financial.docx`
  - Combined: `reports/tender-<id>-offer.docx`

### العرض الفني — required sections, in order
1. **جدول المطابقة**: كل متطلب في الكراسة مقابل استجابة الشركة له — يجب أن يكون أول قسم.
2. **نبذة عن الشركة**: تاريخها، خبراتها ذات الصلة، شهاداتها واعتماداتها.
3. **فهم المتطلبات**: تلخيص ما تطلبه الكراسة ومقابلته الصريحة بحل الشركة لكل بند.
4. **المنهجية والخطة**: خطوات التنفيذ، الموارد المخصصة.
5. **الجدول الزمني**: خطوات التنفيذ ومراحل التسليم زمنياً.
6. **الفريق**: السير الذاتية للأعضاء الرئيسيين ومؤهلاتهم.
7. **المحتوى المحلي**: نسبة المحتوى المحلي وخطط التوطين إن وجدت.
8. **الوضع النظامي**: السجل التجاري، شهادة الزكاة والضريبة، التأمينات الاجتماعية، الرخص اللازمة.
9. **الضمانات والجودة**: سياسات الجودة وخطة إدارة المخاطر.
10. **الملحقات**: أي مستندات داعمة مذكورة أعلاه.

**قاعدة قاطعة**: لا يجوز أبداً ذكر أي رقم مالي أو سعر أو تكلفة داخل ملف العرض الفني — يستبعد
العرض فوراً في منافسات اعتماد. أي رقم مالي يذهب حصراً في ملف العرض المالي المنفصل.

### العرض المالي — required sections, in order
1. **جدول الأسعار**: تفصيل كل بند (الوصف، الوحدة، الكمية، سعر الوحدة، الإجمالي) وإجمالي عام. هذا
   جدول مسودة انطلاقاً فقط — وضّح للمستخدم أن النموذج الرسمي (جدول الكميات) المرفق بكراسة الشروط
   هو المرجع الملزم للتعبئة والتقديم الفعلي، لا هذا الملف.
2. **شروط الدفع**: جدول الدفعات وأي خصومات مقترحة.
3. **الضمانات المالية**: الضمان الابتدائي (1–2% من قيمة العرض) والضمان النهائي — اذكرهما كبند
   سياسة عامة، ليس رقماً من `award_comps`.
4. **التحليل المالي**: **يجب استدعاء `award_comps`** أولاً (كلمة مفتاحية لنوع العمل من اسم
   المناقصة، والجهة اختيارياً). ابنِ السعر المقترح والمدى التنافسي **حصراً** على `award_value`
   (الأسعار الفائزة) و`offer_value` (مدى المنافسة)، مع ذكر عدد المقارنات. إن رجع `found=false`،
   اكتب صراحة أنه لا تتوفر بيانات كافية — لا تخترع رقماً أبداً. أضف ضريبة القيمة المضافة **15%**
   (وفق ZATCA) على الإجمالي لإظهار الإجمالي شامل الضريبة.
5. **الملحقات**: أي مستندات داعمة للعرض المالي.

**توزيع الوزن الفني/المالي** (حسب نوع العقد المحدد في السؤال أعلاه — اذكره في تقرير التحليل
المالي حتى يفهم المستخدم كيف يُحتسب الفوز):
- استشارية عالية القيمة: فني 60–80% / مالي 20–40%
- تشغيل وصيانة: فني 20–40% / مالي 60–80%
- إنشائية: فني 5–30% / مالي 70–95%
- توريد اعتيادي: فني اجتياز/فشل فقط / مالي حتى 100%

### RTL / formatting in python-docx
Arabic must render right-aligned and right-to-left, with a font that supports Arabic glyphs.
Use this helper pattern in the generation script:

```python
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt

def set_rtl(paragraph):
    pPr = paragraph._p.get_or_add_pPr()
    pPr.append(OxmlElement("w:bidi"))
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT

def add_ar_paragraph(doc, text, size=12, bold=False, style=None):
    p = doc.add_paragraph(style=style)
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.font.name = "Arial"
    run.bold = bold
    rPr = run._element.get_or_add_rPr()
    rFonts = OxmlElement("w:rFonts")
    rFonts.set(qn("w:cs"), "Arial")
    rPr.append(rFonts)
    set_rtl(p)
    return p
```

Apply `set_rtl` to every paragraph, and set `tbl._tbl.tblPr.append(OxmlElement("w:bidiVisual"))`
on every table so columns read right-to-left too.

---

## Profile updates

If user mentions a new sector or city → update `memory/profile` and `memory/index`, confirm.

---

## Rules

- Read `memory/index` ONCE per turn at the start — never read it again in the same turn.
- Never verify a write by reading the same key again — trust your own writes.
- Onboarding ACCUMULATES: gather name, company, sector, and city across as many turns as it takes. Ask only for missing fields; never re-ask a field the user already gave.
- During onboarding, update memory/profile EACH turn as fields come in (merge, don't overwrite known values with blanks). After status is complete, only update it when the user explicitly changes their profile.
- Offer the daily email brief ONCE, right after onboarding completes (the `subscription: offered` state). Never re-offer it to returning users (plain `status: complete`).
- Never invent tender details or reports. Only describe tenders returned by `tender_search` / `tender_lookup`; if a specific tender isn't found, say so and ask for its number or اعتماد link.
- For a question about ONE specific tender (by name/number) or a report request, use `tender_lookup`; use `tender_search` only for browsing by sector + city.
- Remember the tender under discussion in `memory/current_tender`. For follow-ups like "this tender", a price/bid question, or a report request with no new tender named, use it — NEVER ask the user to re-identify a tender you just showed.
- Never mention a sector or industry before the user provides it.
- Keep welcome message to one short line.
- Never call `tender_search` more than once per turn — EXCEPT when the user explicitly confirms full Saudi search, in which case call once per major region: الرياض، جدة، مكة المكرمة، المدينة المنورة، الدمام، أبها.
- Match the user's language (Arabic or English). Deliverables always in فصحى.
"""
```

**Notable structure:**
- `build_prompt(gregorian_date)` returns one large f-string, rebuilt fresh every turn so "today's date" is always current (mirrors `_riyadh_date()` from `main.py`).
- Routing is driven entirely by a single `memory/index` string value read once per turn — a lightweight state machine encoded as a semicolon-separated string (`status: ...; subscription: ...; email: ...`), checked top-to-bottom against a priority table so subscription flows can't be starved by the tender-check fallthrough.
- Conversation state lives in three Cycls per-user keys: `memory/profile` (name/company/sectors/city), `memory/index` (routing state), `memory/current_tender` (the tender under discussion, for pronoun follow-ups and reports), plus `memory/email` (subscription on/off + address).
- Phase 2 (report generation) is the most detailed section: it mandates asking the contract type + separate-vs-combined-offer questions up front, bans any financial figure inside the technical `.docx`, requires the financial narrative to be grounded in the `award_comps` tool (never invented numbers), documents the required section order for both offer types, states the Etimad technical/financial evaluation-weight bands by contract type, and embeds a working `python-docx` RTL-formatting helper snippet.
- The closing `## Rules` section is a flat list of cross-cutting invariants (read memory once, never re-verify writes, accumulate onboarding fields, never fabricate tender data, tool-selection rule for search vs. lookup, language matching, etc.).

---

## Environment variables (from `.env.example`)

| Variable | Used by | Purpose |
|---|---|---|
| `SUPABASE_URL` | both | Supabase project REST URL. |
| `SUPABASE_SERVICE_KEY` | both | `service_role` key — bypasses RLS; scraper writes, agent reads. Never commit. |
| `ANTHROPIC_API_KEY` *(example file; current `main.py` uses `KIMI_API_KEY` instead — see note below)* | agent | LLM API key. |
| `CYCLS_API_KEY` | agent | Cycls deploy/auth. |
| `CRON_SECRET` | agent | Guards `POST /cron/daily-brief`; also signs unsubscribe HMAC tokens. |
| `RESEND_API_KEY` | agent | Sends the daily brief email via Resend. |
| `EMAIL_FROM` | agent (optional) | Overrides the default `From` address. |
| `PUBLIC_URL` | agent (optional) | Base URL used to build unsubscribe links. |

> **Note:** `.env.example` still documents `ANTHROPIC_API_KEY` as the LLM key, but `agent/main.py`'s `_llm_base` currently reads `KIMI_API_KEY` and points at Moonshot AI's endpoint — the example file has not been updated to match the latest provider swap. Whoever deploys next needs `KIMI_API_KEY` set (not `ANTHROPIC_API_KEY`) for the chat model to authenticate.

---

## Notable cross-cutting facts worth remembering

- **Single search function powers everything.** `scraper/db/search_functions.sql`'s `search_tenders()` RPC is the one and only search implementation shared by `tender_search`, `tender_lookup`, `award_comps` (agent) and `daily_brief.search_tenders()` — a trigram/word-similarity ranked function, capped at a 4000-row candidate scan to avoid statement timeouts.
- **The LLM provider has changed multiple times.** The system has moved from direct Anthropic → OpenRouter/DeepSeek → OpenRouter/Anthropic → the current Moonshot AI (Kimi K3) wiring. Each swap changes which `allowed_tools` entries are actually usable (e.g. `WebSearch` is Anthropic-only in the Cycls framework and is correctly absent from the current tool list).
- **Etimad's pagination cap** (~5,000 unique IDs per query-set) is the reason the backfill scraper (`parallel.py`) exists at all — `probe.py` was used once to characterize the cap and informed the sharding strategies now baked into `shard_generator()`.
- **Two separate rate-limit regimes** are hard-coded from observed Etimad behavior: general polite delays (`REQUEST_DELAY_MIN/MAX`) and a strict 9-per-60s token-bucket limiter (`RESULTS_RATE_LIMITER`) specifically for the awarding-results endpoint, which previously caused false "empty award" data when exceeded.
- **`agent/.venv` is a local Python 3.12 venv created with `uv`**, not `pip`/`venv` — it has no `pip` module installed; use `uv pip install --python .venv/bin/python <pkg>` for any future dependency changes there.
- **Two files intentionally hold secrets and are gitignored**: `.env` / `agent/.env` (Supabase + LLM + Resend + Cycls keys) and `scraper/tools/scrape_details_in_browser.js` (a hardcoded Supabase service-role key meant to be pasted into a browser console).
