"""
Etimad Tenders Scraper
======================
بيسحب كل المنافسات النشطة من tenders.etimad.sa
ويرفعها على Supabase.

Usage:
    export SUPABASE_URL="https://xxxxx.supabase.co"
    export SUPABASE_SERVICE_KEY="eyJ..."
    python scraper.py
"""

import os
import re
import json
import time
import random
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup
from supabase import create_client, Client

# لو في .env في الـ working directory، حمّل منه. مفيش مشكلة لو الـ env vars
# متسطّبة بطريقة تانية (مثلاً GitHub Actions secrets).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------- Configuration ----------
BASE_URL = "https://tenders.etimad.sa"
# الصفحة بتاعت listing عند زيارتها بترندر JS وبتنادي على الـ JSON endpoint ده.
# الـ API بيرجّع كل البيانات الأساسية للمنافسات بدون ما نحتاج نـ parse HTML.
LISTING_API_URL = f"{BASE_URL}/Tender/AllSupplierTendersForVisitorAsync"
LISTING_REFERER = f"{BASE_URL}/Tender/AllTendersForVisitor"
LISTING_PAGE_SIZE = 6  # نفس الـ default بتاع الـ Vue app
# PublishDateId=5 هو الـ default filter اللي الـ JS بيضيفه على كل request.
# لو شيلناه، الـ API بيرجّع 283k tender (كل اللي اتسجلوا) بدل ~9600 النشطين.
# لو شيلناه كمان، الـ pagination بتقف عند page 1.
LISTING_PUBLISH_DATE_ID = 5
# الـ endpoint بتاع تصنيف المقاولين + مكان التنفيذ. الـ "Relations" مجرد اسم داخلي.
RELATIONS_URL = f"{BASE_URL}/Tender/GetRelationsDetailsViewComponenet"  # spelling sic
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# NOTE: `or "4"` (not a get-default) so an env var set to EMPTY string still
# falls back. GHA passes `REQUEST_DELAY_MIN: ${{ inputs.x }}` which is "" on
# scheduled runs (inputs only exist for workflow_dispatch) — float("") crashes
# the whole scraper at import. This bit the daily cron silently for days.
REQUEST_DELAY_MIN = float(os.environ.get("REQUEST_DELAY_MIN") or "4")   # ثواني بين الطلبات. أقل من 5 ثواني الـ WAF بيرجّع responses فاضية أحياناً.
REQUEST_DELAY_MAX = float(os.environ.get("REQUEST_DELAY_MAX") or "6")
MAX_RETRIES = 3
TIMEOUT = 30
MAX_PAGES = 1700           # 9600 / PageSize 6 = 1600 صفحة + buffer
UPSERT_BATCH_SIZE = 50     # كل N منافسة، نـ flush للـ DB. بيدّي visibility + crash resilience.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("etimad")


# ---------- Helpers ----------
def random_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def polite_sleep():
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def parse_arabic_date(text: Optional[str]) -> Optional[str]:
    """
    Etimad بيعرض التواريخ بصيغة مختلفة. ده بيحاول يستخرج تاريخ ISO منها.
    لو فشل، يرجّع None ونحتفظ بالـ raw في raw_data.
    """
    if not text:
        return None
    text = text.strip()
    # نمط: 2025-11-20 14:30 أو 2025/11/20
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", text)
    if m:
        y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        hh = (m.group(4) or "00").zfill(2)
        mm = (m.group(5) or "00").zfill(2)
        try:
            dt = datetime.fromisoformat(f"{y}-{mo}-{d}T{hh}:{mm}:00+03:00")  # Saudi TZ
            return dt.isoformat()
        except ValueError:
            return None
    return None


def parse_money(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    # شيل أي حاجة مش رقم أو فاصلة
    clean = re.sub(r"[^\d.,]", "", text).replace(",", "")
    try:
        return float(clean) if clean else None
    except ValueError:
        return None


def extract_tender_id(url: str) -> Optional[str]:
    """يستخرج المعرّف الفريد من رابط تفاصيل المنافسة."""
    if not url:
        return None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    # المفتاح ممكن يكون STenderId أو tenderIdString
    for key in ("STenderId", "tenderIdString", "TenderId"):
        if key in qs:
            return qs[key][0]
    # fallback: آخر segment في الـ path
    parts = [p for p in parsed.path.split("/") if p]
    return parts[-1] if parts else None


# ---------- HTTP with retry ----------
class EtimadClient:
    def __init__(self):
        self.client = httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers=random_headers(),
        )

    def get(self, url: str, params: Optional[dict] = None, extra_headers: Optional[dict] = None) -> Optional[str]:
        for attempt in range(MAX_RETRIES):
            try:
                # غيّر الـ user-agent كل طلب لتنويع الـ signature
                self.client.headers.update({"User-Agent": random.choice(USER_AGENTS)})
                r = self.client.get(url, params=params, headers=extra_headers or {})
                if r.status_code == 200:
                    return r.text
                if r.status_code in (429, 503):
                    wait = (attempt + 1) * 10
                    log.warning("Rate limited (%d). Waiting %ds…", r.status_code, wait)
                    time.sleep(wait)
                    continue
                log.warning("HTTP %d for %s", r.status_code, url)
            except httpx.RequestError as e:
                # شامل: NetworkError, TimeoutException, RemoteProtocolError, ProxyError, …
                log.warning("Request error (attempt %d, %s): %s", attempt + 1, type(e).__name__, e)
                time.sleep(5 * (attempt + 1))
        return None

    def get_json(self, url: str, params: Optional[dict] = None, referer: Optional[str] = None) -> Optional[dict]:
        """
        بينادي JSON endpoint مع الـ headers المناسبة (X-Requested-With + Accept JSON + Referer).
        لو الـ WAF رمى لينا HTML challenge بدل JSON، بيرجّع None.
        """
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        if referer:
            headers["Referer"] = referer
        for attempt in range(MAX_RETRIES):
            try:
                self.client.headers.update({"User-Agent": random.choice(USER_AGENTS)})
                r = self.client.get(url, params=params, headers=headers)
                ctype = r.headers.get("content-type") or ""
                if r.status_code == 200 and "json" in ctype:
                    return r.json()
                # 400 من Etimad غالباً مؤقت ("حدث خطأ غير متوقع")، 429/503 الـ rate-limit المعتاد،
                # و JSON غير صالح بـ status 200 = WAF challenge. كلها بنعاملها retry-with-backoff.
                if r.status_code in (400, 429, 500, 502, 503) or (r.status_code == 200 and "json" not in ctype):
                    wait = (attempt + 1) * 10
                    log.warning("Transient (%d, %s) for page-like request. Waiting %ds…", r.status_code, ctype, wait)
                    time.sleep(wait)
                    continue
                log.warning("HTTP %d for %s", r.status_code, url)
            except httpx.RequestError as e:
                log.warning("Request error (attempt %d, %s): %s", attempt + 1, type(e).__name__, e)
                time.sleep(5 * (attempt + 1))
        return None

    def close(self):
        self.client.close()


# ---------- Listing API parser ----------
def _clean_iso_date(value: Optional[str]) -> Optional[str]:
    """
    الـ API بيرجّع تواريخ في صيغة ISO من غير timezone (e.g. '2026-06-01T09:59:00').
    بنفترض أنها بتوقيت السعودية (+03:00) ونحوّلها لـ ISO كامل.
    لو القيمة فاضية أو حسرة (e.g. '0001-01-01T00:00:00')، يرجّع None.
    """
    if not value or not isinstance(value, str):
        return None
    if value.startswith("0001-") or value.startswith("0000-"):
        return None
    if "+" in value or value.endswith("Z"):
        return value
    return value + "+03:00"


def tender_from_json(item: dict) -> Optional[dict]:
    """
    يحوّل tender واحد من JSON response لـ dict متوافق مع schema tenders.
    """
    tender_id = item.get("tenderIdString")
    if not tender_id:
        return None

    if item.get("isUGRP") and item.get("ugrpRfxUrl"):
        detail_url = urljoin(BASE_URL, item["ugrpRfxUrl"])
    else:
        from urllib.parse import quote
        detail_url = f"{BASE_URL}/Tender/DetailsForVisitor?STenderId={quote(tender_id, safe='')}"

    return {
        "etimad_tender_id": tender_id,
        "tender_name": item.get("tenderName") or "بدون اسم",
        "tender_number": item.get("tenderNumber"),
        "reference_number": item.get("referenceNumber"),
        "agency_name": item.get("agencyName"),
        "branch_name": item.get("branchName"),
        "tender_type": item.get("tenderTypeName"),
        "tender_status": item.get("tenderStatusName"),
        "last_offer_date": _clean_iso_date(item.get("lastOfferPresentationDate")),
        "last_enquiry_date": _clean_iso_date(item.get("lastEnqueriesDate")),
        "offers_opening_date": _clean_iso_date(item.get("offersOpeningDate")),
        # createdAt دايماً بيرجع 0001-01-01 (sentinel). submitionDate (sic) هو وقت النشر الفعلي.
        "publish_date": _clean_iso_date(item.get("submitionDate")),
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


# ---------- Detail page parsing ----------
# الـ Arabic labels دي محتاجة تأكيد من صفحة /Tender/Details فعلية.
# لو لقيت حقول مش بتتعبّى، افتح صفحة تفاصيل في المتصفح وقارن الـ labels بالكلام ده.
DETAIL_FIELD_LABELS = {
    "الرقم المرجعي": "reference_number",
    "رقم المنافسة": "tender_number",
    "اسم المنافسة": "tender_name",
    "نوع المنافسة": "tender_type",
    "الغرض من المنافسة": "tender_purpose",
    "اسم الجهة الحكومية": "agency_name",
    "الجهة الحكومية": "agency_name",
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


def _clean_label(text: str) -> str:
    return re.sub(r"[:：\s]+$", "", text.strip())


def parse_detail_page(html: str) -> dict:
    """
    يستخرج بيانات تفاصيل المنافسة من صفحة /Tender/Details.
    الـ structure الأساسي في صفحات اعتماد:
        <div class="row">
            <div class="col-4 etd-item-title">LABEL</div>
            <div class="col-8 etd-item-info"><span>VALUE</span></div>
        </div>
    بنطابق الـ Arabic label مع DETAIL_FIELD_LABELS.
    """
    soup = BeautifulSoup(html, "html.parser")
    pairs: list[tuple[str, str]] = []

    # النمط الأساسي بتاع اعتماد: .etd-item-title + .etd-item-info كأخوات في نفس الـ .row
    for title in soup.select(".etd-item-title"):
        parent = title.parent
        if not parent:
            continue
        info = parent.select_one(".etd-item-info")
        if not info:
            continue

        # شيل markers بتاع "عرض المزيد"/"عرض الأقل"
        info_clone = BeautifulSoup(str(info), "html.parser")
        for tag in info_clone.select("i.readMore, i.readLess"):
            tag.decompose()

        # الـ "الغرض من المنافسة" بيكون فيه span للنص المختصر + span hidden للنص الكامل.
        # خد الأطول.
        spans = [s.get_text(" ", strip=True) for s in info_clone.find_all("span")]
        spans = [s for s in spans if s]
        value_text = max(spans, key=len) if len(spans) >= 2 else info_clone.get_text(" ", strip=True)
        pairs.append((title.get_text(" ", strip=True), value_text))

    # fallbacks لو الـ structure اتغير أو في sections بـ <dl> أو <table>
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            pairs.append((dt.get_text(strip=True), dd.get_text(" ", strip=True)))
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            pairs.append((cells[0].get_text(strip=True), cells[1].get_text(" ", strip=True)))

    fields: dict = {}
    for raw_label, raw_value in pairs:
        column = DETAIL_FIELD_LABELS.get(_clean_label(raw_label))
        if not column:
            continue
        value = raw_value.strip()
        if not value or value in ("—", "-", "غير محدد"):
            continue
        if column in DETAIL_DATE_FIELDS:
            parsed = parse_arabic_date(value)
            if parsed:
                fields[column] = parsed
        elif column in DETAIL_MONEY_FIELDS:
            parsed = parse_money(value)
            if parsed is not None:
                fields[column] = parsed
        else:
            fields[column] = value

    # snippet من صفحة التفاصيل للـ debugging لو الـ labels مش متطابقة
    fields["_detail_html"] = str(soup)[:5000]

    return fields


def fetch_tender_relations(client: "EtimadClient", tender_id_str: str) -> dict:
    """
    بيسحب الـ AJAX fragment بتاع تصنيف المقاولين + مكان التنفيذ + نشاط المنافسة.
    بيرجّع dict فيه:
      - 'place' لو لقاه (من 'مكان التنفيذ')
      - '_raw' بكل الـ label→value pairs كـ Arabic dict (للـ raw_data)
    """
    from urllib.parse import quote
    referer = f"{BASE_URL}/Tender/DetailsForVisitor?STenderId={quote(tender_id_str, safe='')}"
    html = client.get(
        RELATIONS_URL,
        params={"tenderIdStr": tender_id_str},
        extra_headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/html, */*; q=0.01",
            "Referer": referer,
        },
    )
    if not html:
        return {}

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

    if not pairs:
        return {}

    out: dict = {"_raw": pairs}
    place = pairs.get("مكان التنفيذ")
    if place:
        out["place"] = place
    return out


# ---------- Database ----------
class SupabaseRepo:
    def __init__(self, url: str, key: str):
        self.client: Client = create_client(url, key)

    @staticmethod
    def _with_retry(op_name: str, fn, max_attempts: int = 4):
        """
        بـ retry للـ Supabase operations لو حصلت errors مؤقتة (StreamReset, network blip, etc).
        الـ upsert idempotent بسبب on_conflict، فآمن نعيد المحاولة.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                return fn()
            except httpx.RequestError as e:
                last_exc = e
                wait = 5 * (attempt + 1)
                log.warning("Supabase %s error (attempt %d/%d, %s): %s. Waiting %ds…",
                            op_name, attempt + 1, max_attempts, type(e).__name__, e, wait)
                time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    def start_run(self) -> int:
        r = self._with_retry("start_run",
            lambda: self.client.table("scrape_runs").insert({"status": "running"}).execute())
        return r.data[0]["id"]

    def finish_run(self, run_id: int, **kwargs):
        kwargs["finished_at"] = datetime.now(timezone.utc).isoformat()
        self._with_retry("finish_run",
            lambda: self.client.table("scrape_runs").update(kwargs).eq("id", run_id).execute())

    def upsert_tenders(self, tenders: list[dict]) -> tuple[int, int]:
        """
        Upsert by etimad_tender_id.
        يرجّع (new_count, updated_count) — تقريبي.
        """
        if not tenders:
            return 0, 0

        # جيب الموجود حالياً عشان نفرّق بين new و updated
        ids = [t["etimad_tender_id"] for t in tenders]
        existing = self._with_retry("get-existing",
            lambda: self.client.table("tenders").select("etimad_tender_id").in_("etimad_tender_id", ids).execute())
        existing_ids = {row["etimad_tender_id"] for row in existing.data}

        new_count = len([t for t in tenders if t["etimad_tender_id"] not in existing_ids])
        updated_count = len(tenders) - new_count

        # Upsert على دفعات (Supabase fine with ~500 per batch)
        BATCH = 200
        for i in range(0, len(tenders), BATCH):
            batch = tenders[i : i + BATCH]
            self._with_retry("upsert",
                lambda b=batch: self.client.table("tenders").upsert(
                    b, on_conflict="etimad_tender_id"
                ).execute())

        return new_count, updated_count

    def get_ids_needing_details(self, seen_ids: set[str]) -> set[str]:
        """
        يرجّع الـ IDs اللي محتاجة سحب صفحة تفاصيل:
        - المنافسات الجديدة (مش في DB)
        - المنافسات الموجودة بس بدون reference_number (يعني صفحة التفاصيل مسحبتش قبل كده)
        """
        if not seen_ids:
            return set()

        ids_list = list(seen_ids)
        BATCH = 200
        existing_with_details: set[str] = set()
        for i in range(0, len(ids_list), BATCH):
            batch = ids_list[i : i + BATCH]
            r = self._with_retry("get-needing-details",
                lambda b=batch: self.client.table("tenders")
                    .select("etimad_tender_id")
                    .in_("etimad_tender_id", b)
                    .not_.is_("reference_number", "null")
                    .execute())
            existing_with_details.update(row["etimad_tender_id"] for row in r.data)

        return set(seen_ids) - existing_with_details

    def deactivate_missing(self, seen_ids: set[str]) -> int:
        """
        المنافسات اللي كانت active ومش ظاهرة في الـ scrape ده — اعتبرها انتهت.
        Snapshot logic: is_active = false للمنافسات اللي مظهرتش النهاردة.
        """
        if not seen_ids:
            return 0
        # نجيب الـ IDs النشطة حالياً وغير موجودة في seen_ids
        result = self.client.table("tenders").select("etimad_tender_id").eq("is_active", True).execute()
        currently_active = {row["etimad_tender_id"] for row in result.data}
        to_deactivate = currently_active - seen_ids

        if to_deactivate:
            # update على دفعات
            ids_list = list(to_deactivate)
            BATCH = 200
            for i in range(0, len(ids_list), BATCH):
                self.client.table("tenders").update({"is_active": False}).in_(
                    "etimad_tender_id", ids_list[i : i + BATCH]
                ).execute()

        return len(to_deactivate)


# ---------- Main pipeline ----------
def main():
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not (supabase_url and supabase_key):
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars")

    repo = SupabaseRepo(supabase_url, supabase_key)
    run_id = repo.start_run()
    log.info("Started scrape run #%d", run_id)

    client = EtimadClient()
    all_tenders: list[dict] = []
    seen_ids: set[str] = set()
    pages_scraped = 0
    error_msg = None

    try:
        page = 1
        total_count = None
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 10  # نكسر بس لو 10 صفحات متتاليات فشلوا
        while page <= MAX_PAGES:
            log.info("Fetching listing page %d…", page)
            resp = client.get_json(
                LISTING_API_URL,
                params={"PageNumber": page, "PageSize": LISTING_PAGE_SIZE, "PublishDateId": LISTING_PUBLISH_DATE_ID},
                referer=LISTING_REFERER,
            )
            if not resp:
                consecutive_failures += 1
                log.warning("Page %d failed (consecutive: %d/%d), skipping",
                            page, consecutive_failures, MAX_CONSECUTIVE_FAILURES)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error("%d consecutive failures, stopping listing pass", consecutive_failures)
                    break
                page += 1
                polite_sleep()
                continue
            consecutive_failures = 0

            items = resp.get("data") or []
            pages_scraped += 1
            if total_count is None:
                total_count = resp.get("totalCount") or 0
                log.info("Total tenders reported by API: %d", total_count)

            if not items:
                # API قال خلاص — لو وصلنا للنهاية الحقيقية، توقف
                log.info("No items on page %d, stopping", page)
                break

            page_new = 0
            for item in items:
                t = tender_from_json(item)
                if t and t["etimad_tender_id"] not in seen_ids:
                    seen_ids.add(t["etimad_tender_id"])
                    all_tenders.append(t)
                    page_new += 1

            log.info("Page %d: %d items, %d new (running total: %d)", page, len(items), page_new, len(all_tenders))

            page_size = resp.get("pageSize") or LISTING_PAGE_SIZE
            if total_count and page * page_size >= total_count:
                log.info("Reached end of results (total=%d)", total_count)
                break

            page += 1
            polite_sleep()

        # ---------- سحب صفحة التفاصيل لكل منافسة محتاجاها ----------
        # بنسحب التفاصيل بس للمنافسات الجديدة أو اللي مسحبتش تفاصيلها قبل كده،
        # عشان متضربش نفس الصفحة كل يوم.
        needs_details = repo.get_ids_needing_details(seen_ids)
        to_fetch = [t for t in all_tenders if t["etimad_tender_id"] in needs_details]
        log.info("Fetching detail pages for %d tenders (of %d total, skipping %d already complete)…",
                 len(to_fetch), len(all_tenders), len(all_tenders) - len(to_fetch))

        details_failed = 0
        new_count = 0
        updated_count = 0
        batch_buffer: list[dict] = []

        def flush_batch():
            nonlocal new_count, updated_count, batch_buffer
            if not batch_buffer:
                return
            n, u = repo.upsert_tenders(batch_buffer)
            new_count += n
            updated_count += u
            log.info("Flushed %d to DB (cumulative: %d new, %d updated)", len(batch_buffer), new_count, updated_count)
            batch_buffer = []

        for i, tender in enumerate(to_fetch, 1):
            detail_url = tender.get("detail_url")
            if not detail_url:
                continue
            html = client.get(detail_url)
            if not html:
                details_failed += 1
                log.warning("Detail fetch failed for %s", tender["etimad_tender_id"])
                polite_sleep()
                continue

            try:
                detail_fields = parse_detail_page(html)
            except Exception as e:
                details_failed += 1
                log.error("Detail parse failed for %s: %s", tender["etimad_tender_id"], e)
                polite_sleep()
                continue

            # خزّن snippet التفاصيل تحت raw_data للـ debugging
            raw = tender.get("raw_data") or {}
            if "_detail_html" in detail_fields:
                raw["detail_html_snippet"] = detail_fields.pop("_detail_html")
            tender["raw_data"] = raw

            # الـ detail بيطغى على الـ listing (لأن أدق)، بس بنشيل أي None
            tender.update({k: v for k, v in detail_fields.items() if v is not None})

            # تصنيف المقاولين + مكان التنفيذ
            polite_sleep()
            relations = fetch_tender_relations(client, tender["etimad_tender_id"])
            if relations:
                raw_relations = relations.pop("_raw", None)
                tender.update({k: v for k, v in relations.items() if v})
                if raw_relations:
                    tender["raw_data"]["relations"] = raw_relations

            # دفعة جاهزة — ضيفها للـ buffer وفلش كل UPSERT_BATCH_SIZE منافسة
            batch_buffer.append(tender)
            if len(batch_buffer) >= UPSERT_BATCH_SIZE:
                flush_batch()

            if i % 25 == 0:
                log.info("Details progress: %d/%d (%d failed)", i, len(to_fetch), details_failed)
            polite_sleep()

        # الـ batch الأخيرة (لو ناقصة UPSERT_BATCH_SIZE)
        flush_batch()
        log.info("Details done: %d fetched, %d failed", len(to_fetch) - details_failed, details_failed)

        # عطّل المنافسات اللي اختفت
        deactivated = repo.deactivate_missing(seen_ids)

        log.info(
            "Done: %d new, %d updated, %d deactivated",
            new_count, updated_count, deactivated,
        )

        repo.finish_run(
            run_id,
            status="success",
            tenders_found=len(all_tenders),
            tenders_new=new_count,
            tenders_updated=updated_count,
            tenders_deactivated=deactivated,
            pages_scraped=pages_scraped,
        )

    except Exception as e:
        log.exception("Scrape failed")
        error_msg = str(e)[:1000]
        # محاولة نـ mark الـ run كـ failed، بس مش نـ crash لو الـ DB كمان مش راضي.
        try:
            repo.finish_run(
                run_id,
                status="failed",
                tenders_found=len(all_tenders),
                pages_scraped=pages_scraped,
                error_message=error_msg,
            )
        except Exception as e2:
            log.error("Failed to update scrape_runs to 'failed': %s", e2)
        raise
    finally:
        client.close()


def dry_run():
    """
    اختبار سريع من غير Supabase: بينادي الـ listing JSON endpoint، يحوّل أول صفحة
    لـ tender dicts، وبيجرب يسحب صفحة تفاصيل واحدة. كل الـ outputs على القرص.
    """
    client = EtimadClient()
    try:
        log.info("Fetching listing page 1 via JSON API…")
        resp = client.get_json(
            LISTING_API_URL,
            params={"PageNumber": 1, "PageSize": LISTING_PAGE_SIZE, "PublishDateId": LISTING_PUBLISH_DATE_ID},
            referer=LISTING_REFERER,
        )
        if not resp:
            log.error("Failed to fetch listing (network error or blocked)")
            return

        with open("dry_run_listing.json", "w", encoding="utf-8") as f:
            json.dump(resp, f, ensure_ascii=False, indent=2)
        log.info(
            "Saved → dry_run_listing.json | totalCount=%d, pageSize=%s, rows=%d",
            resp.get("totalCount", 0), resp.get("pageSize"), len(resp.get("data") or []),
        )

        items = resp.get("data") or []
        tenders = [t for t in (tender_from_json(it) for it in items) if t]
        log.info("Mapped %d tenders from listing", len(tenders))

        if not tenders:
            log.warning("0 tenders mapped — افحص dry_run_listing.json")
            return

        for i, t in enumerate(tenders[:3], 1):
            log.info(
                "Tender %d: id=%s name=%r agency=%r",
                i, t["etimad_tender_id"][:20] + "...",
                (t.get("tender_name") or "")[:60],
                (t.get("agency_name") or "")[:40],
            )

        first = tenders[0]
        detail_url = first.get("detail_url")
        if not detail_url:
            log.warning("First tender has no detail_url, skipping detail test")
            return

        log.info("Fetching detail page for tender %s…", first["etimad_tender_id"][:20])
        polite_sleep()
        detail_html = client.get(detail_url, extra_headers={"Referer": LISTING_REFERER})
        if not detail_html:
            log.error("Failed to fetch detail page")
            return

        with open("dry_run_detail.html", "w", encoding="utf-8") as f:
            f.write(detail_html)
        log.info("Saved detail HTML → dry_run_detail.html (%d bytes)", len(detail_html))

        detail_fields = parse_detail_page(detail_html)
        detail_fields.pop("_detail_html", None)

        log.info("Detail fields parsed: %d", len(detail_fields))
        for k, v in detail_fields.items():
            log.info("  %s = %r", k, str(v)[:100])

        # fetch relations (classification + execution location)
        polite_sleep()
        log.info("Fetching relations (classification + place)…")
        relations = fetch_tender_relations(client, first["etimad_tender_id"])
        raw_relations = relations.pop("_raw", None) if relations else None
        for k, v in relations.items():
            log.info("  relations %s = %r", k, str(v)[:120])
        if raw_relations:
            log.info("  raw relations labels: %s", list(raw_relations.keys()))

        merged = {**first, **detail_fields, **relations}
        if raw_relations:
            merged.setdefault("raw_data", {})["relations"] = raw_relations
        with open("dry_run_tender.json", "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2, default=str)
        log.info("Saved merged tender → dry_run_tender.json")

    finally:
        client.close()


if __name__ == "__main__":
    if os.environ.get("DRY_RUN") == "1":
        dry_run()
    else:
        main()
