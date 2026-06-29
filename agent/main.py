# Etimad — a tender discovery agent built on Cycls.
#
# Run locally:   uv run cycls run main.py
# Deploy:        uv run cycls deploy main.py

import json
import os
import statistics
from datetime import datetime, timedelta, timezone

import cycls
import daily_brief
from dotenv import load_dotenv
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from prompt import build_prompt

load_dotenv(".env")

# ----------------------------------------------------------------------
# Image
# ----------------------------------------------------------------------
image = (
    cycls.Image()
    # apt FIRST — matches TasiBot's known-good order
    .apt("libpango-1.0-0", "libpangoft2-1.0-0", "fontconfig")
    # pip SECOND — FastAPI pins in the SAME call as other packages.
    # The cycls SDK installs fastapi[standard] UNPINNED in its base layer;
    # putting our pins here (after apt, before run) ensures they win.
    # Exact versions from cycls 0.0.2.132's known-good trio (same as TasiBot).
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

web = cycls.Web().title("Etimad — tender discovery agent")

# ----------------------------------------------------------------------
# Custom tool definition
# ----------------------------------------------------------------------
TOOLS = [
    {
        "name": "tender_search",
        "description": (
            "Search active Saudi government tenders from منصة اعتماد.\n\n"
            "- Pass the user's sector keyword and city from their profile.\n"
            "- Matches by sector keyword, then ranks by city: same-city first, then tenders with no "
            "listed location, then other cities. Tenders without a city are NOT dropped, so some "
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
            "- Pass a SHORT, distinctive phrase from the tender name (e.g. 'الهوية البصرية', "
            "'تقويم التعليم') OR the tender/reference number. Do NOT paste the whole sentence.\n"
            "- Searches the FULL tenders table, including CLOSED tenders, and ignores city.\n"
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
                    "description": "A short distinctive phrase from the tender name, or the tender/reference number.",
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


def _city_rank(tender: dict, region: str) -> int:
    """Rank a tender by city relevance — used to sort without EXCLUDING:
    0 = same city, 1 = no listed location (keep it), 2 = a different city."""
    p = (tender.get("place") or "").strip()
    if region and region in p:
        return 0
    if not p:
        return 1
    return 2


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
        import requests

        keyword = args.get("sector", "")
        place   = args.get("region", "")

        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
        }

        fields = "etimad_tender_id,tender_name,agency_name,publish_date,last_offer_date,condition_booklet_price,place,detail_url"

        # Forgiving search: match by sector keyword only, then RANK by city below.
        # We never drop a tender just because its place is empty or a different
        # region (place coverage is incomplete in the DB).
        url = (
            f"{supabase_url}/rest/v1/active_tenders"
            f"?tender_name=ilike.*{keyword}*"
            f"&select={fields}"
            f"&order=last_offer_date.asc"
            f"&limit=50"
        )
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return {"error": f"Supabase returned {r.status_code}", "detail": r.text[:500]}

        rows = r.json()

        # Last resort: the sector matched nothing → soonest active tenders in the city.
        if not rows:
            url_fallback = (
                f"{supabase_url}/rest/v1/active_tenders"
                f"?place=ilike.*{place}*"
                f"&select={fields}"
                f"&order=last_offer_date.asc"
                f"&limit=10"
            )
            r2 = requests.get(url_fallback, headers=headers, timeout=10)
            rows = r2.json() if r2.status_code == 200 else []

        if not rows:
            return {"tenders": [], "count": 0, "hint": "No active tenders found."}

        # Stable sort keeps deadline order within each city tier.
        rows.sort(key=lambda t: _city_rank(t, place))
        rows = rows[:10]

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
        import requests

        query = (args.get("query") or "").strip()
        if not query:
            return {"tenders": [], "count": 0, "found": False, "hint": "Empty query."}

        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
        }

        # 1) by name (most recent first; includes closed tenders)
        url = (
            f"{supabase_url}/rest/v1/tenders"
            f"?tender_name=ilike.*{query}*"
            f"&select={_LOOKUP_FIELDS}"
            f"&order=publish_date.desc.nullslast"
            f"&limit=5"
        )
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return {"error": f"Supabase returned {r.status_code}", "detail": r.text[:500]}
        rows = r.json()

        # 2) fallback: by tender / reference number
        if not rows:
            url_num = (
                f"{supabase_url}/rest/v1/tenders"
                f"?or=(tender_number.ilike.*{query}*,reference_number.ilike.*{query}*)"
                f"&select={_LOOKUP_FIELDS}"
                f"&order=publish_date.desc.nullslast"
                f"&limit=5"
            )
            r2 = requests.get(url_num, headers=headers, timeout=10)
            rows = r2.json() if r2.status_code == 200 else []

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

        # 1) comparable tenders by work-type keyword (+ optional same agency)
        url = (
            f"{supabase_url}/rest/v1/tenders"
            f"?tender_name=ilike.*{keyword}*"
            + (f"&agency_name=ilike.*{agency}*" if agency else "")
            + "&select=etimad_tender_id,tender_name,agency_name&limit=80"
        )
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return {"error": f"Supabase returned {r.status_code}", "detail": r.text[:300]}
        tenders = r.json()
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
    .model("anthropic/claude-sonnet-4-6")
    .api_key(os.environ.get("ANTHROPIC_API_KEY", ""))
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
@cycls.agent(image=image, web=web, name="etimad")
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


etimad.local()