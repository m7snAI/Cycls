# Etimad — a tender discovery agent built on Cycls.
#
# Run locally:   uv run cycls run main.py
# Deploy:        uv run cycls deploy main.py

import json
import os
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
            "- Results are sorted by deadline (soonest first).\n"
            "- Returns up to 10 matching tenders with: etimad_tender_id, tender_name, agency_name, last_offer_date, condition_booklet_price, place, detail_url.\n"
            "- If no results, a fallback search by city only is attempted automatically.\n"
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
    }
]

# ----------------------------------------------------------------------
# Tool handler
# ----------------------------------------------------------------------
_MAX_PAYLOAD_CHARS = 20_000


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

        url = (
            f"{supabase_url}/rest/v1/active_tenders"
            f"?tender_name=ilike.*{keyword}*"
            f"&place=ilike.*{place}*"
            f"&select=etimad_tender_id,tender_name,agency_name,last_offer_date,condition_booklet_price,place,detail_url"
            f"&order=last_offer_date.asc"
            f"&limit=10"
        )
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return {"error": f"Supabase returned {r.status_code}", "detail": r.text[:500]}

        rows = r.json()

        if not rows:
            url_fallback = (
                f"{supabase_url}/rest/v1/active_tenders"
                f"?place=ilike.*{place}*"
                f"&select=etimad_tender_id,tender_name,agency_name,last_offer_date,condition_booklet_price,place,detail_url"
                f"&order=last_offer_date.asc"
                f"&limit=10"
            )
            r2 = requests.get(url_fallback, headers=headers, timeout=10)
            rows = r2.json() if r2.status_code == 200 else []

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