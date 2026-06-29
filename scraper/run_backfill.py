"""
Autonomous backfill orchestrator — runs the two remaining Etimad phases in order:

    1. AWARDS  — one pass over awarded-status tenders missing bidder data.
                 Single pass on purpose: genuinely-empty tenders never get rows,
                 so a loop-to-zero would never terminate. The 9/min results
                 rate-limiter (now wired in) recovers the ~23% that earlier
                 429-storming runs lost as false-empties.
    2. DETAILS — batched loop until no tenders need details. Each batch is a
                 fresh subprocess (bounds memory, gives clean restart points).
                 Stops when a cheap "any row left?" probe comes back empty.

Both phases are DB-resumable (each subprocess recomputes its work-list from
Supabase on start), so a crash just means the orchestrator relaunches and
continues from wherever the DB now stands.

Run (background):
    USE_PROXY=false python run_backfill.py > backfill.orchestrator.log 2>&1

Tunables via env (sensible defaults baked in):
    AWARD_CONCURRENCY (6), DETAIL_CONCURRENCY (5), DETAIL_BATCH (5000)
    SKIP_AWARDS=true   — jump straight to details
    SKIP_DETAILS=true  — awards only
"""
import os
import sys
import time
import subprocess
from datetime import datetime, timezone

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

DETAIL_BATCH = os.environ.get("DETAIL_BATCH", "5000")
AWARD_CONCURRENCY = os.environ.get("AWARD_CONCURRENCY", "6")
DETAIL_CONCURRENCY = os.environ.get("DETAIL_CONCURRENCY", "5")
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parallel.py")

PHASE_RETRIES = 6          # relaunch a crashed subprocess up to this many times
RETRY_SLEEP_S = 60
MAX_DETAIL_BATCHES = 200   # safety cap (200 × 5000 = 1M, far above the ~193k need)


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} | ORCH | {msg}", flush=True)


def any_rows_need_details() -> bool:
    """Cheap probe: is there at least one tender with purpose AND place NULL?
    limit=1 returns instantly even on the 290k table (no full count scan)."""
    url = (f"{SUPABASE_URL}/rest/v1/tenders"
           f"?select=etimad_tender_id&tender_purpose=is.null&place=is.null&limit=1")
    for attempt in range(5):
        try:
            r = httpx.get(url, headers=SB_HEADERS, timeout=60)
            if r.status_code in (200, 206):
                return len(r.json()) > 0
        except httpx.HTTPError as e:
            log(f"probe error (attempt {attempt+1}): {e}")
        time.sleep(5 * (attempt + 1))
    # On persistent probe failure, assume there's still work (safer than stopping early).
    return True


def run_subprocess(extra_env: dict, label: str) -> int:
    env = {**os.environ, "USE_PROXY": os.environ.get("USE_PROXY", "false"), **extra_env}
    log(f"launching {label}: {extra_env}")
    proc = subprocess.run([sys.executable, SCRIPT], env=env)
    log(f"{label} exited rc={proc.returncode}")
    return proc.returncode


def run_phase_with_retries(extra_env: dict, label: str) -> bool:
    for attempt in range(1, PHASE_RETRIES + 1):
        rc = run_subprocess(extra_env, f"{label} (attempt {attempt}/{PHASE_RETRIES})")
        if rc == 0:
            return True
        log(f"{label} failed (rc={rc}); retry in {RETRY_SLEEP_S}s")
        time.sleep(RETRY_SLEEP_S)
    log(f"{label} exhausted {PHASE_RETRIES} attempts — giving up on this phase")
    return False


def main():
    log("=== backfill orchestrator start ===")

    if os.environ.get("SKIP_AWARDS", "").lower() not in ("true", "1", "yes"):
        log("PHASE 1: AWARDS (single pass)")
        ok = run_phase_with_retries(
            {"MODE": "awards", "AWARD_CONCURRENCY": AWARD_CONCURRENCY},
            "awards",
        )
        log(f"PHASE 1 AWARDS {'completed' if ok else 'gave up'}")
    else:
        log("PHASE 1: AWARDS skipped (SKIP_AWARDS set)")

    if os.environ.get("SKIP_DETAILS", "").lower() not in ("true", "1", "yes"):
        log("PHASE 2: DETAILS (batched loop)")
        batch_n = 0
        while batch_n < MAX_DETAIL_BATCHES:
            if not any_rows_need_details():
                log("DETAILS: no rows left needing details — phase complete")
                break
            batch_n += 1
            ok = run_phase_with_retries(
                {"MODE": "details",
                 "LIMIT_DETAILS": DETAIL_BATCH,
                 "DETAIL_CONCURRENCY": DETAIL_CONCURRENCY},
                f"details-batch-{batch_n}",
            )
            if not ok:
                log("DETAILS: batch gave up after retries — pausing 5m then continuing")
                time.sleep(300)
        else:
            log(f"DETAILS: hit MAX_DETAIL_BATCHES={MAX_DETAIL_BATCHES} safety cap — stopping")
    else:
        log("PHASE 2: DETAILS skipped (SKIP_DETAILS set)")

    log("=== backfill orchestrator done ===")


if __name__ == "__main__":
    main()
