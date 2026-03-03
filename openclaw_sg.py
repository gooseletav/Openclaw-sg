import os
import re
import json
import time
import sqlite3
import hashlib
import logging
import requests
import feedparser
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openclaw")

BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
DB_PATH     = "openclaw_sg.db"
MIN_BEDS    = 4
MAX_RENT    = 4000
MIN_RENT    = 1800
TARGET_ZIP  = "91776"

APIFY_RUN_URL = "https://api.apify.com/v2/acts/tri_angle~real-estate-aggregator/run-sync-get-dataset-items"
APIFY_INPUT = {
    "location": TARGET_ZIP,
    "offerType": "rent",
    "providers": ["zillow", "realtor", "zumper", "apartments"],
    "maxResultsPerProvider": 50,
    "deduplicateResults": True,
}

KEYWORDS = [
    ("available now", 2), ("vacant", 2), ("must rent", 4),
    ("flexible lease", 2), ("immediate move", 2), ("move in ready", 1),
    ("price reduced", 4), ("reduced", 2), ("open to offers", 4),
    ("utilities included", 2), ("concession", 3), ("free month", 4),
    ("first month free", 4), ("negotiable", 4), ("flexible", 1),
    ("move in special", 3), ("motivated", 3), ("no deposit", 3),
    ("no fee", 2), ("below market", 4), ("owner motivated", 4),
]
_KW = [(re.compile(re.escape(k), re.I), w) for k, w in KEYWORDS]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id      TEXT UNIQUE,
            title           TEXT,
            rent_price      REAL,
            bedrooms        REAL,
            neighborhood    TEXT,
            url             TEXT,
            description     TEXT,
            source          TEXT,
            first_seen      TEXT,
            last_seen       TEXT,
            price_history   TEXT DEFAULT '[]',
            repost_count    INTEGER DEFAULT 0,
            distress_score  REAL DEFAULT 0,
            suggested_offer REAL,
            alerted         INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def upsert(listing):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    lid = listing["listing_id"]
    cur.execute("SELECT * FROM listings WHERE listing_id=?", (lid,))
    row = cur.fetchone()
    if row is None:
        ph = json.dumps([{"p": listing["rent_price"], "d": now}])
        cur.execute(
            "INSERT INTO listings (listing_id,title,rent_price,bedrooms,neighborhood,url,description,source,first_seen,last_seen,price_history,repost_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            (lid, listing["title"], listing["rent_price"], listing["bedrooms"],
             listing["neighborhood"], listing["url"], listing["description"],
             listing.get("source", "unknown"), now, now, ph),
        )
        conn.commit()
        conn.close()
        return {"is_new": True, "price_dropped": False, "drop_pct": 0, "repost_count": 0, "first_seen": now}
    ph = json.loads(row["price_history"] or "[]")
    rc = row["repost_count"] or 0
    old_p = row["rent_price"]
    new_p = listing["rent_price"]
    dropped = False
    drop_pct = 0.0
    if new_p and old_p and new_p < old_p:
        dropped = True
        drop_pct = ((old_p - new_p) / old_p) * 100
        ph.append({"p": new_p, "d": now})
    last = row["last_seen"] or ""
    if last[:10] != now[:10]:
        rc += 1
    cur.execute(
        "UPDATE listings SET last_seen=?,rent_price=?,price_history=?,repost_count=? WHERE listing_id=?",
        (now, new_p, json.dumps(ph), rc, lid),
    )
    conn.commit()
    conn.close()
    return {"is_new": False, "price_dropped": dropped, "drop_pct": drop_pct,
            "repost_count": rc, "first_seen": row["first_seen"]}


def mark_alerted(lid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE listings SET alerted=1 WHERE listing_id=?", (lid,))
    conn.commit()
    conn.close()


def was_alerted(lid):
    conn = sqlite3.connect(DB_PATH)
    r = conn.execute("SELECT alerted FROM listings WHERE listing_id=?", (lid,)).fetchone()
    conn.close()
    return bool(r and r[0])


def save_scores(lid, score, offer):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE listings SET distress_score=?,suggested_offer=? WHERE listing_id=?", (score, offer, lid))
    conn.commit()
    conn.close()


_PRICE_RE = re.compile(r"\$([0-9,]+)")
_BED_RE   = re.compile(r"(\d+)\s*(?:br|bed|bedroom)", re.I)


def _flt(pat, text):
    m = pat.search(text)
    return float(m.group(1).replace(",", "")) if m else None


def collect_apify():
    if not APIFY_TOKEN:
        log.warning("No APIFY_TOKEN set.")
        return []
    try:
        log.info("Calling Apify for zip %s...", TARGET_ZIP)
        resp = requests.post(
            APIFY_RUN_URL,
            params={"token": APIFY_TOKEN, "timeout": 120, "memory": 512},
            json=APIFY_INPUT,
            timeout=150,
        )
        resp.raise_for_status()
        raw = resp.json()
        results = []
        for item in raw:
            price = item.get("price") or item.get("rentZestimate") or item.get("rent")
            if isinstance(price, dict):
                price = float(list(price.values())[0])
            elif isinstance(price, str):
                price = _flt(_PRICE_RE, price)
            if not price or not (MIN_RENT <= float(price) <= MAX_RENT + 400):
                continue
            beds = item.get("bedrooms") or item.get("beds")
            try:
                if isinstance(beds, dict):
                    beds = float(list(beds.values())[0])
                else:
                    beds = float(beds)
            except Exception:
                beds = float(MIN_BEDS)
            if beds < MIN_BEDS:
                continue
            url   = (item.get("url") or item.get("detailUrl") or
                    item.get("hdpUrl") or item.get("listingUrl") or
                    item.get("permalink") or item.get("link") or "")
            log.info("ITEM KEYS: %s", list(item.keys()))
            title = item.get("streetAddress") or item.get("address") or "Rental"
            desc  = item.get("description") or item.get("homeDescription") or ""
            addr  = item.get("address", "")
            hood  = item.get("neighborhood") or item.get("city") or (addr.split(",")[-2].strip() if "," in addr else "San Gabriel")
            src_map = {"zillow": "zillow", "realtor": "realtor.com", "zumper": "zumper", "apartments": "apartments.com"}
            src   = src_map.get(item.get("provider", ""), item.get("provider", "apify"))
            lid   = hashlib.md5((url or title + str(price)).encode()).hexdigest()
            results.append({
                "listing_id":   lid,
                "title":        str(title).strip(),
                "rent_price":   float(price),
                "bedrooms":     beds,
                "neighborhood": str(hood).strip(),
                "url":          url,
                "description":  str(desc)[:600],
                "source":       src,
            })
        log.info("Apify: %d listings after filter", len(results))
        return results
    except Exception as ex:
        log.error("Apify error: %s", ex)
        return []


def kw_score(text):
    hits = []
    total = 0.0
    for pat, w in _KW:
        if pat.search(text):
            hits.append(pat.pattern)
            total += w
    return total, hits


def dom(first_seen):
    try:
        s = datetime.fromisoformat(first_seen)
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - s).days)
    except Exception:
        return 0


def distress_score(listing, meta):
    d         = dom(meta.get("first_seen", datetime.now(timezone.utc).isoformat()))
    drop_pct  = meta.get("drop_pct", 0)
    reposts   = meta.get("repost_count", 0)
    kw_w, kws = kw_score(listing.get("description", "") + " " + listing.get("title", ""))
    raw = (drop_pct * 3) + (d * 0.4) + kw_w + (reposts * 10)
    return min(raw, 100.0), {"dom": d, "drop_pct": drop_pct, "reposts": reposts,
                              "kw_weight": kw_w, "matched_keywords": kws}


def compute_offer(rent, score):
    if   score < 15: pct, st, pr = 1.00, "none",     0.10
    elif score < 30: pct, st, pr = 0.95, "LOW",      0.25
    elif score < 45: pct, st, pr = 0.90, "MODERATE", 0.40
    elif score < 60: pct, st, pr = 0.85, "MODERATE", 0.52
    elif score < 75: pct, st, pr = 0.82, "HIGH",     0.63
    elif score < 88: pct, st, pr = 0.78, "HIGH",     0.72
    else:            pct, st, pr = 0.75, "MAX",      0.80
    return round(rent * pct / 50) * 50, st, pr


def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print(msg)
        return
    try:
        r = requests.post(
            "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.error("Telegram error: %s", e)


def build_alert(listing, offer, strength, prob, score, details):
    src      = listing.get("source", "").upper()
    kw_line  = ""
    if details["matched_keywords"]:
        kw_line = "\nKeywords: " + ", ".join(details["matched_keywords"][:5])
    extras = ""
    if details["drop_pct"] > 0:
        extras += "\nPrice dropped " + str(round(details["drop_pct"], 1)) + "%"
    if details["reposts"] > 0:
        extras += "\nReposted " + str(details["reposts"]) + "x"
    dom_line = ("\nOn market: " + str(details["dom"]) + " days") if details["dom"] > 0 else ""
    savings  = int(listing["rent_price"] - offer)
    beds     = int(listing.get("bedrooms", MIN_BEDS))
    return (
        "OpenClaw-SG [" + src + "]\n\n"
        + str(listing["title"]) + "\n"
        + str(listing["neighborhood"]) + " - " + str(beds) + "BR\n"
        + "Asking: $" + str(int(listing["rent_price"])) + "/mo\n"
        + "Offer: $" + str(int(offer)) + "/mo (saves $" + str(savings) + "/mo)\n"
        + "Distress: " + str(int(score)) + "/100 | Leverage: " + strength + "\n"
        + "Acceptance est: " + str(int(prob * 100)) + "%"
        + dom_line + extras + kw_line + "\n\n"
        + str(listing["url"])
    )


def run():
    init_db()
    log.info("OpenClaw-SG starting...")
    all_listings = collect_apify()
    log.info("Total candidates: %d", len(all_listings))
    alerts = 0
    for listing in all_listings:
        meta = upsert(listing)
        if not meta["is_new"] and not meta["price_dropped"] and was_alerted(listing["listing_id"]):
            continue
        score, details = distress_score(listing, meta)
        offer, strength, prob = compute_offer(listing["rent_price"], score)
        save_scores(listing["listing_id"], score, offer)
        if listing["rent_price"] > MAX_RENT and score < 65:
            continue
        msg = build_alert(listing, offer, strength, prob, score, details)
        send_telegram(msg)
        mark_alerted(listing["listing_id"])
        alerts += 1
        time.sleep(0.4)
    log.info("Done. %d alerts sent.", alerts)


if __name__ == "__main__":
    run()
