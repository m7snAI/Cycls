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
import logging
from datetime import datetime, timezone
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

PAGE_SIZE = 24
PUBLISH_DATE_ID = int(os.environ.get("PUBLISH_DATE_ID", "0"))  # 0=all-time

BRIGHTDATA_PROXY_URL = os.environ.get("BRIGHTDATA_PROXY_URL", "").strip()
SHARD_STRATEGY = os.environ.get("SHARD_STRATEGY", "type_sort")
MODE = os.environ.get("MODE", "both")  # listing | details | both

SHARD_CONCURRENCY = int(os.environ.get("SHARD_CONCURRENCY", "10"))
DETAIL_CONCURRENCY = int(os.environ.get("DETAIL_CONCURRENCY", "50"))

LIMIT_SHARDS = int(os.environ.get("LIMIT_SHARDS", "0"))
LIMIT_PAGES_PER_SHARD = int(os.environ.get("LIMIT_PAGES_PER_SHARD", "0"))
LIMIT_DETAILS = int(os.environ.get("LIMIT_DETAILS", "0"))

SHARD_SATURATION_THRESHOLD = int(os.environ.get("SHARD_SATURATION_THRESHOLD", "20"))
MAX_PAGES_PER_SHARD = int(os.environ.get("MAX_PAGES_PER_SHARD", "3000"))

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


# ---------- Proxy ----------
def proxy_for_request() -> str | None:
    """Return a proxy URL with {session} substituted (or the URL as-is)."""
    if not BRIGHTDATA_PROXY_URL:
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
                if r.status_code == 200 and r.text:
                    return r.text
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
        for i in range(0, len(tenders), BATCH):
            self.c.table("tenders").upsert(
                tenders[i:i + BATCH], on_conflict="etimad_tender_id"
            ).execute()
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
    pages_walked = 0
    new_ids_in_shard = 0
    buffer: list[dict] = []
    total_upserted_new = 0
    total_upserted_upd = 0
    api_total = None

    max_pages = LIMIT_PAGES_PER_SHARD or MAX_PAGES_PER_SHARD
    for page in range(1, max_pages + 1):
        params = {"PageNumber": page, "PageSize": PAGE_SIZE,
                  "PublishDateId": PUBLISH_DATE_ID, **shard_params}
        resp = await fetch_json(params, attempt_label=f"{shard_label} p{page}")
        if not resp:
            log.warning("[%s] page %d: failed, skipping", shard_label, page)
            continue
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

    async def runner(label, params):
        if label in state["completed_shards"]:
            log.info("[%s] already completed (checkpoint), skipping", label)
            return
        async with sem:
            try:
                result = await scrape_shard(label, params, repo, seen_global)
                state["shard_results"][label] = result
                state["completed_shards"].append(label)
                save_checkpoint(state)
            except Exception:
                log.exception("[%s] shard failed", label)

    await asyncio.gather(*(runner(lbl, prm) for lbl, prm in shards))

    total_unique = len(seen_global)
    total_new = sum(r["new"] for r in state["shard_results"].values())
    log.info("LISTING done: unique_ids=%d total_new_to_db=%d", total_unique, total_new)


# ---------- DETAILS phase ----------
async def fetch_one_detail(tid: str, detail_url: str, base_raw: dict | None) -> dict | None:
    label = f"detail {tid[:16]}"
    html = await fetch_html(detail_url, referer=LISTING_REFERER, attempt_label=label)
    if not html:
        return None
    fields = parse_detail_page(html)
    relations_html = await fetch_html(
        RELATIONS_URL,
        params={"tenderIdStr": tid, "_x_requested_with": "XMLHttpRequest"},
        referer=detail_url,
        attempt_label=f"rel {tid[:16]}",
    )
    raw = dict(base_raw or {})
    if relations_html:
        rel = parse_relations_html(relations_html)
        rel_raw = rel.pop("_raw", None)
        if rel.get("place"):
            fields["place"] = rel["place"]
        if rel_raw:
            raw["relations"] = rel_raw
    row = {"etimad_tender_id": tid, "raw_data": raw,
           "scraped_at": datetime.now(timezone.utc).isoformat()}
    row.update({k: v for k, v in fields.items() if v is not None})
    return row


async def run_details(repo: Repo):
    log.info("DETAILS phase: fetching ids needing details from Supabase…")
    ids = await asyncio.to_thread(repo.get_ids_needing_details, LIMIT_DETAILS)
    log.info("DETAILS: %d tenders need details (limit=%d)", len(ids), LIMIT_DETAILS or 0)
    if not ids:
        return
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


# ---------- Main ----------
async def main_async():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key):
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY")

    repo = Repo(url, key)
    run_id = await asyncio.to_thread(repo.start_run)
    log.info("Started parallel run #%d  proxy=%s  strategy=%s  mode=%s",
             run_id, bool(BRIGHTDATA_PROXY_URL), SHARD_STRATEGY, MODE)

    error_msg = None
    try:
        if MODE in ("listing", "both"):
            await run_listing(repo, run_id)
        if MODE in ("details", "both"):
            await run_details(repo)
        await asyncio.to_thread(repo.finish_run, run_id, status="success")
    except Exception as e:
        log.exception("Run failed")
        error_msg = str(e)[:1000]
        await asyncio.to_thread(repo.finish_run, run_id, status="failed", error_message=error_msg)
        raise


if __name__ == "__main__":
    asyncio.run(main_async())
