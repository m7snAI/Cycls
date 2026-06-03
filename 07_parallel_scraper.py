"""
Async parallel Etimad scraper for the full ~283k backfill.

Design:
- Bright Data (or compatible) rotating proxy via BRIGHTDATA_PROXY_URL.
  If the URL contains the literal token {session}, we substitute a random session
  string per request so each request comes from a fresh IP. Without {session},
  the URL is used as-is (relies on the proxy provider's own rotation).
- Sharding to break Etimad's ~5k per-result-set pagination cap. Strategies:
    sort_flip   — same filter, 4 sort permutations (≤20k coverage; cheap baseline)
    type        — one shard per TenderTypeId in {1..15} (covers most types)
    type_sort   — type × sort permutations (up to ~60 shards; broadest coverage)
- Two phases:
    LISTING  — walk pages per shard sequentially, accumulate ID → tender row,
               batch-upsert to Supabase. Each shard stops at saturation
               (SHARD_SATURATION_THRESHOLD consecutive 0-new pages).
    DETAILS  — read ids-needing-details from Supabase, fan out detail+relations
               fetches across DETAIL_CONCURRENCY workers, batch-upsert.
- Resume-safe:
    LISTING tracks completed shards in CHECKPOINT_FILE; restart skips them.
    DETAILS recomputes ids-needing-details from DB each start; restart is no-op
    for already-detailed rows.

SMOKE TEST MODE: set LIMIT_PAGES_PER_SHARD=200 + LIMIT_DETAILS=200 + LIMIT_SHARDS=2
to validate proxy + sharding cheaply before burning quota on the full run.
"""
from __future__ import annotations

import os
import re
import json
import time
import random
import asyncio
import calendar
import logging
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, quote

import httpx
from bs4 import BeautifulSoup
from supabase import create_client, Client

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------- Configuration ----------
BASE_URL = "https://tenders.etimad.sa"
LISTING_API_URL = f"{BASE_URL}/Tender/AllSupplierTendersForVisitorAsync"
LISTING_REFERER = f"{BASE_URL}/Tender/AllTendersForVisitor"
RELATIONS_URL = f"{BASE_URL}/Tender/GetRelationsDetailsViewComponenet"
AWARDING_GROUPS_URL = f"{BASE_URL}/Tender/GetAwardingTenderGroupsForVisitorViewComponent"
AWARDING_RESULTS_URL = f"{BASE_URL}/Tender/GetAwardingResultsForVisitorViewComponenet"
AWARDED_STATUS = "تم اعتماد الترسية"

PAGE_SIZE = 24
PUBLISH_DATE_ID = int(os.environ.get("PUBLISH_DATE_ID", "0"))  # 0=all-time

BRIGHTDATA_PROXY_URL = os.environ.get("BRIGHTDATA_PROXY_URL", "").strip()
USE_PROXY = os.environ.get("USE_PROXY", "true").strip().lower() not in ("false", "0", "no", "")
SHARD_STRATEGY = os.environ.get("SHARD_STRATEGY", "type_sort")
MODE = os.environ.get("MODE", "both")  # listing | details | both

SHARD_CONCURRENCY = int(os.environ.get("SHARD_CONCURRENCY", "10"))
DETAIL_CONCURRENCY = int(os.environ.get("DETAIL_CONCURRENCY", "50"))
AWARD_CONCURRENCY = int(os.environ.get("AWARD_CONCURRENCY", "10"))
LIMIT_AWARDS = int(os.environ.get("LIMIT_AWARDS", "0"))
# Awarded-status tenders that come back empty (Etimad hasn't published the
# bidder/award breakdown yet) get stamped tenders.awards_last_checked and are
# skipped on subsequent runs until this many days pass — then re-checked once,
# in case Etimad published late. Keeps the recurring awards job short instead of
# re-fetching ~13k permanent-empties every run. 0 = re-check every run (old behavior).
AWARDS_RECHECK_DAYS = int(os.environ.get("AWARDS_RECHECK_DAYS", "30"))

LIMIT_SHARDS = int(os.environ.get("LIMIT_SHARDS", "0"))
LIMIT_PAGES_PER_SHARD = int(os.environ.get("LIMIT_PAGES_PER_SHARD", "0"))
LIMIT_DETAILS = int(os.environ.get("LIMIT_DETAILS", "0"))

SHARD_SATURATION_THRESHOLD = int(os.environ.get("SHARD_SATURATION_THRESHOLD", "20"))
MAX_PAGES_PER_SHARD = int(os.environ.get("MAX_PAGES_PER_SHARD", "3000"))

# Per-page jittered delay. Smoke test (2026-05-18) showed ~8 req/sec back-to-back
# trips per-IP rate limits within ~5 min. ~2s/page keeps a sustained run polite.
REQUEST_DELAY_MIN = float(os.environ.get("REQUEST_DELAY_MIN", "1.5"))
REQUEST_DELAY_MAX = float(os.environ.get("REQUEST_DELAY_MAX", "2.5"))

TIMEOUT = 30
MAX_RETRIES = 3
UPSERT_BATCH_SIZE = 50
CHECKPOINT_FILE = Path(os.environ.get("CHECKPOINT_FILE", "parallel-checkpoint.json"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("etimad-parallel")


# ---------- Rate limiter ----------
class AsyncRateLimiter:
    """Token-bucket-ish rate limiter for asyncio. Each acquire() blocks until
    the call falls within the policy of max N calls per period seconds.

    Used to respect Etimad's explicit `max 10 calls per 1 minute` quota on the
    awarding-results endpoint (the 429 body says exactly that). Without this
    limiter, concurrency=5 routinely overran the cap and ~30% of tenders ended
    up "empty" because all 3 of their retry attempts fell within the same
    bad-minute window — not because Etimad had no data for them."""

    def __init__(self, max_calls: int, period_s: float):
        self.max_calls = max_calls
        self.period_s = period_s
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        while True:
            async with self._lock:
                now = time.monotonic()
                # Drop timestamps older than the period — they're outside the window.
                while self._timestamps and self._timestamps[0] <= now - self.period_s:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return
                # Otherwise: wait until the oldest call ages out. Compute outside the
                # lock to let other waiters re-check after sleep.
                wait_s = self._timestamps[0] + self.period_s - now + 0.05
            await asyncio.sleep(max(wait_s, 0.1))


# Etimad caps the Results endpoint at 10/min per IP. Use 9 for a safety buffer
# (clock drift, retries, etc.); even 1 leftover slot avoids the 429 cliff.
RESULTS_RATE_LIMITER = AsyncRateLimiter(max_calls=9, period_s=60.0)


# ---------- Proxy ----------
def proxy_for_request() -> str | None:
    """Return a proxy URL with {session} substituted (or the URL as-is).
    Returns None if USE_PROXY=false even when BRIGHTDATA_PROXY_URL is set —
    lets us test direct-runner-IP behavior without unsetting the secret."""
    if not USE_PROXY or not BRIGHTDATA_PROXY_URL:
        return None
    if "{session}" in BRIGHTDATA_PROXY_URL:
        session = f"{random.randint(0, 2**31):x}"
        return BRIGHTDATA_PROXY_URL.replace("{session}", session)
    return BRIGHTDATA_PROXY_URL


def make_client() -> httpx.AsyncClient:
    """An AsyncClient with no built-in proxy — we pass a fresh proxy per call."""
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": random.choice(USER_AGENTS)},
    )


async def fetch_json(params: dict, attempt_label: str = "") -> dict | None:
    """GET the listing API. New AsyncClient per request so proxy stays fresh."""
    for attempt in range(MAX_RETRIES):
        proxy = proxy_for_request()
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT,
                follow_redirects=True,
                proxy=proxy,
            ) as client:
                r = await client.get(
                    LISTING_API_URL,
                    params=params,
                    headers={
                        "User-Agent": random.choice(USER_AGENTS),
                        "Accept": "application/json, text/plain, */*",
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": LISTING_REFERER,
                    },
                )
                ctype = r.headers.get("content-type") or ""
                if r.status_code == 200 and "json" in ctype:
                    return r.json()
                # 400/429/503 or HTML-instead-of-JSON = WAF blip; retry with fresh session
                log.warning("[%s] HTTP %d ctype=%s attempt=%d/%d",
                            attempt_label, r.status_code, ctype, attempt + 1, MAX_RETRIES)
        except httpx.RequestError as e:
            log.warning("[%s] net error attempt=%d/%d: %s", attempt_label, attempt + 1, MAX_RETRIES, e)
        await asyncio.sleep(2 * (attempt + 1))
    return None


async def fetch_html(url: str, params: dict | None = None, referer: str | None = None,
                    attempt_label: str = "") -> str | None:
    for attempt in range(MAX_RETRIES):
        proxy = proxy_for_request()
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT,
                follow_redirects=True,
                proxy=proxy,
            ) as client:
                headers = {
                    "User-Agent": random.choice(USER_AGENTS),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
                }
                if referer:
                    headers["Referer"] = referer
                if params and "X-Requested-With" in (params.get("_x_requested_with") or ""):
                    headers["X-Requested-With"] = "XMLHttpRequest"
                r = await client.get(url, params=params or {}, headers=headers)
                final_host = (r.url.host or "") if r.url else ""
                if "login.etimad.sa" in final_host:
                    log.warning("[%s] redirected to login (auth-walled) attempt=%d/%d",
                                attempt_label, attempt + 1, MAX_RETRIES)
                elif r.status_code == 200 and r.text:
                    return r.text
                else:
                    log.warning("[%s] HTTP %d attempt=%d/%d for %s",
                                attempt_label, r.status_code, attempt + 1, MAX_RETRIES, url)
        except httpx.RequestError as e:
            log.warning("[%s] net error attempt=%d/%d: %s", attempt_label, attempt + 1, MAX_RETRIES, e)
        await asyncio.sleep(2 * (attempt + 1))
    return None


# ---------- Listing JSON → tender row ----------
def _clean_iso(value):
    if not value or not isinstance(value, str):
        return None
    if value.startswith("0001-") or value.startswith("0000-"):
        return None
    if "+" in value or value.endswith("Z"):
        return value
    return value + "+03:00"


def tender_from_json(item: dict) -> dict | None:
    tid = item.get("tenderIdString")
    if not tid:
        return None
    if item.get("isUGRP") and item.get("ugrpRfxUrl"):
        detail_url = urljoin(BASE_URL, item["ugrpRfxUrl"])
    else:
        detail_url = f"{BASE_URL}/Tender/DetailsForVisitor?STenderId={quote(tid, safe='')}"
    return {
        "etimad_tender_id": tid,
        "tender_name": item.get("tenderName") or "بدون اسم",
        "tender_number": item.get("tenderNumber"),
        "reference_number": item.get("referenceNumber"),
        "agency_name": item.get("agencyName"),
        "branch_name": item.get("branchName"),
        "tender_type": item.get("tenderTypeName"),
        "tender_status": item.get("tenderStatusName"),
        "last_offer_date": _clean_iso(item.get("lastOfferPresentationDate")),
        "last_enquiry_date": _clean_iso(item.get("lastEnqueriesDate")),
        "offers_opening_date": _clean_iso(item.get("offersOpeningDate")),
        "publish_date": _clean_iso(item.get("submitionDate")),
        "condition_booklet_price": item.get("condetionalBookletPrice"),
        "invitation_cost": item.get("invitationCost"),
        "detail_url": detail_url,
        "raw_data": {
            "tender_id": item.get("tenderId"),
            "tender_status_id": item.get("tenderStatusId"),
            "tender_type_id": item.get("tenderTypeId"),
            "tender_activity": item.get("tenderActivityName"),
            "inside_ksa": item.get("insideKSA"),
            "is_ugrp": item.get("isUGRP"),
            "financial_fees": item.get("financialFees"),
            "buying_cost": item.get("buyingCost"),
        },
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "is_active": True,
    }


# ---------- Detail page parsing (same as 02_scraper) ----------
DETAIL_FIELD_LABELS = {
    "الرقم المرجعي": "reference_number", "رقم المنافسة": "tender_number",
    "اسم المنافسة": "tender_name", "نوع المنافسة": "tender_type",
    "الغرض من المنافسة": "tender_purpose",
    "اسم الجهة الحكومية": "agency_name", "الجهة الحكومية": "agency_name",
    "اسم الفرع": "branch_name",
    "آخر موعد لتقديم العروض": "last_offer_date",
    "آخر موعد لاستلام الاستفسارات": "last_enquiry_date",
    "آخر موعد للاستفسارات": "last_enquiry_date",
    "موعد فتح المظاريف": "offers_opening_date",
    "تاريخ النشر": "publish_date",
    "ثمن كراسة الشروط": "condition_booklet_price",
    "قيمة وثائق المنافسة": "condition_booklet_price",
    "تكلفة الدعوة": "invitation_cost",
    "مكان التنفيذ": "place",
    "طريقة تقديم العروض": "submitting_method",
    "طريقة التقديم": "submitting_method",
    "حالة المنافسة": "tender_status",
}
DETAIL_DATE_FIELDS = {"last_offer_date", "last_enquiry_date", "offers_opening_date", "publish_date"}
DETAIL_MONEY_FIELDS = {"condition_booklet_price", "invitation_cost"}


def _parse_arabic_date(text):
    if not text:
        return None
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", text.strip())
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(
            f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
            f"T{(m.group(4) or '00').zfill(2)}:{(m.group(5) or '00').zfill(2)}:00+03:00"
        )
        return dt.isoformat()
    except ValueError:
        return None


def _parse_money(text):
    if not text:
        return None
    clean = re.sub(r"[^\d.,]", "", text).replace(",", "")
    try:
        return float(clean) if clean else None
    except ValueError:
        return None


def _clean_label(text: str) -> str:
    return re.sub(r"[:：\s]+$", "", text.strip())


def parse_detail_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    pairs: list[tuple[str, str]] = []
    for title in soup.select(".etd-item-title"):
        parent = title.parent
        if not parent:
            continue
        info = parent.select_one(".etd-item-info")
        if not info:
            continue
        info_clone = BeautifulSoup(str(info), "html.parser")
        for tag in info_clone.select("i.readMore, i.readLess"):
            tag.decompose()
        spans = [s.get_text(" ", strip=True) for s in info_clone.find_all("span")]
        spans = [s for s in spans if s]
        value_text = max(spans, key=len) if len(spans) >= 2 else info_clone.get_text(" ", strip=True)
        pairs.append((title.get_text(" ", strip=True), value_text))
    fields: dict = {}
    for raw_label, raw_value in pairs:
        col = DETAIL_FIELD_LABELS.get(_clean_label(raw_label))
        if not col:
            continue
        v = raw_value.strip()
        if not v or v in ("—", "-", "غير محدد"):
            continue
        if col in DETAIL_DATE_FIELDS:
            p = _parse_arabic_date(v)
            if p:
                fields[col] = p
        elif col in DETAIL_MONEY_FIELDS:
            p = _parse_money(v)
            if p is not None:
                fields[col] = p
        else:
            fields[col] = v
    return fields


def parse_awarding_groups(html: str) -> list[tuple[int, str]]:
    """Parse the Groups endpoint HTML into [(group_id, group_name), ...].
    Each tender has 1+ groups (lots). Most have a single "حزمة افتراضية" default."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[int, str]] = []
    for a in soup.select("a.awardingGroupForVisitor"):
        gid_raw = a.get("data-id")
        if not gid_raw:
            continue
        try:
            gid = int(gid_raw)
        except (TypeError, ValueError):
            continue
        out.append((gid, a.get_text(" ", strip=True)))
    return out


def parse_awarding_results(html: str, group_id: int, group_name: str | None) -> list[dict]:
    """Parse the Results endpoint HTML into bidder rows.
    The endpoint returns two tables: first is submitted (bidder, offer, tech_eval),
    second is awarded (bidder, offer, award_value). Returns a flat list of dicts
    with role='submitted' or 'awarded' to match the tender_awards schema."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select("table")
    out: list[dict] = []
    for ti, tbl in enumerate(tables[:2]):  # only first two tables matter
        role = "submitted" if ti == 0 else "awarded"
        for tr in tbl.select("tbody tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.select("td")]
            if len(cells) < 2 or not cells[0]:
                continue
            row = {
                "group_id": group_id,
                "group_name": group_name,
                "bidder_name": cells[0],
                "offer_value": _parse_money(cells[1]) if len(cells) > 1 else None,
                "role": role,
            }
            if role == "submitted":
                row["tech_evaluation"] = cells[2] if len(cells) > 2 else None
                row["award_value"] = None
            else:
                row["tech_evaluation"] = None
                row["award_value"] = _parse_money(cells[2]) if len(cells) > 2 else None
            out.append(row)
    return out


def parse_relations_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    pairs: dict[str, str] = {}
    for title in soup.select(".etd-item-title"):
        parent = title.parent
        if not parent:
            continue
        info = parent.select_one(".etd-item-info")
        if info:
            label = _clean_label(title.get_text(" ", strip=True))
            value = info.get_text(" ", strip=True)
            if value:
                pairs[label] = value
    out: dict = {}
    if pairs:
        out["_raw"] = pairs
        place = pairs.get("مكان التنفيذ")
        if place:
            out["place"] = place
    return out


# ---------- Sharding ----------
SORT_PERMS = [
    {},  # default (no sort param ≈ DESC submission)
    {"Sort": "SortBySubmissionDate", "SortDirection": "ASC", "IsSearch": "true"},
    {"Sort": "SortByOfferOpeningDate", "SortDirection": "DESC", "IsSearch": "true"},
    {"Sort": "SortByOfferOpeningDate", "SortDirection": "ASC", "IsSearch": "true"},
]

# TenderTypeId observed values; 15 is UGRP. Some IDs may have 0 results — that's fine.
TENDER_TYPE_IDS = list(range(1, 11)) + [15]

# Main activities exceeding 5012 in either type=1 or type=2, per 2026-05-18 probe.
# These get sub-activity sharding; others are scraped as a single (type, main) shard.
OVER_CAP_MAIN_AIDS = {1, 2, 3, 8, 9, 11, 19}


def _load_activity_probe_data() -> tuple[list[int], dict[tuple[int, int, int], int]]:
    """Load activities + sub-activity counts. activities.json: from /Tender/GetMainActivitiesAsync.
    sub_activity_totals.json: probe results of (main, sub, type) → totalCount.
    Returns (all_main_aids, sub_count_by_key)."""
    with open("activities.json", encoding="utf-8") as f:
        activities = json.load(f)
    with open("sub_activity_totals.json", encoding="utf-8") as f:
        sub_totals = json.load(f)
    mains = [int(a["value"]) for a in activities
             if str(a.get("value", "")).isdigit() and int(a["value"]) <= 100]
    sub_by_key: dict[tuple[int, int, int], int] = {}
    for r in sub_totals:
        if isinstance(r.get("totalCount"), int):
            sub_by_key[(int(r["main"]), int(r["sub"]), int(r["type"]))] = r["totalCount"]
    return mains, sub_by_key


def shard_generator(strategy: str) -> Iterator[tuple[str, dict]]:
    if strategy == "sort_flip":
        for i, sort in enumerate(SORT_PERMS):
            label = "default" if not sort else f"{sort['Sort']}-{sort['SortDirection']}"
            yield f"sort:{label}", {**sort}
    elif strategy == "type":
        for tid in TENDER_TYPE_IDS:
            yield f"type:{tid}", {"TenderTypeId": tid, "IsSearch": "true"}
    elif strategy == "type_sort":
        for tid in TENDER_TYPE_IDS:
            for i, sort in enumerate(SORT_PERMS):
                label = "default" if not sort else f"{sort['Sort']}-{sort['SortDirection']}"
                yield f"type:{tid}|sort:{label}", {"TenderTypeId": tid, "IsSearch": "true", **sort}
    elif strategy == "type_subactivity_sort":
        mains, sub_data = _load_activity_probe_data()
        for tid in (1, 2):
            for main_aid in mains:
                if main_aid not in OVER_CAP_MAIN_AIDS:
                    # Main fits under the 5k cap for both types — single shard
                    yield (
                        f"t{tid}|m{main_aid}",
                        {"TenderTypeId": tid, "TenderActivityId": main_aid, "IsSearch": "true"},
                    )
                else:
                    # Over-cap main — shard by sub-activity
                    subs_for_main = sorted({k[1] for k in sub_data if k[0] == main_aid})
                    for sub_aid in subs_for_main:
                        count = sub_data.get((main_aid, sub_aid, tid), 0)
                        if count == 0:
                            continue  # empty bucket for this type, skip
                        base = {
                            "TenderTypeId": tid, "TenderActivityId": main_aid,
                            "TenderSubActivityId": sub_aid, "IsSearch": "true",
                        }
                        if count <= 5000:
                            yield f"t{tid}|m{main_aid}|s{sub_aid}", base
                        else:
                            # Sub still exceeds cap — emit 4 sort permutations to maximize coverage
                            for sort in SORT_PERMS:
                                label = "default" if not sort else f"{sort['Sort']}-{sort['SortDirection']}"
                                yield f"t{tid}|m{main_aid}|s{sub_aid}|{label}", {**base, **sort}
    elif strategy == "single":
        # PublishDateId only — single shard, useful for testing the cap baseline
        yield "single", {}
    elif strategy == "date_month":
        # One shard per calendar month between DATE_SHARD_START and now (inclusive).
        # Each month-bucket usually stays under the 5012-per-shard cap on its own;
        # for peak months that exceed it, the run still gets the first ~5012 and we
        # add bi-weekly sub-shards in a follow-up pass.
        start_yyyymm = os.environ.get("DATE_SHARD_START", "202001")  # earliest data is 2020
        try:
            start_year, start_month = int(start_yyyymm[:4]), int(start_yyyymm[4:])
        except ValueError:
            raise ValueError(f"DATE_SHARD_START must be YYYYMM, got {start_yyyymm!r}")
        end = datetime.now(timezone.utc)
        y, m = start_year, start_month
        while (y, m) <= (end.year, end.month):
            last_day = calendar.monthrange(y, m)[1]
            from_str = f"01/{m:02d}/{y}"
            to_str = f"{last_day:02d}/{m:02d}/{y}"
            yield (
                f"m:{y}{m:02d}",
                {
                    "IsSearch": "true",
                    "FromLastOfferPresentationDateString": from_str,
                    "ToLastOfferPresentationDateString": to_str,
                },
            )
            m += 1
            if m == 13:
                m, y = 1, y + 1
    else:
        raise ValueError(f"Unknown SHARD_STRATEGY: {strategy}")


# ---------- Supabase (sync client, called from async via to_thread) ----------
class Repo:
    def __init__(self, url: str, key: str):
        self.c: Client = create_client(url, key)

    def start_run(self) -> int:
        return self.c.table("scrape_runs").insert({"status": "running"}).execute().data[0]["id"]

    def finish_run(self, run_id: int, **kwargs):
        kwargs["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.c.table("scrape_runs").update(kwargs).eq("id", run_id).execute()

    def reap_stale_runs(self, current_run_id: int) -> list[int]:
        """Mark any still-'running' rows from crashed/cancelled prior runs as
        failed. Prior runs leak 'running' because a workflow timeout or Ctrl-C
        kills the process before finish_run fires. Excludes the current run.
        Returns the reaped ids."""
        r = (self.c.table("scrape_runs")
             .select("id")
             .eq("status", "running")
             .neq("id", current_run_id)
             .execute())
        ids = [row["id"] for row in (r.data or [])]
        if ids:
            self.c.table("scrape_runs").update({
                "status": "failed",
                "error_message": "reaped on next-run startup (process died before finish_run)",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }).in_("id", ids).execute()
        return ids

    def upsert_tenders(self, tenders: list[dict]) -> tuple[int, int]:
        if not tenders:
            return 0, 0
        ids = [t["etimad_tender_id"] for t in tenders]
        BATCH = 200
        existing_ids: set[str] = set()
        for i in range(0, len(ids), BATCH):
            r = self.c.table("tenders").select("etimad_tender_id").in_(
                "etimad_tender_id", ids[i:i + BATCH]
            ).execute()
            existing_ids.update(row["etimad_tender_id"] for row in r.data)
        new_n = sum(1 for t in tenders if t["etimad_tender_id"] not in existing_ids)
        upd_n = len(tenders) - new_n
        # Retry each batch on transient Supabase/HTTP blips. A multi-day details
        # run regularly hits home-network connection resets (WinError 10054 /
        # RemoteProtocolError); without this a single blip kills the whole run.
        UPSERT_RETRIES = 8
        for i in range(0, len(tenders), BATCH):
            batch = tenders[i:i + BATCH]
            for attempt in range(UPSERT_RETRIES):
                try:
                    self.c.table("tenders").upsert(
                        batch, on_conflict="etimad_tender_id"
                    ).execute()
                    break
                except (httpx.RemoteProtocolError, httpx.RequestError,
                        httpx.HTTPStatusError) as e:
                    if attempt == UPSERT_RETRIES - 1:
                        raise
                    sleep_s = min(30, 3 * (attempt + 1) + attempt * attempt)
                    log.warning("upsert_tenders batch retry %d/%d in %ds: %s",
                                attempt + 1, UPSERT_RETRIES, sleep_s, e)
                    time.sleep(sleep_s)
        return new_n, upd_n

    def get_ids_needing_details(self, limit: int = 0) -> list[str]:
        """Return tender IDs that haven't had their detail page fetched.
        Marker: both `tender_purpose` and `place` are NULL. Both fields are only
        populated by the detail page / relations endpoint, not by the listing JSON
        (the listing provides reference_number, so we can't use that as a marker)."""
        out: list[str] = []
        BATCH = 1000
        offset = 0
        while True:
            q = (self.c.table("tenders")
                 .select("etimad_tender_id")
                 .is_("tender_purpose", "null")
                 .is_("place", "null")
                 .range(offset, offset + BATCH - 1))
            r = q.execute()
            rows = r.data or []
            out.extend(row["etimad_tender_id"] for row in rows)
            if len(rows) < BATCH:
                break
            offset += BATCH
            if limit and len(out) >= limit:
                out = out[:limit]
                break
        return out

    def get_ids_needing_awards(self, limit: int = 0) -> list[str]:
        """Return tender IDs with status='تم اعتماد الترسية' that have no rows in
        tender_awards yet AND weren't checked-and-found-empty within the last
        AWARDS_RECHECK_DAYS. Dedup checked once at startup, in batches of 1000.

        Falls back gracefully (no empty-skipping) if the awards_last_checked
        column doesn't exist yet — so a recurring run works before the ALTER
        TABLE is applied, just less efficiently."""
        BATCH = 1000
        # cutoff: tenders stamped more recently than this are skipped as
        # known-empty. None disables the skip (AWARDS_RECHECK_DAYS=0).
        cutoff = None
        if AWARDS_RECHECK_DAYS > 0:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=AWARDS_RECHECK_DAYS)).isoformat()

        # Detect whether the awards_last_checked column exists; pick select cols.
        select_cols = "etimad_tender_id,awards_last_checked"
        try:
            self.c.table("tenders").select(select_cols).limit(1).execute()
        except Exception:
            log.warning("awards_last_checked column not found — empty-skip "
                        "disabled (run the ALTER TABLE from 01_supabase_schema.sql)")
            select_cols = "etimad_tender_id"
            cutoff = None

        # All awarded-status tenders, skipping recently-checked-empty ones.
        skip_recent = 0
        all_ids: list[str] = []
        offset = 0
        while True:
            r = (self.c.table("tenders")
                 .select(select_cols)
                 .eq("tender_status", AWARDED_STATUS)
                 .range(offset, offset + BATCH - 1))
            rows = r.execute().data or []
            for row in rows:
                last = row.get("awards_last_checked") if cutoff else None
                if last and last >= cutoff:
                    skip_recent += 1
                    continue
                all_ids.append(row["etimad_tender_id"])
            if len(rows) < BATCH:
                break
            offset += BATCH
        if skip_recent:
            log.info("AWARDS: skipped %d awarded tenders checked-empty within %dd",
                     skip_recent, AWARDS_RECHECK_DAYS)

        # IDs already in tender_awards (may have many rows per tender)
        done_ids: set[str] = set()
        offset = 0
        while True:
            r = (self.c.table("tender_awards")
                 .select("etimad_tender_id")
                 .range(offset, offset + BATCH - 1))
            rows = r.execute().data or []
            done_ids.update(row["etimad_tender_id"] for row in rows)
            if len(rows) < BATCH:
                break
            offset += BATCH

        remaining = [t for t in all_ids if t not in done_ids]
        if limit and len(remaining) > limit:
            remaining = remaining[:limit]
        return remaining

    def mark_awards_checked(self, ids: list[str]) -> None:
        """Stamp tenders.awards_last_checked=now() for these ids so empty ones
        aren't re-fetched every run. Best-effort: silently no-ops if the column
        doesn't exist yet."""
        if not ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        BATCH = 200
        for i in range(0, len(ids), BATCH):
            batch = ids[i:i + BATCH]
            try:
                (self.c.table("tenders")
                 .update({"awards_last_checked": now})
                 .in_("etimad_tender_id", batch)
                 .execute())
            except Exception as e:
                log.warning("mark_awards_checked batch failed (column missing?): %s", e)
                return

    def upsert_awards(self, rows: list[dict]) -> int:
        """Upsert award rows. Unique constraint on
        (etimad_tender_id, group_id, bidder_name, role) prevents duplicates.
        Retries each batch on transient Supabase/HTTP errors — long runs
        regularly hit HTTP/2 stream resets and they shouldn't kill the job."""
        if not rows:
            return 0
        # Dedupe within input by unique-constraint key. Some Etimad tenders list
        # the same bidder twice in one group/role (data quirk on their side);
        # Postgres rejects an upsert that proposes duplicate keys in one batch.
        # Last occurrence wins.
        deduped: dict[tuple, dict] = {}
        for row in rows:
            key = (row["etimad_tender_id"], row["group_id"],
                   row["bidder_name"], row["role"])
            deduped[key] = row
        rows = list(deduped.values())
        BATCH = 200
        # Use longer retry budget than fetch retries: home-network DNS or
        # ISP outages can last several minutes, and losing a 3-day run to a
        # transient blip is much worse than waiting it out.
        UPSERT_RETRIES = 8
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            for attempt in range(UPSERT_RETRIES):
                try:
                    self.c.table("tender_awards").upsert(
                        batch,
                        on_conflict="etimad_tender_id,group_id,bidder_name,role",
                    ).execute()
                    break
                except (httpx.RemoteProtocolError, httpx.RequestError,
                        httpx.HTTPStatusError) as e:
                    if attempt == UPSERT_RETRIES - 1:
                        raise
                    # backoff: 3, 6, 10, 15, 20, 25, 30, 30s — ~140s total budget
                    sleep_s = min(30, 3 * (attempt + 1) + attempt * attempt)
                    log.warning("upsert_awards batch retry %d/%d in %ds: %s",
                                attempt + 1, UPSERT_RETRIES, sleep_s, e)
                    time.sleep(sleep_s)
        return len(rows)

    def fetch_tender_skeletons(self, ids: list[str]) -> dict[str, dict]:
        """Get current rows for these IDs (we need detail_url + existing raw_data)."""
        out: dict[str, dict] = {}
        BATCH = 200
        for i in range(0, len(ids), BATCH):
            r = self.c.table("tenders").select(
                "etimad_tender_id,detail_url,raw_data"
            ).in_("etimad_tender_id", ids[i:i + BATCH]).execute()
            for row in r.data:
                out[row["etimad_tender_id"]] = row
        return out


# ---------- Checkpoint ----------
def load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("checkpoint corrupted, starting fresh")
    return {"completed_shards": [], "started_at": datetime.now(timezone.utc).isoformat()}


def save_checkpoint(state: dict):
    CHECKPOINT_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------- LISTING phase ----------
async def scrape_shard(shard_label: str, shard_params: dict, repo: Repo,
                      seen_global: set[str]) -> dict:
    """Walk one shard sequentially, batch-upsert as we go."""
    log.info("[%s] starting", shard_label)
    consecutive_zero = 0
    consecutive_fail = 0
    pages_walked = 0
    new_ids_in_shard = 0
    buffer: list[dict] = []
    total_upserted_new = 0
    total_upserted_upd = 0
    api_total = None
    # If we see this many failed pages in a row, the IP has likely hit a WAF wall
    # for this shard — bail out to save retry budget for other shards.
    consecutive_fail_threshold = int(os.environ.get("SHARD_FAIL_THRESHOLD", "8"))

    max_pages = LIMIT_PAGES_PER_SHARD or MAX_PAGES_PER_SHARD
    for page in range(1, max_pages + 1):
        if page > 1 and REQUEST_DELAY_MAX > 0:
            await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
        params = {"PageNumber": page, "PageSize": PAGE_SIZE,
                  "PublishDateId": PUBLISH_DATE_ID, **shard_params}
        resp = await fetch_json(params, attempt_label=f"{shard_label} p{page}")
        if not resp:
            consecutive_fail += 1
            log.warning("[%s] page %d: failed (consec_fail=%d/%d)",
                        shard_label, page, consecutive_fail, consecutive_fail_threshold)
            if consecutive_fail >= consecutive_fail_threshold:
                log.warning("[%s] %d consecutive page failures — IP likely WAF-walled, stopping shard",
                            shard_label, consecutive_fail)
                break
            continue
        consecutive_fail = 0
        pages_walked += 1
        items = resp.get("data") or []
        if api_total is None:
            api_total = resp.get("totalCount") or 0
            log.info("[%s] api totalCount=%d", shard_label, api_total)
        if not items:
            log.info("[%s] page %d: empty, stopping", shard_label, page)
            break

        before = len(seen_global)
        rows_this_page: list[dict] = []
        for it in items:
            t = tender_from_json(it)
            if not t:
                continue
            if t["etimad_tender_id"] in seen_global:
                continue
            seen_global.add(t["etimad_tender_id"])
            rows_this_page.append(t)
            buffer.append(t)
        new = len(seen_global) - before
        new_ids_in_shard += new

        if new == 0:
            consecutive_zero += 1
        else:
            consecutive_zero = 0

        if page % 25 == 0 or new < len(items):
            log.info("[%s] page %d items=%d new=%d shard_total=%d global_total=%d consec0=%d",
                     shard_label, page, len(items), new, new_ids_in_shard,
                     len(seen_global), consecutive_zero)

        if len(buffer) >= UPSERT_BATCH_SIZE:
            n, u = await asyncio.to_thread(repo.upsert_tenders, buffer)
            total_upserted_new += n
            total_upserted_upd += u
            log.info("[%s] flushed %d (cum new=%d upd=%d)", shard_label, len(buffer),
                     total_upserted_new, total_upserted_upd)
            buffer.clear()

        if consecutive_zero >= SHARD_SATURATION_THRESHOLD:
            log.info("[%s] saturated at %d consec 0-new pages, unique=%d, stopping",
                     shard_label, consecutive_zero, new_ids_in_shard)
            break

    # final flush
    if buffer:
        n, u = await asyncio.to_thread(repo.upsert_tenders, buffer)
        total_upserted_new += n
        total_upserted_upd += u

    log.info("[%s] DONE pages=%d unique_in_shard=%d new=%d upd=%d api_claimed=%s",
             shard_label, pages_walked, new_ids_in_shard,
             total_upserted_new, total_upserted_upd, api_total)
    return {
        "label": shard_label, "pages": pages_walked, "unique": new_ids_in_shard,
        "new": total_upserted_new, "updated": total_upserted_upd,
        "api_claimed": api_total,
    }


async def run_listing(repo: Repo, run_id: int):
    shards = list(shard_generator(SHARD_STRATEGY))
    if LIMIT_SHARDS:
        shards = shards[:LIMIT_SHARDS]
    log.info("LISTING phase: strategy=%s shards=%d concurrency=%d",
             SHARD_STRATEGY, len(shards), SHARD_CONCURRENCY)

    state = load_checkpoint()
    state.setdefault("completed_shards", [])
    state.setdefault("shard_results", {})

    sem = asyncio.Semaphore(SHARD_CONCURRENCY)
    seen_global: set[str] = set()
    # Counts for THIS run only (skipped/checkpointed shards don't contribute) —
    # these get written back to scrape_runs so the row isn't all-zero.
    run_counts = {"found": 0, "new": 0, "updated": 0, "pages": 0}

    async def runner(label, params):
        if label in state["completed_shards"]:
            log.info("[%s] already completed (checkpoint), skipping", label)
            return
        async with sem:
            try:
                result = await scrape_shard(label, params, repo, seen_global)
                state["shard_results"][label] = result
                state["completed_shards"].append(label)
                run_counts["found"] += result["unique"]
                run_counts["new"] += result["new"]
                run_counts["updated"] += result["updated"]
                run_counts["pages"] += result["pages"]
                save_checkpoint(state)
            except Exception:
                log.exception("[%s] shard failed", label)

    await asyncio.gather(*(runner(lbl, prm) for lbl, prm in shards))

    total_unique = len(seen_global)
    log.info("LISTING done: unique_ids=%d new_to_db=%d updated=%d pages=%d",
             total_unique, run_counts["new"], run_counts["updated"], run_counts["pages"])
    return run_counts


# ---------- DETAILS phase ----------
DETAIL_WARMUP = os.environ.get("DETAIL_WARMUP", "false").strip().lower() not in ("false", "0", "no", "")
AWARD_WARMUP = os.environ.get("AWARD_WARMUP", "false").strip().lower() not in ("false", "0", "no", "")


async def fetch_one_detail(tid: str, detail_url: str, base_raw: dict | None) -> dict | None:
    """Fetch the detail page (+ relations, best-effort) for one tender.

    The per-tender warmup GET to AllTendersForVisitor was REMOVED by default
    (DETAIL_WARMUP=false): a 2026-06-03 smoke test showed DetailsForVisitor
    returns 200 without it, while the warmup endpoint is Etimad's most
    aggressively rate-limited route — hitting it once per tender triggered a
    429 cascade that also starved the relations endpoint. Dropping it cuts
    request volume by a third and keeps the detail fetch clean. Set
    DETAIL_WARMUP=true to restore the old behavior if a future WAF change
    makes cookies mandatory again.
    """
    label = f"detail {tid[:16]}"
    ua = random.choice(USER_AGENTS)
    for attempt in range(MAX_RETRIES):
        proxy = proxy_for_request()
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT,
                follow_redirects=True,
                proxy=proxy,
                headers={"User-Agent": ua,
                         "Accept-Language": "ar,en-US;q=0.9,en;q=0.8"},
            ) as client:
                # Warmup (opt-in via DETAIL_WARMUP=true): hit the listing page to
                # establish session cookies. Off by default — it 429-cascades.
                if DETAIL_WARMUP:
                    await client.get(
                        LISTING_REFERER,
                        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                    )
                # Detail
                rd = await client.get(
                    detail_url,
                    headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                             "Referer": LISTING_REFERER},
                )
                final_host = (rd.url.host or "") if rd.url else ""
                if "login.etimad.sa" in final_host:
                    log.warning("[%s] auth-walled (redirected to login) attempt=%d/%d",
                                label, attempt + 1, MAX_RETRIES)
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if rd.status_code != 200 or not rd.text:
                    log.warning("[%s] HTTP %d attempt=%d/%d",
                                label, rd.status_code, attempt + 1, MAX_RETRIES)
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                fields = parse_detail_page(rd.text)
                # Canary: a real detail page always has tender_name. Empty/missing means
                # we got a login page, an error page, or the parser couldn't read it.
                # Skip rather than upsert a row with all-NULL non-id columns.
                if not fields.get("tender_name"):
                    log.warning("[%s] parsed but no tender_name — skipping (likely non-detail HTML)",
                                label)
                    return None
                # Relations (best-effort; failure is non-fatal)
                raw = dict(base_raw or {})
                try:
                    rr = await client.get(
                        RELATIONS_URL,
                        params={"tenderIdStr": tid, "_x_requested_with": "XMLHttpRequest"},
                        headers={"Accept": "text/html,*/*",
                                 "Referer": detail_url,
                                 "X-Requested-With": "XMLHttpRequest"},
                    )
                    rr_host = (rr.url.host or "") if rr.url else ""
                    if rr.status_code == 200 and rr.text and "login.etimad.sa" not in rr_host:
                        rel = parse_relations_html(rr.text)
                        rel_raw = rel.pop("_raw", None)
                        if rel.get("place"):
                            fields["place"] = rel["place"]
                        if rel_raw:
                            raw["relations"] = rel_raw
                    else:
                        log.warning("[rel %s] HTTP %d — proceeding without relations",
                                    tid[:16], rr.status_code)
                except httpx.RequestError as e:
                    log.warning("[rel %s] net error: %s", tid[:16], e)
                row = {"etimad_tender_id": tid, "raw_data": raw,
                       "scraped_at": datetime.now(timezone.utc).isoformat()}
                row.update({k: v for k, v in fields.items() if v is not None})
                return row
        except httpx.RequestError as e:
            log.warning("[%s] net error attempt=%d/%d: %s",
                        label, attempt + 1, MAX_RETRIES, e)
        await asyncio.sleep(2 * (attempt + 1))
    return None


async def run_details(repo: Repo):
    log.info("DETAILS phase: fetching ids needing details from Supabase…")
    ids = await asyncio.to_thread(repo.get_ids_needing_details, LIMIT_DETAILS)
    log.info("DETAILS: %d tenders need details (limit=%d)", len(ids), LIMIT_DETAILS or 0)
    if not ids:
        return {"done": 0, "failed": 0}
    skeletons = await asyncio.to_thread(repo.fetch_tender_skeletons, ids)
    log.info("DETAILS: loaded %d skeletons", len(skeletons))

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    buffer: list[dict] = []
    buffer_lock = asyncio.Lock()
    done = 0
    failed = 0

    async def flush_if_full(force=False):
        nonlocal buffer
        async with buffer_lock:
            if not buffer:
                return
            if force or len(buffer) >= UPSERT_BATCH_SIZE:
                batch = buffer
                buffer = []
                await asyncio.to_thread(repo.upsert_tenders, batch)
                log.info("DETAILS: flushed %d (done=%d failed=%d)", len(batch), done, failed)

    async def worker(tid):
        nonlocal done, failed
        async with sem:
            skel = skeletons.get(tid)
            if not skel or not skel.get("detail_url"):
                failed += 1
                return
            row = await fetch_one_detail(tid, skel["detail_url"], skel.get("raw_data"))
            if not row:
                failed += 1
                return
            async with buffer_lock:
                buffer.append(row)
            done += 1
            if done % 100 == 0:
                log.info("DETAILS progress: done=%d failed=%d remaining=%d",
                         done, failed, len(ids) - done - failed)
        await flush_if_full()

    await asyncio.gather(*(worker(tid) for tid in ids))
    await flush_if_full(force=True)
    log.info("DETAILS done: %d fetched, %d failed", done, failed)
    return {"done": done, "failed": failed}


# ---------- AWARDS phase ----------
async def fetch_one_award(tid: str) -> list[dict] | None:
    """Fetch all groups + per-group results for one tender. Returns a list of
    award rows (each carrying etimad_tender_id, group_id, group_name, bidder_name,
    offer_value, tech_evaluation, award_value, role) ready for upsert.

    Returns:
        - [] if the tender has no groups (rare; usually means awarding not yet
          published despite the status). Treated as success — nothing to insert.
        - None on hard failure (will be retried by caller's outer loop).
    """
    label = f"award {tid[:16]}"
    ua = random.choice(USER_AGENTS)
    detail_referer = f"{BASE_URL}/Tender/DetailsForVisitor?STenderId={quote(tid, safe='')}"
    for attempt in range(MAX_RETRIES):
        proxy = proxy_for_request()
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT,
                follow_redirects=True,
                proxy=proxy,
                headers={"User-Agent": ua,
                         "Accept-Language": "ar,en-US;q=0.9,en;q=0.8"},
            ) as client:
                # Warmup (opt-in via AWARD_WARMUP=true): off by default — like the
                # details path, hitting AllTendersForVisitor once per tender
                # 429-storms and starves the awarding endpoints.
                if AWARD_WARMUP:
                    await client.get(
                        LISTING_REFERER,
                        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                    )
                # Groups
                rg = await client.get(
                    AWARDING_GROUPS_URL,
                    params={"tenderIdStr": tid},
                    headers={"Accept": "text/html,*/*",
                             "Referer": detail_referer,
                             "X-Requested-With": "XMLHttpRequest"},
                )
                if rg.status_code != 200 or not rg.text:
                    log.warning("[%s] groups HTTP %d attempt=%d/%d",
                                label, rg.status_code, attempt + 1, MAX_RETRIES)
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                groups = parse_awarding_groups(rg.text)
                if not groups:
                    # Empty group list means awarding not announced — nothing to scrape.
                    # Return [] (success, no rows) so caller doesn't retry endlessly.
                    return []
                # Per-group results — each group call has its own retry loop on
                # 429/5xx, otherwise rate-limit storms drop bidder data silently
                # and show up as fake "empty awarding" tenders.
                rows: list[dict] = []
                all_groups_ok = True
                for gid, gname in groups:
                    group_ok = False
                    for g_attempt in range(MAX_RETRIES):
                        # Respect Etimad's "max 10 calls/min" on the Results
                        # endpoint. This global limiter is THE mechanism that
                        # prevents false-empties; without the acquire() the
                        # limiter object is dead code (was the case before
                        # 2026-06-03). Shared across all AWARD_CONCURRENCY workers.
                        await RESULTS_RATE_LIMITER.acquire()
                        rr = await client.get(
                            AWARDING_RESULTS_URL,
                            params={"tenderIdStr": tid, "groupId": gid},
                            headers={"Accept": "text/html,*/*",
                                     "Referer": detail_referer,
                                     "X-Requested-With": "XMLHttpRequest"},
                        )
                        if rr.status_code == 200 and rr.text:
                            parsed = parse_awarding_results(rr.text, gid, gname)
                            for row in parsed:
                                row["etimad_tender_id"] = tid
                                row["scraped_at"] = datetime.now(timezone.utc).isoformat()
                                rows.append(row)
                            group_ok = True
                            break
                        log.warning("[%s g%d] results HTTP %d attempt=%d/%d",
                                    label, gid, rr.status_code, g_attempt + 1, MAX_RETRIES)
                        await asyncio.sleep(2 * (g_attempt + 1))
                    if not group_ok:
                        all_groups_ok = False
                if not all_groups_ok:
                    # At least one group still failed after retries — bubble up so
                    # the tender is counted as failed and retried later.
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return rows
        except httpx.RequestError as e:
            log.warning("[%s] net error attempt=%d/%d: %s",
                        label, attempt + 1, MAX_RETRIES, e)
        await asyncio.sleep(2 * (attempt + 1))
    return None


async def run_awards(repo: Repo):
    log.info("AWARDS phase: fetching ids needing awards from Supabase…")
    ids = await asyncio.to_thread(repo.get_ids_needing_awards, LIMIT_AWARDS)
    log.info("AWARDS: %d tenders need awards (limit=%d)", len(ids), LIMIT_AWARDS or 0)
    if not ids:
        return {"done": 0, "empty": 0, "failed": 0}

    sem = asyncio.Semaphore(AWARD_CONCURRENCY)
    buffer: list[dict] = []
    buffer_lock = asyncio.Lock()
    # Tenders we successfully checked (empty OR with rows) — stamped with
    # awards_last_checked so the recurring job skips known-empties next time.
    checked_ids: list[str] = []
    checked_lock = asyncio.Lock()
    done = 0       # fetched (incl. empty-group successes)
    empty = 0      # got 0 rows (awarding not published)
    failed = 0     # gave up after MAX_RETRIES

    async def flush_if_full(force=False):
        nonlocal buffer
        async with buffer_lock:
            if not buffer:
                return
            if force or len(buffer) >= UPSERT_BATCH_SIZE:
                batch = buffer
                buffer = []
                await asyncio.to_thread(repo.upsert_awards, batch)
                log.info("AWARDS: flushed %d rows (done=%d empty=%d failed=%d)",
                         len(batch), done, empty, failed)

    async def flush_checked(force=False):
        nonlocal checked_ids
        async with checked_lock:
            if not checked_ids:
                return
            if force or len(checked_ids) >= 500:
                batch = checked_ids
                checked_ids = []
                await asyncio.to_thread(repo.mark_awards_checked, batch)

    async def worker(tid):
        nonlocal done, empty, failed
        async with sem:
            rows = await fetch_one_award(tid)
            if rows is None:
                failed += 1
                return
            if not rows:
                empty += 1
            else:
                async with buffer_lock:
                    buffer.extend(rows)
            async with checked_lock:
                checked_ids.append(tid)
            done += 1
            if done % 100 == 0:
                log.info("AWARDS progress: done=%d empty=%d failed=%d remaining=%d",
                         done, empty, failed, len(ids) - done - failed)
        await flush_if_full()
        await flush_checked()

    await asyncio.gather(*(worker(tid) for tid in ids))
    await flush_if_full(force=True)
    await flush_checked(force=True)
    log.info("AWARDS done: %d fetched (%d empty), %d failed", done, empty, failed)
    return {"done": done, "empty": empty, "failed": failed}


# ---------- Main ----------
async def main_async():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key):
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY")

    repo = Repo(url, key)
    run_id = await asyncio.to_thread(repo.start_run)
    reaped = await asyncio.to_thread(repo.reap_stale_runs, run_id)
    if reaped:
        log.info("Reaped %d stale 'running' run(s) from prior crashes: %s", len(reaped), reaped)
    log.info("Started parallel run #%d  proxy=%s  strategy=%s  mode=%s",
             run_id, bool(USE_PROXY and BRIGHTDATA_PROXY_URL), SHARD_STRATEGY, MODE)

    error_msg = None
    counters: dict = {}
    try:
        if MODE in ("listing", "both"):
            lc = await run_listing(repo, run_id)
            counters.update(tenders_found=lc["found"], tenders_new=lc["new"],
                            tenders_updated=lc["updated"], pages_scraped=lc["pages"])
        if MODE in ("details", "both"):
            dc = await run_details(repo)
            # details enriches existing rows — count fetched as 'updated'
            counters["tenders_updated"] = counters.get("tenders_updated", 0) + dc["done"]
        if MODE == "awards":
            ac = await run_awards(repo)
            # no awards-specific column; record enriched tenders as 'updated'
            counters["tenders_updated"] = ac["done"]
        await asyncio.to_thread(repo.finish_run, run_id, status="success", **counters)
    except Exception as e:
        log.exception("Run failed")
        error_msg = str(e)[:1000]
        await asyncio.to_thread(repo.finish_run, run_id, status="failed",
                                error_message=error_msg, **counters)
        raise


if __name__ == "__main__":
    asyncio.run(main_async())
