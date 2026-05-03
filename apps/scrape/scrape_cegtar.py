"""
nemzeticegtar_scraper.py
------------------------
Scrapes Hungarian companies from nemzeticegtar.hu
Improved version with stealth measures to avoid bot detection.
"""

import asyncio
import csv
import logging
import random
import re
import sqlite3
import time
from dataclasses import dataclass, fields, astuple
from itertools import product
from pathlib import Path

import httpx
import trafilatura
from trafilatura import extract
from trafilatura.settings import use_config

# ── Config ─────────────────────────────────────────────────────────────────────

BASE_URL    = "https://www.nemzeticegtar.hu"
CONCURRENCY = 4          # Lowered for stealth
DELAY_MIN   = 3.0        # Increased delays
DELAY_MAX   = 7.0
DB_PATH     = Path("cegtar.db")
CSV_PATH    = Path("cegtar.csv")
LOG_PATH    = Path("cegtar.log")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
]

# County codes 01–20, company form codes (most common)
COUNTY_CODES = [f"{i:02d}" for i in range(1, 21)]
FORM_CODES   = ["01", "02", "09", "10", "11", "12", "13", "14",
                "16", "17", "18", "19", "20", "21", "31", "32"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

traf_cfg = use_config()
traf_cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "30")

# Global flag to handle blocks
BLOCKED_UNTIL = 0

# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class Company:
    ceg_id: str
    url: str
    name: str
    short_name: str
    full_name: str
    founded: str
    tax_id: str
    main_activity: str
    address: str
    branch_count: str
    headcount: str
    revenue: str
    registered_capital: str
    profit_after_tax: str
    risk_ratio: str
    positive_info: str
    negative_info: str
    signatories: str
    status_info: str
    raw_text: str

# ── Database ───────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            ceg_id TEXT PRIMARY KEY,
            url TEXT,
            name TEXT,
            short_name TEXT,
            full_name TEXT,
            founded TEXT,
            tax_id TEXT,
            main_activity TEXT,
            address TEXT,
            branch_count TEXT,
            headcount TEXT,
            revenue TEXT,
            registered_capital TEXT,
            profit_after_tax TEXT,
            risk_ratio TEXT,
            positive_info TEXT,
            negative_info TEXT,
            signatories TEXT,
            status_info TEXT,
            raw_text TEXT,
            scraped_at REAL DEFAULT (unixepoch())
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failed (
            ceg_id TEXT PRIMARY KEY,
            reason TEXT,
            attempts INTEGER DEFAULT 1,
            last_tried REAL DEFAULT (unixepoch())
        )
    """)
    conn.commit()

def already_done(conn: sqlite3.Connection, ceg_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM companies WHERE ceg_id=?", (ceg_id,)
    ).fetchone() is not None

def save_company(conn: sqlite3.Connection, c: Company):
    conn.execute("""
        INSERT OR REPLACE INTO companies
        (ceg_id, url, name, short_name, full_name, founded, tax_id,
         main_activity, address, branch_count, headcount, revenue,
         registered_capital, profit_after_tax, risk_ratio, 
         positive_info, negative_info, signatories, status_info, raw_text)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, astuple(c))
    conn.commit()

def mark_failed(conn: sqlite3.Connection, ceg_id: str, reason: str):
    conn.execute("""
        INSERT INTO failed (ceg_id, reason) VALUES (?, ?)
        ON CONFLICT(ceg_id) DO UPDATE SET
            attempts = attempts + 1,
            reason = excluded.reason,
            last_tried = unixepoch()
    """, (ceg_id, reason))
    conn.commit()

# ── ID generation ──────────────────────────────────────────────────────────────

def generate_ceg_ids():
    ids = []
    for county, form in product(COUNTY_CODES, FORM_CODES):
        # We start with a smaller range to be realistic, then expand
        for serial in range(1, 100_000):
            ids.append(f"{county}{form}{serial:06d}")
    
    random.shuffle(ids)  # Shuffling is key for stealth
    for ceg_id in ids:
        yield ceg_id

def ceg_id_to_url(ceg_id: str) -> str:
    return f"{BASE_URL}/ceg-c{ceg_id}.html"

# ── Parsing ────────────────────────────────────────────────────────────────────

def parse_company(ceg_id: str, url: str, html: str) -> Company | str | None:
    """
    Returns Company object on success, "BLOCKED" if blocked, or None if invalid.
    """
    text = extract(
        html,
        config=traf_cfg,
        include_tables=True,
        include_links=False,
        no_fallback=False,
        favor_recall=True,
    )
    if not text:
        return None

    # Bot detection check
    if "Szokatlan forgalmat" in text or "igazolja, hogy nem robot" in text:
        return "BLOCKED"

    # Check for "not found" / search result page
    if any(s in text for s in ["Az oldal nem található", "404", "nincs találat", "Találati oldal"]):
        return None

    def f(pattern, default=""):
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if not m: return default
        val = m.group(1).strip()
        labels = ["Rövidített név", "Teljes név", "Alapítás éve", "Adószám", "Főtevékenység", 
                  "székhely", "telephelyek száma", "létszám", "Pozitív információk", 
                  "Negatív információk", "Cégjegyzésre jogosultak", "üzletkötési javaslat"]
        if any(l in val for l in labels) and len(val) < 50:
            return default
        return val

    name = f(r"^#\s+(.+)$")
    if not name:
        name = text.split("\n")[0].strip()

    def m(pattern, default=""):
        labels = ["Rövidített név", "Teljes név", "Alapítás éve", "Adószám", "Főtevékenység", 
                  "székhely", "telephelyek száma", "létszám", "Pozitív információk", 
                  "Negatív információk", "Cégjegyzésre jogosultak", "üzletkötési javaslat",
                  "Határon túli", "All in", "Privát cégelemzés"]
        lookahead = "|".join([re.escape(l) for l in labels])
        full_pattern = f"{pattern}\\s*\\n([\\s\\S]+?)(?=\\n(?:{lookahead})|$)"
        match = re.search(full_pattern, text, re.IGNORECASE)
        if not match: return default
        return match.group(1).strip()

    return Company(
        ceg_id           = ceg_id,
        url              = url,
        name             = name,
        short_name       = f(r"Rövidített név\s*\n(.+)"),
        full_name        = f(r"Teljes név\s*\n(.+)"),
        founded          = f(r"Alapítás éve\s*\n(\d{4})"),
        tax_id           = f(r"Adószám\s*\n([\d\-]+)"),
        main_activity    = f(r"Főtevékenység\s*\n(.+)"),
        address          = f(r"székhely\s*\n(.+)"),
        branch_count     = f(r"telephelyek száma\s*\n(\d+)"),
        headcount        = f(r"létszám\s*\n(.+)"),
        revenue          = f(r"nettó árbevétel[^\n]*\n(.+)"),
        registered_capital = f(r"jegyzett tőke[^\n]*\n(.+)"),
        profit_after_tax = f(r"adózott eredmény[^\n]*\n(.+)"),
        risk_ratio       = f(r"Magas kockázatú[^\n]*\n(.+?)\s*%"),
        positive_info    = m(r"Pozitív információk"),
        negative_info    = m(r"Negatív információk"),
        signatories      = m(r"Cégjegyzésre jogosultak"),
        status_info      = m(r"üzletkötési javaslat"),
        raw_text         = text[:4000],
    )

# ── HTTP client ────────────────────────────────────────────────────────────────

def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "hu-HU,hu;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": BASE_URL + "/",
        },
        timeout=30,
        follow_redirects=True,
        http2=True,
        limits=httpx.Limits(max_connections=CONCURRENCY + 2),
    )

# ── Worker ─────────────────────────────────────────────────────────────────────

async def worker(
    worker_id: int,
    queue: asyncio.Queue,
    client: httpx.AsyncClient,
    db_lock: asyncio.Lock,
    conn: sqlite3.Connection,
    stats: dict,
):
    global BLOCKED_UNTIL
    
    while True:
        ceg_id = await queue.get()
        try:
            # Check if we are in a cooldown period
            wait_block = BLOCKED_UNTIL - time.time()
            if wait_block > 0:
                log.info(f"[W{worker_id}] Global block active, sleeping {int(wait_block)}s")
                await asyncio.sleep(wait_block)

            async with db_lock:
                if already_done(conn, ceg_id):
                    stats["skipped"] += 1
                    continue

            url = ceg_id_to_url(ceg_id)
            
            # Rotate User-Agent per request for extra stealth
            client.headers["User-Agent"] = random.choice(USER_AGENTS)

            for attempt in range(3):
                try:
                    resp = await client.get(url)
                    break
                except Exception as e:
                    wait = 20 * (attempt + 1)
                    log.warning(f"[W{worker_id}] {ceg_id} error, retry in {wait}s: {e}")
                    await asyncio.sleep(wait)
            else:
                stats["failed"] += 1
                continue

            if resp.status_code == 429:
                log.warning(f"[W{worker_id}] 429 Rate Limited! Setting 10 min cooldown")
                BLOCKED_UNTIL = time.time() + 600
                continue

            if resp.status_code != 200:
                stats["failed"] += 1
                continue

            result = parse_company(ceg_id, str(resp.url), resp.text)

            if result == "BLOCKED":
                log.warning(f"[W{worker_id}] Bot detection triggered! Setting 15 min cooldown")
                BLOCKED_UNTIL = time.time() + 900
                continue

            if result is None:
                stats["not_found"] += 1
                continue

            async with db_lock:
                save_company(conn, result)
            stats["saved"] += 1

            if stats["saved"] % 100 == 0:
                log.info(f"Progress — saved: {stats['saved']} | not_found: {stats['not_found']} | failed: {stats['failed']}")

        finally:
            queue.task_done()
            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

# ── Orchestrator ───────────────────────────────────────────────────────────────

async def main():
    import sys
    
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    
    if len(sys.argv) > 1:
        ceg_id = sys.argv[1]
        log.info(f"Test mode: Scraping single entity {ceg_id}")
        async with make_client() as client:
            url = ceg_id_to_url(ceg_id)
            resp = await client.get(url)
            result = parse_company(ceg_id, str(resp.url), resp.text)
            if isinstance(result, Company):
                save_company(conn, result)
                print(f"Scraped & Saved: {result.name}")
            else:
                print(f"Result: {result}")
    else:
        db_lock = asyncio.Lock()
        stats = {"saved": 0, "not_found": 0, "failed": 0, "skipped": 0}
        queue: asyncio.Queue = asyncio.Queue(maxsize=CONCURRENCY * 10)

        async with make_client() as client:
            tasks = [asyncio.create_task(worker(i, queue, client, db_lock, conn, stats)) for i in range(CONCURRENCY)]
            for ceg_id in generate_ceg_ids():
                await queue.put(ceg_id)
            await queue.join()
            for t in tasks: t.cancel()
        
        log.info(f"Crawl finished. Stats: {stats}")

    # Export to CSV
    rows = conn.execute("SELECT * FROM companies").fetchall()
    col_names = [d[0] for d in conn.execute("SELECT * FROM companies LIMIT 0").description]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(col_names)
        w.writerows(rows)
    log.info(f"Exported to {CSV_PATH}")
    conn.close()

if __name__ == "__main__":
    asyncio.run(main())
