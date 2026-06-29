# Etimad — Daily Brief (scheduled email digest).
#
# Batch job — no user present. Runs once per day via GitHub Actions hitting
# POST /cron/daily-brief (secret-guarded) in main.py.
#
# For each subscribed user it:
#   1. Reads memory/profile  (company, sectors, city)
#   2. Calls tender_search directly against Supabase
#   3. Builds an Arabic HTML email with the top BRIEF_TENDER_COUNT tenders
#   4. Sends via Resend
#
# Subscription is stored in memory/email:
#   {"enabled": true, "email": "user@example.com"}

import asyncio
import hashlib
import hmac
import html as _html
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone

import requests
from cycls.app.db import DB, _store, workspace

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
AGENT_NAME   = "etimad"
VOLUME       = "/workspace"
EMAIL_KEY    = "memory/email"
PROFILE_KEY  = "memory/profile"
RESEND_URL   = "https://api.resend.com/emails"
BRIEF_TENDER_COUNT = 5   # how many tenders to include in each daily email

_RIYADH = timezone(timedelta(hours=3))
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Colors
_BG      = "#0f1117"
_SURFACE = "#1a1d27"
_BORDER  = "#2a2d3a"
_TEXT    = "#e8eaf0"
_MUTED   = "#8b90a0"
_BRAND   = "#4f8ef7"
_GREEN   = "#22c55e"
_YELLOW  = "#f59e0b"
_RED     = "#ef4444"


# ----------------------------------------------------------------------
# Environment helpers
# ----------------------------------------------------------------------
def _cron_secret() -> str:
    return os.environ.get("CRON_SECRET", "")

def _resend_key() -> str:
    return os.environ.get("RESEND_API_KEY", "")

def _supabase_url() -> str:
    return os.environ.get("SUPABASE_URL", "")

def _supabase_key() -> str:
    return os.environ.get("SUPABASE_SERVICE_KEY", "")

def _valid_email(addr: str) -> bool:
    return bool(addr) and bool(_EMAIL_RE.match(addr.strip()))

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _riyadh_date() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).date().strftime("%Y-%m-%d")


# ----------------------------------------------------------------------
# Storage helpers (mirrors TasiBot pattern)
# ----------------------------------------------------------------------
_on_gcp_cache: bool | None = None

def _on_gcp() -> bool:
    global _on_gcp_cache
    if _on_gcp_cache is None:
        try:
            req = urllib.request.Request(
                "http://metadata.google.internal/computeMetadata/v1/instance/id",
                headers={"Metadata-Flavor": "Google"},
            )
            urllib.request.urlopen(req, timeout=0.5).read()
            _on_gcp_cache = True
        except Exception:
            _on_gcp_cache = False
    return _on_gcp_cache

def storage_base() -> str:
    return f"gs://cycls-ws-{AGENT_NAME}" if _on_gcp() else f"file://{VOLUME}"

def _email_from() -> str:
    explicit = os.environ.get("EMAIL_FROM")
    if explicit:
        return explicit
    return "Etimad <brief@etimad.cycls.ai>" if _on_gcp() else "Etimad <onboarding@resend.dev>"

def _public_url() -> str:
    return os.environ.get("PUBLIC_URL", "https://etimad.cycls.ai").rstrip("/")

def _user_db(subject: str) -> DB:
    return DB(workspace(subject, VOLUME, base=storage_base(), slot=".database"))


# ----------------------------------------------------------------------
# HMAC tokens for unsubscribe links
# ----------------------------------------------------------------------
def make_token(user_id: str, action: str) -> str:
    import base64
    sig = hmac.new(_cron_secret().encode(), f"{action}:{user_id}".encode(),
                   hashlib.sha256).hexdigest()[:16]
    uid = base64.urlsafe_b64encode(user_id.encode()).decode().rstrip("=")
    return f"{uid}.{sig}"

def verify_token(token: str, action: str) -> str | None:
    import base64
    if not _cron_secret() or not token or "." not in token:
        return None
    uid_b64, _, sig = token.partition(".")
    try:
        pad = "=" * (-len(uid_b64) % 4)
        user_id = base64.urlsafe_b64decode(uid_b64 + pad).decode()
    except Exception:
        return None
    expected = hmac.new(_cron_secret().encode(), f"{action}:{user_id}".encode(),
                        hashlib.sha256).hexdigest()[:16]
    return user_id if hmac.compare_digest(sig, expected) else None


# ----------------------------------------------------------------------
# Subscription state mutations (called by /email/* routes in main.py)
# ----------------------------------------------------------------------
async def set_unsubscribed(user_id: str) -> bool:
    db = _user_db(user_id)
    pref = await db.get(EMAIL_KEY)
    if not pref:
        return False
    pref["enabled"] = False
    pref["unsubscribed_at"] = _now_iso()
    await db.put(EMAIL_KEY, pref)
    return True


# ----------------------------------------------------------------------
# Tender search (calls Supabase directly — no LLM involved)
# ----------------------------------------------------------------------
def search_tenders(sectors: str, city: str) -> list[dict]:
    """Query Supabase active_tenders for this user's profile."""
    url_base = _supabase_url()
    key      = _supabase_key()
    if not url_base or not key:
        return []

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    fields = "etimad_tender_id,tender_name,agency_name,publish_date,last_offer_date,condition_booklet_price,place,detail_url"

    # Trigram + multi-field search, ranked by city → relevance → deadline
    # (search_tenders RPC; see scraper/db/search_functions.sql).
    def _rpc(q):
        body = {"q": q, "only_active": True, "city": city or "", "agency": "", "max_rows": BRIEF_TENDER_COUNT}
        r = requests.post(f"{url_base}/rest/v1/rpc/search_tenders?select={fields}",
                          headers=headers, json=body, timeout=12)
        return r.json() if r.status_code == 200 else []

    rows = _rpc(sectors)
    if not rows:                 # sector matched nothing → soonest active in the city
        rows = _rpc("")

    return rows[:BRIEF_TENDER_COUNT]  # top N for the email


# ----------------------------------------------------------------------
# HTML email builder
# ----------------------------------------------------------------------
def _esc(s) -> str:
    return _html.escape(str(s or ""))

def _tender_row(i: int, t: dict) -> str:
    name      = _esc(t.get("tender_name", "—"))
    agency    = _esc(t.get("agency_name", "—"))
    published = _esc(t.get("publish_date", "—"))[:10]
    deadline  = _esc(t.get("last_offer_date", "—"))[:10]
    price     = t.get("condition_booklet_price")
    price_str = "مجاني" if not price or price == 0 else f"{price:,} ر.س"
    url       = t.get("detail_url", "#")

    return f"""
    <tr style="border-bottom:1px solid {_BORDER};">
      <td style="padding:14px 10px;color:{_BRAND};font-weight:700;font-size:16px;">{i}</td>
      <td style="padding:14px 10px;">
        <a href="{_esc(url)}" style="color:{_TEXT};text-decoration:none;font-weight:600;">{name}</a>
        <div style="color:{_MUTED};font-size:13px;margin-top:4px;">{agency}</div>
      </td>
      <td style="padding:14px 10px;color:{_MUTED};font-size:13px;white-space:nowrap;">{published}</td>
      <td style="padding:14px 10px;color:{_YELLOW};font-size:13px;white-space:nowrap;">{deadline}</td>
      <td style="padding:14px 10px;color:{_MUTED};font-size:13px;white-space:nowrap;">{price_str}</td>
    </tr>"""

def build_email(company: str, sectors: str, city: str,
                tenders: list[dict], user_id: str, date_str: str) -> tuple[str, str]:
    """Build (html, subject) for one subscriber."""

    unsub_url = f"{_public_url()}/email/unsubscribe?token={make_token(user_id, 'unsub')}"

    if tenders:
        rows_html = "".join(_tender_row(i + 1, t) for i, t in enumerate(tenders))
        table = f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;direction:rtl;text-align:right;">
          <thead>
            <tr style="border-bottom:2px solid {_BORDER};">
              <th style="padding:10px;color:{_MUTED};font-size:12px;font-weight:500;">#</th>
              <th style="padding:10px;color:{_MUTED};font-size:12px;font-weight:500;">المناقصة</th>
              <th style="padding:10px;color:{_MUTED};font-size:12px;font-weight:500;">تاريخ النشر</th>
              <th style="padding:10px;color:{_MUTED};font-size:12px;font-weight:500;">الموعد النهائي</th>
              <th style="padding:10px;color:{_MUTED};font-size:12px;font-weight:500;">رسوم الكراسة</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>"""
        body_content = f"""
        <p style="color:{_MUTED};font-size:14px;margin:0 0 20px;">
          إليك أبرز المناقصات المتاحة اليوم في قطاع <strong style="color:{_TEXT};">{_esc(sectors)}</strong>
          — منطقة <strong style="color:{_TEXT};">{_esc(city)}</strong>:
        </p>
        {table}"""
    else:
        body_content = f"""
        <p style="color:{_MUTED};font-size:14px;text-align:center;padding:40px 0;">
          لا توجد مناقصات متاحة حالياً في قطاعك ومنطقتك — سنُعلمك فور ظهور فرص جديدة.
        </p>"""

    html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>تقرير اعتماد اليومي</title>
</head>
<body style="margin:0;padding:0;background:{_BG};font-family:'IBM Plex Sans Arabic',Tahoma,Arial,sans-serif;direction:rtl;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:{_BG};padding:40px 20px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:{_SURFACE};border-radius:12px;border:1px solid {_BORDER};overflow:hidden;max-width:600px;width:100%;">

        <!-- Header -->
        <tr>
          <td style="background:{_BRAND};padding:24px 32px;">
            <div style="color:#fff;font-size:22px;font-weight:700;">اعتماد</div>
            <div style="color:rgba(255,255,255,0.75);font-size:13px;margin-top:4px;">
              التقرير اليومي للمناقصات — {_esc(date_str)}
            </div>
          </td>
        </tr>

        <!-- Greeting -->
        <tr>
          <td style="padding:28px 32px 0;">
            <p style="color:{_TEXT};font-size:16px;margin:0;">
              مرحباً بك في <strong>{_esc(company)}</strong> 👋
            </p>
          </td>
        </tr>

        <!-- Tenders -->
        <tr>
          <td style="padding:20px 32px;">
            {body_content}
          </td>
        </tr>

        <!-- CTA -->
        <tr>
          <td style="padding:0 32px 32px;text-align:center;">
            <a href="{_public_url()}"
               style="display:inline-block;background:{_BRAND};color:#fff;
                      text-decoration:none;padding:12px 32px;border-radius:8px;
                      font-weight:600;font-size:15px;margin-top:8px;">
              فتح اعتماد
            </a>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:20px 32px;border-top:1px solid {_BORDER};text-align:center;">
            <p style="color:{_MUTED};font-size:12px;margin:0;">
              لإلغاء الاشتراك في هذا التقرير،
              <a href="{_esc(unsub_url)}" style="color:{_MUTED};">اضغط هنا</a>
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    subject = f"مناقصات اعتماد اليومية — {date_str}"
    return html, subject


# ----------------------------------------------------------------------
# Send via Resend
# ----------------------------------------------------------------------
async def _send(to: str, subject: str, html: str, unsub_url: str | None = None) -> dict:
    key = _resend_key()
    if not key:
        raise RuntimeError("RESEND_API_KEY not configured")
    import httpx
    payload = {
        "from": _email_from(),
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if unsub_url:
        payload["headers"] = {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            RESEND_URL,
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Resend {r.status_code}: {r.text[:300]}")
        return r.json()


# ----------------------------------------------------------------------
# Load subscribers
# ----------------------------------------------------------------------
def _subject_from_key(k: str) -> str | None:
    parts = k.split("/")
    if len(parts) < 2 or parts[1] != ".database":
        return None
    depth = EMAIL_KEY.count("/") + 1
    after = len(parts) - 2
    if after == depth:
        return parts[0]
    if after == depth + 1:
        return f"{parts[0]}:{parts[2]}"
    return None

async def _load_subscribers(only_subject: str | None) -> list[tuple[str, DB, dict]]:
    base = storage_base()
    root = _store(base)
    keys = set(await root.list_keys(glob=f"*/.database/{EMAIL_KEY}"))
    keys |= set(await root.list_keys(glob=f"*/.database/*/{EMAIL_KEY}"))
    subs = []
    for k in keys:
        subject = _subject_from_key(k)
        if subject is None or (only_subject and subject != only_subject):
            continue
        db = _user_db(subject)
        pref = await db.get(EMAIL_KEY)
        if not pref or not pref.get("enabled"):
            continue
        if not _valid_email(pref.get("email", "")):
            continue
        subs.append((subject, db, pref))
    return subs


# ----------------------------------------------------------------------
# Main batch entry point
# ----------------------------------------------------------------------
async def run_daily_brief(
    *,
    dry_run: bool = False,
    only_user: str | None = None,
    force: bool = False,
) -> dict:
    """Called by POST /cron/daily-brief in main.py."""
    date_str = _riyadh_date()
    subs = await _load_subscribers(only_user)

    result = {
        "date": date_str,
        "subscribers": len(subs),
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "dry_run": dry_run,
    }

    previews, errors = [], []

    for user_id, db, pref in subs:
        # Idempotency — skip if already sent today
        sent_key = f"usage/daily_brief/{date_str}"
        if not dry_run and not force and await db.get(sent_key):
            result["skipped"] += 1
            continue

        # Load profile
        profile = await db.get(PROFILE_KEY) or {}
        company = profile.get("company", "شركتك")
        sectors = profile.get("sectors", "")
        city    = profile.get("city", "")

        if not sectors or not city:
            result["skipped"] += 1
            continue

        # Search tenders (direct Supabase — no LLM)
        tenders = search_tenders(sectors, city)

        # Build email
        try:
            html, subject = build_email(company, sectors, city, tenders, user_id, date_str)
        except Exception as e:
            result["failed"] += 1
            errors.append({"user": user_id, "error": f"render: {e}"})
            continue

        if dry_run:
            previews.append({
                "user": user_id,
                "email": pref["email"],
                "subject": subject,
                "tenders_found": len(tenders),
            })
            continue

        # Send
        try:
            unsub = f"{_public_url()}/email/unsubscribe?token={make_token(user_id, 'unsub')}"
            await _send(pref["email"], subject, html, unsub_url=unsub)
            await db.put(sent_key, {"at": _now_iso(), "email": pref["email"]})
            result["sent"] += 1
        except Exception as e:
            result["failed"] += 1
            errors.append({"user": user_id, "error": str(e)})

    if previews:
        result["previews"] = previews
    if errors:
        result["errors"] = errors

    return result


# ----------------------------------------------------------------------
# Tiny HTML page for verify/unsubscribe routes
# ----------------------------------------------------------------------
def page(message: str, ok: bool = True) -> str:
    """Small HTML page returned by the unsubscribe route in main.py."""
    color = _GREEN if ok else _RED
    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>اعتماد</title>
</head>
<body style="margin:0;padding:0;background:{_BG};font-family:Tahoma,Arial,sans-serif;direction:rtl;">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:80px 20px;">
    <tr><td align="center">
      <div style="max-width:480px;background:{_SURFACE};border-radius:12px;
                  padding:36px;border:1px solid {_BORDER};text-align:center;">
        <div style="font-size:20px;font-weight:700;color:{_BRAND};margin-bottom:16px;">اعتماد</div>
        <p style="font-size:16px;color:{color};margin:0;">{_esc(message)}</p>
      </div>
    </td></tr>
  </table>
</body>
</html>"""