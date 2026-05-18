"""
Probe Etimad's listing pagination cap for PublishDateId=0 (all-time, 283k claimed).

We already know PublishDateId=5 (active, 9256 claimed) caps at ~5012 unique IDs after
~838 pages — the API keeps returning duplicates. This probe answers:

  1. Does PublishDateId=0 also cap, and at what unique-ID count?
  2. Does sorting ASC by submission date surface a *different* set of IDs
     (which would let us slice past the cap)?
  3. Sanity: what totalCount does the API claim per tender type?

Two paginated passes (default sort, then SortBySubmissionDate ASC) plus a quick
filter-totals sample. Stops a pass early after SATURATION_THRESHOLD consecutive
0-new pages.

Output: probe-summary.json (small) + probe.log (full trace).
No Supabase writes.
"""
import os
import json
import time
import random
import logging

import httpx

BASE = "https://tenders.etimad.sa"
LISTING = f"{BASE}/Tender/AllSupplierTendersForVisitorAsync"
REFERER = f"{BASE}/Tender/AllTendersForVisitor"

PAGE_SIZE = 24
PUBLISH_DATE_ID = 0
MAX_PAGES = int(os.environ.get("MAX_PAGES", "2000"))
SATURATION_THRESHOLD = int(os.environ.get("SATURATION_THRESHOLD", "50"))
REQUEST_DELAY_MIN = float(os.environ.get("REQUEST_DELAY_MIN", "4"))
REQUEST_DELAY_MAX = float(os.environ.get("REQUEST_DELAY_MAX", "6"))
MAX_RETRIES = 3
TIMEOUT = 30

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("probe")


def get_json(client: httpx.Client, params: dict):
    for attempt in range(MAX_RETRIES):
        try:
            r = client.get(
                LISTING,
                params=params,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": REFERER,
                    "User-Agent": UA,
                },
            )
            ctype = r.headers.get("content-type") or ""
            if r.status_code == 200 and "json" in ctype:
                return r.json()
            wait = (attempt + 1) * 10
            log.warning("Transient (%d, %s). Waiting %ds…", r.status_code, ctype, wait)
            time.sleep(wait)
        except httpx.RequestError as e:
            log.warning("Net error (attempt %d, %s): %s", attempt + 1, type(e).__name__, e)
            time.sleep(5 * (attempt + 1))
    return None


def polite_sleep():
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def walk(client: httpx.Client, label: str, extra_params: dict | None = None):
    seen: set[str] = set()
    consecutive_zero = 0
    first_saturation_page = None
    total_count_reported = None
    pages_walked = 0

    for page in range(1, MAX_PAGES + 1):
        params = {"PageNumber": page, "PageSize": PAGE_SIZE, "PublishDateId": PUBLISH_DATE_ID}
        if extra_params:
            params.update(extra_params)

        resp = get_json(client, params)
        if not resp:
            log.warning("[%s] page %d: fetch failed, skipping", label, page)
            polite_sleep()
            continue

        pages_walked += 1
        items = resp.get("data") or []
        if total_count_reported is None:
            total_count_reported = resp.get("totalCount") or 0
            log.info("[%s] totalCount reported by API: %d", label, total_count_reported)

        if not items:
            log.info("[%s] page %d: empty response, stopping", label, page)
            break

        before = len(seen)
        for it in items:
            tid = it.get("tenderIdString")
            if tid:
                seen.add(tid)
        new = len(seen) - before

        if new == 0:
            consecutive_zero += 1
            if first_saturation_page is None and consecutive_zero >= 10:
                first_saturation_page = page - 9  # the page where the run-of-zeros began
        else:
            consecutive_zero = 0

        if page % 25 == 0 or new < len(items):
            log.info("[%s] page %d: items=%d new=%d unique=%d consec_zero=%d",
                     label, page, len(items), new, len(seen), consecutive_zero)

        if consecutive_zero >= SATURATION_THRESHOLD:
            log.info("[%s] saturation: %d consecutive 0-new pages → stopping at unique=%d (first plateau page ≈ %s)",
                     label, consecutive_zero, len(seen), first_saturation_page)
            break

        polite_sleep()
    else:
        log.info("[%s] hit MAX_PAGES=%d without saturating (unique=%d)", label, MAX_PAGES, len(seen))

    return {
        "label": label,
        "total_count_reported": total_count_reported,
        "unique_ids": len(seen),
        "pages_walked": pages_walked,
        "first_saturation_page": first_saturation_page,
        "saturated": consecutive_zero >= SATURATION_THRESHOLD,
        "_ids": seen,
    }


def sample_filter_totals(client: httpx.Client):
    """Hit page 1 with a few filter combos to see how the API partitions 283k by tender type.
    Helps estimate whether sharding by TenderTypeId can keep each shard under the cap."""
    out = {}
    for type_id in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15]:
        resp = get_json(client, {
            "PageNumber": 1, "PageSize": PAGE_SIZE,
            "PublishDateId": PUBLISH_DATE_ID,
            "TenderTypeId": type_id, "IsSearch": "true",
        })
        if resp:
            out[f"TenderTypeId={type_id}"] = {
                "total_count_reported": resp.get("totalCount"),
                "first_item_type_name": ((resp.get("data") or [{}])[0]).get("tenderTypeName"),
            }
            log.info("filter TenderTypeId=%d → totalCount=%s (%s)",
                     type_id, resp.get("totalCount"),
                     ((resp.get("data") or [{}])[0]).get("tenderTypeName"))
        polite_sleep()
    return out


def main():
    client = httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    try:
        log.info("=== Pass 1: PublishDateId=0, default sort ===")
        default = walk(client, "default")

        log.info("=== Pass 2: PublishDateId=0, SortBySubmissionDate ASC ===")
        asc = walk(client, "asc-submission", extra_params={
            "Sort": "SortBySubmissionDate", "SortDirection": "ASC", "IsSearch": "true",
        })

        log.info("=== Filter totals sample (TenderTypeId 1..10, 15) ===")
        type_totals = sample_filter_totals(client)

        default_ids = default.pop("_ids")
        asc_ids = asc.pop("_ids")
        overlap = len(default_ids & asc_ids)
        only_default = len(default_ids - asc_ids)
        only_asc = len(asc_ids - default_ids)
        combined = len(default_ids | asc_ids)

        total_claimed = default.get("total_count_reported") or 0
        coverage_pct = (100.0 * combined / total_claimed) if total_claimed else None

        summary = {
            "page_size": PAGE_SIZE,
            "publish_date_id": PUBLISH_DATE_ID,
            "max_pages_per_pass": MAX_PAGES,
            "saturation_threshold": SATURATION_THRESHOLD,
            "default": default,
            "asc_submission": asc,
            "overlap_between_passes": overlap,
            "only_in_default": only_default,
            "only_in_asc": only_asc,
            "combined_unique": combined,
            "combined_coverage_pct_of_claimed": coverage_pct,
            "filter_type_totals": type_totals,
            "interpretation": {
                "cap_observed": default.get("saturated") or asc.get("saturated"),
                "sort_unlocks_new_ids": only_asc > 100,
                "two_passes_get_all": (
                    bool(total_claimed) and combined >= int(total_claimed * 0.95)
                ),
            },
        }

        with open("probe-summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        log.info("Summary saved → probe-summary.json")

        log.info("=" * 60)
        log.info("RESULTS:")
        log.info("  claimed total (PublishDateId=0): %s", total_claimed)
        log.info("  default-sort unique: %d (saturated=%s, first plateau page=%s)",
                 default["unique_ids"], default["saturated"], default["first_saturation_page"])
        log.info("  asc-sort unique:     %d (saturated=%s, first plateau page=%s)",
                 asc["unique_ids"], asc["saturated"], asc["first_saturation_page"])
        log.info("  overlap: %d   only-default: %d   only-asc: %d",
                 overlap, only_default, only_asc)
        log.info("  combined (2 passes): %d  →  %.1f%% of claimed",
                 combined, coverage_pct or 0.0)
        log.info("=" * 60)

    finally:
        client.close()


if __name__ == "__main__":
    main()
