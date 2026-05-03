"""
nemzeticegtar_scraper.py
------------------------
Scrapes all ~600k Hungarian companies from nemzeticegtar.hu
using direct ID enumeration + trafilatura for extraction.

Robots.txt: Disallow: (empty) → full crawl permitted.
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
CONCURRENCY = 8          # parallel workers — keep low to be polite
DELAY_MIN   = 1.5        # seconds between requests per worker
DELAY_MAX   = 3.5
DB_PATH     = Path("cegtar.db")
CSV_PATH    = Path("cegtar.csv")
LOG_PATH    = Path("cegtar.log")

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

# Trafilatura config — extract everything, no noise filtering
traf_cfg = use_config()
traf_cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "30")

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
    negative_info: str
    raw_text: str  # full trafilatura extract — keep for later parsing

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
            negative_info TEXT,
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
         registered_capital, profit_after_tax, risk_ratio, negative_info, raw_text)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
    """
    Yields all candidate cégjegyzékszám strings like '0109331320'.
    Format: CC (2) + FF (2) + NNNNNN (6) = 10 digits → URL suffix cCCFFNNNNNN
    Sequential numbers run 000001–999999 per county+form pair.
    In practice ~600k companies exist across all combinations.
    """
    for county, form in product(COUNTY_CODES, FORM_CODES):
        # Real max serial per pair varies; 999999 is safe upper bound.
        # In practice most pairs stop well before 100000.
        for serial in range(1, 999_999):
            yield f"{county}{form}{serial:06d}"

def ceg_id_to_url(ceg_id: str) -> str:
    """
    We don't know the slug (company name part), but the site accepts
    any slug as long as the ID suffix matches. Use a placeholder slug.
    """
    # The site resolves purely on the ID — the slug before `-c` is cosmetic.
    return f"{BASE_URL}/ceg-c{ceg_id}.html"

# ── Parsing ────────────────────────────────────────────────────────────────────

def _find(text: str, pattern: str) -> str:
    """Extract first capture group from trafilatura plain text."""
    m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip() if m else ""

def parse_company(ceg_id: str, url: str, html: str) -> Company | None:
    """
    Use trafilatura to extract clean text, then regex-parse the fields.
    trafilatura handles encoding, boilerplate removal, and Unicode normalization.
    """
    text = extract(
        html,
        config=traf_cfg,
        include_tables=True,
        include_links=False,
        no_fallback=False,
        favor_recall=True,   # keep more content, we'll filter ourselves
    )
    if not text:
        return None

    # Check for "not found" / deleted company pages
    if any(s in text for s in ["Az oldal nem található", "404", "nincs találat"]):
        return None

    def f(pattern): return _find(text, pattern)

    return Company(
        ceg_id           = ceg_id,
        url              = url,
        name             = f(r"^#\s+(.+)$"),                             # H1
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
        negative_info    = f(r"Negatív információk\s*\n(.+?)(?:\n\n|\Z)"),
        raw_text         = text[:4000],   # cap storage; full text in HTML if needed
    )

# ── HTTP client ────────────────────────────────────────────────────────────────

def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "hu-HU,hu;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": BASE_URL + "/",
        },
        timeout=20,
        follow_redirects=True,
        http2=True,       # HTTP/2 is more efficient and looks more browser-like
        limits=httpx.Limits(max_connections=CONCURRENCY + 4),
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
    while True:
        ceg_id = await queue.get()
        try:
            async with db_lock:
                if already_done(conn, ceg_id):
                    stats["skipped"] += 1
                    continue

            url = ceg_id_to_url(ceg_id)

            for attempt in range(4):
                try:
                    resp = await client.get(url)
                    break
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    wait = 10 * (attempt + 1)
                    log.warning(f"[W{worker_id}] {ceg_id} network error, retry in {wait}s: {e}")
                    await asyncio.sleep(wait)
            else:
                async with db_lock:
                    mark_failed(conn, ceg_id, "network_error")
                stats["failed"] += 1
                continue

            if resp.status_code == 404:
                # Company ID doesn't exist — expected for most IDs
                stats["not_found"] += 1
                continue

            if resp.status_code == 429:
                # Back off significantly
                log.warning(f"[W{worker_id}] Rate limited! Sleeping 120s")
                await asyncio.sleep(120)
                async with db_lock:
                    mark_failed(conn, ceg_id, "rate_limited")
                continue

            if resp.status_code != 200:
                async with db_lock:
                    mark_failed(conn, ceg_id, f"http_{resp.status_code}")
                stats["failed"] += 1
                continue

            company = parse_company(ceg_id, str(resp.url), resp.text)

            if company is None:
                stats["not_found"] += 1
                continue

            async with db_lock:
                save_company(conn, company)
            stats["saved"] += 1

            if stats["saved"] % 500 == 0:
                log.info(
                    f"Progress — saved: {stats['saved']} | "
                    f"not_found: {stats['not_found']} | "
                    f"failed: {stats['failed']}"
                )

        finally:
            queue.task_done()
            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

# ── Orchestrator ───────────────────────────────────────────────────────────────

async def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    db_lock = asyncio.Lock()
    stats = {"saved": 0, "not_found": 0, "failed": 0, "skipped": 0}

    queue: asyncio.Queue = asyncio.Queue(maxsize=CONCURRENCY * 4)

    async with make_client() as client:
        # Start workers
        tasks = [
            asyncio.create_task(
                worker(i, queue, client, db_lock, conn, stats)
            )
            for i in range(CONCURRENCY)
        ]

        # Feed IDs into the queue
        for ceg_id in generate_ceg_ids():
            await queue.put(ceg_id)

        await queue.join()

        for t in tasks:
            t.cancel()

    # Export to CSV
    log.info("Exporting to CSV…")
    rows = conn.execute("SELECT * FROM companies").fetchall()
    col_names = [d[0] for d in conn.execute("SELECT * FROM companies LIMIT 0").description]
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(col_names)
        w.writerows(rows)

    log.info(f"Done. {stats}")
    conn.close()

if __name__ == "__main__":
    asyncio.run(main())