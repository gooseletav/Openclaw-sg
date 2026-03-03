"""
OpenClaw-SG v2 — Multi-source rental bot for San Gabriel area (91776 ±10mi)
Sources: Craigslist RSS (free) + Apify Real Estate Aggregator (Zillow, Apartments.com, Sends Telegram alerts with distress score + dynamic lowball offer.
Zumper
Setup:
pip install requests feedparser python-dotenv
.env file:
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
APIFY_TOKEN=your_apify_token # free at apify.com — no credit card needed to start
Run once: python openclaw_sg.py
Run on cron: 0 */2 * * * cd /path/to && python openclaw_sg.py >> openclaw.log 2>&1
"""
import os, re, json, time, sqlite3, hashlib, logging, requests, feedparser
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("openclaw")
# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
APIFY_TOKEN= os.getenv("APIFY_TOKEN", "")
DB_PATH = "openclaw_sg.db"
MIN_BEDS = 4
MAX_RENT = 4000
MIN_RENT = 1800
TARGET_ZIP = "91776" # San Gabriel — Apify uses zip for best accuracy
# Craigslist SGV RSS (free, no key needed)
RSS_FEEDS = [
"https://losangeles.craigslist.org/search/sgv/apa?format=rss&min_bedrooms=4&max_price=420
"https://losangeles.craigslist.org/search/sgv/roo?format=rss&min_bedrooms=4&max_price=420
"https://losangeles.craigslist.org/search/lac/apa?format=rss&min_bedrooms=4&max_price=420
]
# Apify — Real Estate Aggregator actor
# Pulls from Zillow, Apartments.com, Zumper, Realtor.com in one call
APIFY_ACTOR = "tri_angle/real-estate-aggregator"
APIFY_RUN_URL = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
APIFY_INPUT = {
"location": TARGET_ZIP,
"offerType": "rent",
"providers": ["zillow", "realtor", "zumper", "apartments"],
"maxResultsPerProvider": 50,
"deduplicateResults": True,
}
# Distress keyword weights
KEYWORDS = [
("available now", 2), ("vacant", 2), ("must rent", 4),
("flexible lease", 2), ("immediate move", 2), ("move in ready", 1),
("price reduced", 4), ("reduced", 2), ("open to offers", 4),
("utilities included",2),("concession", 3), ("free month", 4),
("first month free", 4),("negotiable", 4), ("flexible", 1),
("move in special", 3), ("motivated", 3), ("no deposit", 3),
("no fee", 2), ("below market", 4), ("owner motivated", 4),
("must see", 1), ("wont last", 2), ("priced to rent", 3),
]
_KW = [(re.compile(re.escape(k), re.I), w) for k, w in KEYWORDS]
# ── Database ───────────────────────────────────────────────────────────────────
def init_db():
conn = sqlite3.connect(DB_PATH)
conn.execute("""
CREATE TABLE IF NOT EXISTS listings (
id INTEGER PRIMARY KEY AUTOINCREMENT,
listing_id TEXT UNIQUE,
title TEXT,
rent_price REAL,
bedrooms REAL,
neighborhood TEXT,
url TEXT,
description TEXT,
source TEXT,
first_seen TEXT,
last_seen TEXT,
price_history TEXT DEFAULT '[]',
repost_count INTEGER DEFAULT 0,
distress_score REAL DEFAULT 0,
suggested_offer REAL,
alerted INTEGER DEFAULT 0
)
""")
conn.commit(); conn.close()
def upsert(listing: dict) -> dict:
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
now = datetime.now(timezone.utc).isoformat()
lid = listing["listing_id"]
cur.execute("SELECT * FROM listings WHERE listing_id=?", (lid,))
row = cur.fetchone()
if row is None:
ph = json.dumps([{"p": listing["rent_price"], "d": now}])
cur.execute("""
INSERT INTO listings
(listing_id,title,rent_price,bedrooms,neighborhood,url,description,source,first
VALUES (?,?,?,?,?,?,?,?,?,?,?,0)
""", (lid, listing["title"], listing["rent_price"], listing["bedrooms"],
listing["neighborhood"], listing["url"], listing["description"],
listing.get("source","unknown"), now, now, ph))
conn.commit(); conn.close()
return {"is_new": True, "price_dropped": False, "drop_pct": 0,
"repost_count": 0, "first_seen": now}
ph = json.loads(row["price_history"] or "[]")
rc = row["repost_count"] or 0
old_p, new_p = row["rent_price"], listing["rent_price"]
dropped, drop_pct = False, 0.0
if new_p and old_p and new_p < old_p:
dropped = True
drop_pct = ((old_p - new_p) / old_p) * 100
ph.append({"p": new_p, "d": now})
last = row["last_seen"] or ""
if last[:10] != now[:10]:
rc += 1
cur.execute("""
UPDATE listings SET last_seen=?,rent_price=?,price_history=?,repost_count=?
WHERE listing_id=?
""", (now, new_p, json.dumps(ph), rc, lid))
conn.commit(); conn.close()
return {"is_new": False, "price_dropped": dropped, "drop_pct": drop_pct,
"repost_count": rc, "first_seen": row["first_seen"]}
def mark_alerted(lid):
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE listings SET alerted=1 WHERE listing_id=?", (lid,))
conn.commit(); conn.close()
def was_alerted(lid):
conn = sqlite3.connect(DB_PATH)
r = conn.execute("SELECT alerted FROM listings WHERE listing_id=?", (lid,)).fetchone()
conn.close()
return bool(r and r[0])
def save_scores(lid, score, offer):
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE listings SET distress_score=?,suggested_offer=? WHERE listing_id=?",
conn.commit(); conn.close()
# ── Collectors ─────────────────────────────────────────────────────────────────
_PRICE_RE = re.compile(r"\$([0-9,]+)")
_BED_RE = re.compile(r"(\d+)\s*(?:br|bed|bedroom)", re.I)
_HOOD_RE = re.compile(r"\(([^)]{3,30})\)")
def _flt(pat, text):
m = pat.search(text)
return float(m.group(1).replace(",","")) if m else None
def collect_craigslist() -> list:
results = []
for url in RSS_FEEDS:
try:
feed = feedparser.parse(url)
for e in feed.entries:
title = e.get("title","").strip()
summary = re.sub(r"<[^>]+>","", e.get("summary","") or "")
link = e.get("link","")
full = title + " " + summary
rent = _flt(_PRICE_RE, title) or _flt(_PRICE_RE, summary)
if not rent or not (MIN_RENT <= rent <= MAX_RENT + 400):
continue
beds = _flt(_BED_RE, full)
if beds and beds < MIN_BEDS:
continue
hm = _HOOD_RE.search(title)
hood = hm.group(1).strip() if hm else "SGV"
results.append({
"listing_id": hashlib.md5(link.encode()).hexdigest(),
"title": title,
"rent_price": rent,
"bedrooms": beds or float(MIN_BEDS),
"neighborhood": hood,
"url": link,
"description": summary.strip()[:600],
"source": "craigslist",
})
except Exception as ex:
log.error("Craigslist feed error %s: %s", url, ex)
log.info("Craigslist: %d listings", len(results))
return results
def collect_apify() -> list:
"""
Call Apify Real Estate Aggregator — hits Zillow, Apartments.com, Zumper, Realtor.com.
Returns normalized listing dicts.
"""
if not APIFY_TOKEN:
return []
log.warning("No APIFY_TOKEN set — skipping Zillow/Apartments/Zumper/Realtor sources."
try:
log.info("Calling Apify aggregator for zip %s...", TARGET_ZIP)
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
# Normalize Apify's unified schema
price = item.get("price") or item.get("rentZestimate") or item.get("rent")
if isinstance(price, str):
price = _flt(_PRICE_RE, price)
if not price or not (MIN_RENT <= float(price) <= MAX_RENT + 400):
continue
beds = item.get("bedrooms") or item.get("beds")
try: beds = float(beds)
except: beds = float(MIN_BEDS)
if beds < MIN_BEDS:
continue
url = (item.get("url") or item.get("detailUrl") or
item.get("zillow") or item.get("realtor") or
item.get("zumper") or item.get("apartments") or "")
title = item.get("streetAddress") or item.get("address") or item.get("name") or "
desc = item.get("description") or item.get("homeDescription") or ""
hood = (item.get("neighborhood") or item.get("city") or
item.get("address","").split(",")[-2].strip() if "," in item.get("addres
source_map = {"zillow":"zillow","realtor":"realtor.com","zumper":"zumper","apartm
src = source_map.get(item.get("provider",""), item.get("provider","apify"))
lid = hashlib.md5((url or title + str(price)).encode()).hexdigest()
results.append({
"listing_id": lid,
"title": str(title).strip(),
"rent_price": float(price),
"bedrooms": beds,
"neighborhood": str(hood).strip(),
"url": url,
"description": str(desc)[:600],
"source": src,
})
log.info("Apify aggregator: %d listings after filter", len(results))
return results
except Exception as ex:
log.error("Apify error: %s", ex)
return []
# ── Scoring ────────────────────────────────────────────────────────────────────
def kw_score(text: str) -> tuple:
hits, total = [], 0.0
for pat, w in _KW:
if pat.search(text):
hits.append(pat.pattern); total += w
return total, hits
def dom(first_seen: str) -> int:
try:
s = datetime.fromisoformat(first_seen)
if s.tzinfo is None: s = s.replace(tzinfo=timezone.utc)
return max(0, (datetime.now(timezone.utc) - s).days)
except: return 0
def distress_score(listing: dict, meta: dict) -> tuple:
d = dom(meta.get("first_seen", datetime.now(timezone.utc).isoformat()))
drop_pct = meta.get("drop_pct", 0)
reposts = meta.get("repost_count", 0)
kw_w, kws = kw_score(listing.get("description","") + " " + listing.get("title",""))
raw = (drop_pct * 3) + (d * 0.4) + kw_w + (reposts * 10)
return min(raw, 100.0), {"dom": d, "drop_pct": drop_pct, "reposts": reposts,
"kw_weight": kw_w, "matched_keywords": kws}
def compute_offer(rent: float, score: float) -> tuple:
if score < 15: pct, st, pr = 1.00, "—", elif score < 30: pct, st, pr = 0.95, "LOW", elif score < 45: pct, st, pr = 0.90, "MODERATE", elif score < 60: pct, st, pr = 0.85, "MODERATE", elif score < 75: pct, st, pr = 0.82, "HIGH", elif score < 88: pct, st, pr = 0.78, "HIGH", 0.10
0.25
0.40
0.52
0.63
0.72
else: pct, st, pr = 0.75, " MAX", 0.80
return round(rent * pct / 50) * 50, st, pr
# ── Telegram ───────────────────────────────────────────────────────────────────
SOURCE_EMOJI = {
"zillow": " ", "apartments.com": " ", "zumper": " ",
"realtor.com": " ", "craigslist": " ",
}
def send_telegram(msg: str):
if not BOT_TOKEN or not CHAT_ID:
print("\n" + msg + "\n"); return
try:
r = requests.post(
f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML",
"disable_web_page_preview": False},
timeout=10,
)
r.raise_for_status()
except Exception as e:
log.error("Telegram error: %s", e)
def build_alert(listing: dict, offer: float, strength: str, prob: float,
score: float, details: dict) -> str:
stars = " " * min(5, max(1, int(score / 20)))
src_e = SOURCE_EMOJI.get(listing.get("source",""), " ")
kw_line = ""
if details["matched_keywords"]:
kw_line = "\n <i>" + ", ".join(details["matched_keywords"][:5]) + "</i>"
extras = ""
if details["drop_pct"] > 0:
extras += f"\n Price dropped <b>{details['drop_pct']:.1f}%</b>"
if details["reposts"] > 0:
extras += f"\n Reposted <b>{details['reposts']}x</b>"
dom_line = f"\n On market: <b>{details['dom']} days</b>" if details["dom"] > 0 else ""
savings = int(listing["rent_price"] - offer)
return f""" <b>OpenClaw-SG</b> {stars} {src_e} {listing.get('source','').upper()}
<b>{listing['title']}</b>
{listing['neighborhood']} · {listing.get('bedrooms','?'):.0f}BR
Asking: <b>${listing['rent_price']:,.0f}/mo</b>
Offer: <b>${offer:,.0f}/mo</b> <i>(saves ${savings:,}/mo)</i>
Distress: <b>{score:.0f}/100</b> · Leverage: {strength}
Acceptance est: <b>{prob*100:.0f}%</b>{dom_line}{extras}{kw_line}
<a href="{listing['url']}">View Listing</a>"""
# ── Main ───────────────────────────────────────────────────────────────────────
def run():
init_db()
log.info("━━━ OpenClaw-SG v2 starting ━━━")
all_listings = collect_craigslist() + collect_apify()
log.info("Total candidates: %d", len(all_listings))
alerts = 0
for listing in all_listings:
meta = upsert(listing)
# Skip already-alerted + unchanged listings
if not meta["is_new"] and not meta["price_dropped"] and was_alerted(listing["listing_
continue
score, details = distress_score(listing, meta)
offer, strength, prob = compute_offer(listing["rent_price"], score)
save_scores(listing["listing_id"], score, offer)
# Skip over-budget unless distress is very high (landlord might flex on price)
if listing["rent_price"] > MAX_RENT and score < 65:
continue
msg = build_alert(listing, offer, strength, prob, score, details)
send_telegram(msg)
mark_alerted(listing["listing_id"])
alerts += 1
time.sleep(0.4)
log.info("━━━ Done. %d alert(s) sent. ━━━", alerts)
if __name__ == "__main__":
run()
