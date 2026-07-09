"""
uqn/scrape_news.py
Scrapes uqn.gov.sa/news using Playwright (Vue.js SPA — requires JS execution).

Usage:
    python uqn/scrape_news.py                          # full run
    python uqn/scrape_news.py --limit 5                # first 5 articles
    python uqn/scrape_news.py --dry-run --limit 5      # no DB write
    python uqn/scrape_news.py --category crown-prince  # one category only

Install deps (first time):
    pip install playwright supabase python-dotenv
    playwright install chromium
"""

import argparse
import hashlib
import time
import sys
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL         = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

BASE_URL = "https://www.uqn.gov.sa"

CATEGORY_SLUGS = {
    "custodian":             "خادم الحرمين الشريفين",
    "crown-prince":          "ولي العهد",
    "council-of-ministers":  "مجلس الوزراء",
    "royal-orders":          "أوامر ملكية",
    "statements":            "بيانات رسمية",
    "reports":               "تقارير",
    "royal-decrees":                      "أوامر ملكية",
    "custodian-of-the-two-holy-mosques":  "خادم الحرمين الشريفين",
    "official-statements":                "بيانات رسمية",
}

# Noise lines to strip from full_text_ar
NOISE_PATTERNS = [
    r"^أخبار$",
    r"^/$",
    r"^Facebook$",
    r"^X$",
    r"^WhatsApp$",
    r"^نسخ الرابط$",
    r"^تم نسخ الرابط$",
    r"^طباعة$",
    r"^جميع الحقوق محفوظة.*",
    r"^\{\{.*\}\}$",          # any leftover Vue template literals
    r"^\s*الموافق\s*$",
    r"^\s*الساعة\s*$",
]
NOISE_RE = re.compile("|".join(NOISE_PATTERNS))

HIJRI_RE    = re.compile(r"\d{4}-\d{1,2}-\d{1,2}\s*الموافق")
GREGORIAN_RE = re.compile(r"\d{2}-\d{2}-\d{4}")       # e.g. 09-07-2026


# ── helpers ──────────────────────────────────────────────────────────────────

def make_stable_id(title: str, date_hijri: str) -> str:
    return hashlib.sha1(f"{title}|{date_hijri}".encode()).hexdigest()


def clean_text(raw: str) -> str:
    """Remove noise lines from extracted text."""
    lines = raw.splitlines()
    cleaned = [l for l in lines if l.strip() and not NOISE_RE.match(l.strip())]
    return "\n".join(cleaned).strip()


def parse_gregorian(raw: str) -> str | None:
    """Convert DD-MM-YYYY → YYYY-MM-DD for Supabase date column."""
    m = GREGORIAN_RE.search(raw)
    if not m:
        return None
    parts = m.group().split("-")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return None


# ── browser session ───────────────────────────────────────────────────────────

def make_browser():
    from playwright.sync_api import sync_playwright
    pw      = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        locale="ar-SA",
        extra_http_headers={"Accept-Language": "ar,en;q=0.9"},
    )
    page = context.new_page()
    # Block images/fonts to speed up scraping
    page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf}", lambda r: r.abort())
    return pw, browser, page


# ── listing ───────────────────────────────────────────────────────────────────

def get_article_urls(page, category_slug: str | None = None, limit: int | None = None) -> list[str]:
    urls    = []
    pg_num  = 1

    while True:
        if category_slug:
            listing_url = f"{BASE_URL}/news/{category_slug}?pgno={pg_num}"
        else:
            listing_url = f"{BASE_URL}/News?pgno={pg_num}"

        try:
            page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  [WARN] Timeout on listing page {pg_num}: {e}")
            break

        # Wait for an actual article card to render (has numeric ID in href)
        try:
            page.wait_for_function(
                """() => {
                    const links = [...document.querySelectorAll('a[href*="/news/"]')];
                    return links.some(a => /\\/\\d+$/.test(a.getAttribute('href')));
                }""",
                timeout=15000
            )
        except Exception:
            print(f"  [WARN] Cards didn't render on page {pg_num}")
            break

        # Collect links with numeric IDs at the end
        anchors = page.query_selector_all("a[href*='/news/']")
        page_urls = []
        for a in anchors:
            href = a.get_attribute("href") or ""
            parts = href.rstrip("/").split("/")
            if parts and parts[-1].isdigit():
                full = BASE_URL + href if href.startswith("/") else href
                if full not in urls and full not in page_urls:
                    page_urls.append(full)

        if not page_urls:
            print(f"  [INFO] No new URLs at page {pg_num}, stopping.")
            break

        urls.extend(page_urls)
        print(f"  Page {pg_num}: +{len(page_urls)} URLs (total {len(urls)})")

        if limit and len(urls) >= limit:
            urls = urls[:limit]
            break

        # Check for next page
        next_btn = page.query_selector("a[rel='next']")
        if not next_btn:
            break

        pg_num += 1
        time.sleep(0.5)

    return urls


# ── article parsing ───────────────────────────────────────────────────────────

def parse_article(page, url: str) -> dict | None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"    [TIMEOUT] {url}: {e}")
        return None   # will be counted as failed

    # Wait for h1
    try:
        page.wait_for_selector("h1", timeout=8000)
    except Exception:
        return None   # no content

    # Title
    h1_el    = page.query_selector("h1")
    title_ar = h1_el.inner_text().strip() if h1_el else None
    if not title_ar:
        return None

    # Main article body — after Vue renders, article_body_clean is in the DOM
    article_el = (
        page.query_selector("article") or
        page.query_selector(".article-content") or
        page.query_selector(".news-content") or
        page.query_selector("main")
    )
    raw_text    = article_el.inner_text() if article_el else ""
    full_text_ar = clean_text(raw_text) if raw_text else None

    # Dates — look for rendered hijri/gregorian in the DOM
    date_hijri     = None
    date_gregorian = None

    # Try meta first
    meta_pub = page.query_selector("meta[property='article:published_time'], meta[name='datePublished']")
    if meta_pub:
        content = meta_pub.get_attribute("content") or ""
        date_gregorian = content.split("T")[0] or None

    # Scan all text nodes for hijri (contains هـ) and gregorian (DD-MM-YYYY)
    all_text = page.inner_text("body")
    for line in all_text.splitlines():
        line = line.strip()
        if not date_hijri and HIJRI_RE.search(line):
            date_hijri = line.split("الموافق")[0].strip()   # extract only the hijri part
        if not date_gregorian:
            g = parse_gregorian(line)
            if g:
                date_gregorian = g

    # Category from URL
    category_slug = None
    category      = None
    path_parts    = url.replace(BASE_URL, "").strip("/").split("/")
    if len(path_parts) >= 3 and path_parts[1] in CATEGORY_SLUGS:
        category_slug = path_parts[1]
        category      = CATEGORY_SLUGS[category_slug]
    else:
        for a in page.query_selector_all("a.breadcrumbs-title"):
            title_attr = a.get_attribute("title") or ""
            text = a.inner_text().strip()
            for slug, label in CATEGORY_SLUGS.items():
                if label == title_attr or label == text:
                    category_slug = slug
                    category      = label
                    break

    # Image
    og_img    = page.query_selector("meta[property='og:image']")
    image_url = og_img.get_attribute("content") if og_img else None

    # Tags — rendered keyword links
    tag_els = page.query_selector_all(".tags a, .article-tags a, [class*='tag'] a")
    tags_ar = [t.inner_text().strip() for t in tag_els if t.inner_text().strip()] or None

    return {
        "stable_id":      make_stable_id(title_ar, date_hijri or ""),
        "source_url":     url,
        "title_ar":       title_ar,
        "full_text_ar":   full_text_ar,
        "date_hijri":     date_hijri,
        "date_gregorian": date_gregorian,
        "category":       category,
        "category_slug":  category_slug,
        "image_url":      image_url,
        "tags_ar":        tags_ar,
        "qa_decision":    "keep",
        "updated_at":     datetime.now(timezone.utc).isoformat(),
    }


# ── deduplication ─────────────────────────────────────────────────────────────

def load_existing_urls(supabase) -> set[str]:
    existing = set()
    offset   = 0
    while True:
        res   = supabase.table("uqn_news").select("source_url").range(offset, offset + 999).execute()
        batch = [r["source_url"] for r in res.data]
        existing.update(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return existing


# ── upsert ────────────────────────────────────────────────────────────────────

def upsert_batch(supabase, records: list[dict]) -> tuple[int, int]:
    if not records:
        return 0, 0
    try:
        supabase.table("uqn_news").upsert(records, on_conflict="stable_id").execute()
        return len(records), 0
    except Exception as e:
        if len(records) == 1:
            print(f"    [ERROR] {records[0]['source_url']}: {e}")
            return 0, 1
        mid = len(records) // 2
        s1, f1 = upsert_batch(supabase, records[:mid])
        s2, f2 = upsert_batch(supabase, records[mid:])
        return s1 + s2, f1 + f2


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",    type=int, default=None)
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--category", type=str, default=None,
                        help=f"One of: {', '.join(CATEGORY_SLUGS)}")
    args = parser.parse_args()

    if args.category and args.category not in CATEGORY_SLUGS:
        print(f"Unknown category '{args.category}'. Valid: {', '.join(CATEGORY_SLUGS)}")
        sys.exit(1)

    supabase = None
    if not args.dry_run:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            print("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")
            sys.exit(1)
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    print("=" * 60)
    print("UQN News Scraper (Playwright)")
    print(f"  dry_run  : {args.dry_run}")
    print(f"  limit    : {args.limit or 'all'}")
    print(f"  category : {args.category or 'all'}")
    print("=" * 60)

    pw, browser, page = make_browser()

    try:
        # 1. Collect URLs
        print("\n[1/3] Collecting article URLs...")
        article_urls = get_article_urls(page, category_slug=args.category, limit=args.limit)
        print(f"  → {len(article_urls)} URLs collected")

        # 2. Deduplicate
        skipped_existing = 0
        if supabase:
            print("\n[2/3] Loading existing URLs from DB...")
            existing         = load_existing_urls(supabase)
            before           = len(article_urls)
            article_urls     = [u for u in article_urls if u not in existing]
            skipped_existing = before - len(article_urls)
            print(f"  → {skipped_existing} already in DB, {len(article_urls)} new")
        else:
            print("\n[2/3] Skipping dedup (dry-run)")

        # 3. Scrape + upsert
        print(f"\n[3/3] Scraping {len(article_urls)} articles...")
        BATCH_SIZE         = 20
        batch              = []
        total_success      = 0
        total_failed       = 0
        skipped_no_content = 0
        failed_urls        = []

        skipped_urls_path = os.path.join(os.path.dirname(__file__), "skipped_news_urls.txt")
        skipped_file      = open(skipped_urls_path, "w", encoding="utf-8")

        for i, url in enumerate(article_urls, 1):
            record = parse_article(page, url)

            if record is None and url not in failed_urls:
                # Distinguish timeout (None returned early) vs no-content
                # parse_article returns None for both — check if page loaded
                print(f"  [{i}/{len(article_urls)}] SKIP (no content)  {url}")
                skipped_file.write(url + "\n")
                skipped_no_content += 1
                continue

            print(f"  [{i}/{len(article_urls)}] OK  {record['title_ar'][:60]}")

            if not args.dry_run:
                batch.append(record)
                if len(batch) >= BATCH_SIZE:
                    s, f = upsert_batch(supabase, batch)
                    total_success += s
                    total_failed  += f
                    batch = []

            time.sleep(0.3)

        skipped_file.close()

        # Flush remaining
        if batch and not args.dry_run:
            s, f = upsert_batch(supabase, batch)
            total_success += s
            total_failed  += f

    finally:
        browser.close()
        pw.stop()

    # Report
    print("\n" + "=" * 60)
    print("DONE")
    print(f"  URLs collected       : {len(article_urls) + skipped_existing}")
    print(f"  Already in DB        : {skipped_existing}")
    print(f"  Fetched              : {len(article_urls)}")
    print(f"  Skipped (no content) : {skipped_no_content}")
    if not args.dry_run:
        print(f"  Upserted             : {total_success}")
        print(f"  Failed               : {total_failed}")
    else:
        print("  [DRY RUN — nothing written]")
    if skipped_no_content:
        print(f"  Skipped URLs logged  : {skipped_urls_path}")
    if failed_urls:
        print(f"  Failed URLs ({len(failed_urls)}):")
        for u in failed_urls:
            print(f"    {u}")
    print("=" * 60)


if __name__ == "__main__":
    main()