"""
fetch_news.py
=============
Fetches renewable energy stories from a set of RSS/Atom feeds, classifies
each one (edition, category, region), and merges the results into
data/news.json -- the same file the site's frontend fetches at runtime.

Run manually:
    python3 scripts/fetch_news.py

In production this is run daily by a GitHub Action
(.github/workflows/news-radar.yml), which commits the updated
data/news.json back to the repo.

Output schema (one object per story) -- matches what the frontend expects:
{
  "title":   str,
  "summary": str,             # cleaned, truncated plain-text summary
  "link":    str,
  "source":  str,              # feed's display name, e.g. "pv magazine"
  "date":    "YYYY-MM-DD",
  "edition": "global" | "canada",
  "region":  str | int | None,  # province code (canada) or ISO 3166-1
                                 # numeric country code (global); None if
                                 # no clear region was detected in the text
  "category": str,             # Solar / Wind / Hydro / Storage / Tidal /
                                 # Research / Innovation
  "major":   bool               # true if the story trips the "major" heuristic
}
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "news.json"

# How many days of history to keep in data/news.json. Old entries roll off
# rather than growing the file forever -- see the portfolio's earlier
# decision that a single JSON file is fine up to a year or two of history.
RETENTION_DAYS = 730

# How many entries to pull from each feed per run. Feeds are usually
# checked daily, so this is generous headroom, not an expected volume.
MAX_ENTRIES_PER_FEED = 20


# =============================================================================
# FEED LIST
# Each feed gets a default edition ("global" or "canada") and, optionally, a
# default category -- used when the feed is already category-specific (e.g.
# CleanTechnica's wind-only feed). Feeds without a fixed category (general
# newsrooms) pass category=None and rely entirely on keyword classification.
#
# Add more feeds here as they're found. Candidates not yet wired up:
# Windpower Engineering, Renewables Now, CanREA, Renewable Energy World.
# =============================================================================
FEEDS = [
    {"url": "https://pv-magazine.com/feed", "source": "pv magazine", "edition": "global", "category": "Solar"},
    {"url": "https://cleantechnica.com/category/wind-energy/feed/", "source": "CleanTechnica", "edition": "global", "category": "Wind"},
    {"url": "https://cleantechnica.com/category/solar-energy/feed/", "source": "CleanTechnica", "edition": "global", "category": "Solar"},
    {"url": "https://www.energy-storage.news/feed", "source": "Energy Storage News", "edition": "global", "category": "Storage"},
    {"url": "https://www.theguardian.com/environment/renewableenergy/rss", "source": "The Guardian", "edition": "global", "category": None},
    {
        "url": "https://api.io.canada.ca/io-server/gc/news/en/v2?dept=naturalresourcescanada&sort=publishedDate&orderBy=desc&publishedDate%3E=2021-07-23&pick=50&format=atom&atomtitle=Natural%20Resources%20Canada",
        "source": "Natural Resources Canada",
        "edition": "canada",
        "category": None,
    },
    {"url": "https://electricautonomy.ca/feed", "source": "Electric Autonomy Canada", "edition": "canada", "category": None},
]

# Government/general newsroom feeds carry a lot of non-energy stories (jobs,
# unrelated policy, etc.) -- for feeds without a fixed category, require at
# least one of these words to appear before keeping the story at all.
GENERAL_FEED_ENERGY_GATE = [
    "energy", "renewable", "solar", "wind", "hydro", "electric", "power",
    "grid", "battery", "storage", "turbine", "geothermal", "tidal",
]


# =============================================================================
# CLASSIFICATION
# NOTE: PROVINCE_KEYWORDS / COUNTRY_KEYWORDS intentionally mirror the
# PROVINCE_NAMES / COUNTRY_NAMES tables in the frontend's <script> block.
# If you add a country/province there, add its keywords here too.
# =============================================================================

# Checked in order -- niche/Innovation terms first so e.g. "enhanced
# geothermal" isn't swallowed by a generic "geothermal" match elsewhere.
CATEGORY_KEYWORDS = [
    ("Innovation", [
        "piezoelectric", "kinetic energy harvesting", "osmotic", "salinity gradient",
        "wave energy", "enhanced geothermal", "algae", "biofuel", "biogas",
        "space-based solar", "blue energy", "floating solar", "perovskite",
    ]),
    ("Tidal", ["tidal"]),
    ("Hydro", ["hydro", "hydroelectric", "run-of-river", "dam "]),
    ("Storage", ["battery", "batteries", "storage", "bess"]),
    ("Wind", ["wind farm", "wind turbine", "offshore wind", "onshore wind", " wind "]),
    ("Solar", ["solar", "photovoltaic", " pv "]),
]

PROVINCE_KEYWORDS = {
    "BC": ["british columbia", "bc hydro"],
    "AB": ["alberta"],
    "SK": ["saskatchewan"],
    "MB": ["manitoba"],
    "ON": ["ontario", "ieso"],
    "QC": ["quebec", "québec"],
    "NB": ["new brunswick"],
    "NS": ["nova scotia"],
    "PE": ["prince edward island"],
    "NL": ["newfoundland"],
}

COUNTRY_KEYWORDS = {
    840: ["united states", "u.s.", "usa", "america"],
    826: ["united kingdom", "u.k.", "britain", "england", "scotland", "wales"],
    276: ["germany", "german "],
    156: ["china", "chinese"],
    356: ["india", "indian "],
    36: ["australia", "australian"],
    76: ["brazil", "brazilian"],
    392: ["japan", "japanese"],
    724: ["spain", "spanish"],
    528: ["netherlands", "dutch"],
    352: ["iceland", "icelandic"],
    620: ["portugal", "portuguese"],
}

MAJOR_KEYWORDS = [
    "record", "largest", "first-ever", "billion", "approved", "commissioned",
    "auction", "milestone", "government", "world's largest", "landmark",
]


def classify_category(text, fallback):
    lower = f" {text.lower()} "
    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return category
    return fallback or "Research"


def classify_region(text, edition):
    lower = text.lower()
    table = PROVINCE_KEYWORDS if edition == "canada" else COUNTRY_KEYWORDS
    for code, keywords in table.items():
        if any(kw in lower for kw in keywords):
            return code
    return None


def is_major(text):
    lower = text.lower()
    hits = sum(1 for kw in MAJOR_KEYWORDS if kw in lower)
    return hits >= 2


def passes_energy_gate(text, feed):
    if feed["category"] is not None:
        return True  # feed is already energy-specific by construction
    lower = text.lower()
    return any(kw in lower for kw in GENERAL_FEED_ENERGY_GATE)


# =============================================================================
# FETCHING
# =============================================================================

def clean_summary(raw_html, max_len=220):
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)  # strip leftover HTML entities
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0].rstrip(",.;:") + "..."
    return text


def parse_entry_date(entry):
    for field in ("published_parsed", "updated_parsed"):
        value = getattr(entry, field, None)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc).date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()


def fetch_feed(feed):
    """Fetches and classifies entries from one feed. Never raises -- a
    broken feed should not take down the whole run."""
    results = []
    try:
        parsed = feedparser.parse(feed["url"])
    except Exception as exc:  # pragma: no cover -- defensive, feedparser is usually quiet
        print(f"  ! {feed['source']}: request failed ({exc})", file=sys.stderr)
        return results

    if not parsed.entries:
        reason = getattr(parsed, "bozo_exception", "no entries returned")
        print(f"  ! {feed['source']}: nothing parsed ({reason})", file=sys.stderr)
        return results

    for entry in parsed.entries[:MAX_ENTRIES_PER_FEED]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        if not (link.startswith("http://") or link.startswith("https://")):
            print(f"  ! {feed['source']}: skipping entry with non-absolute link ({link!r})", file=sys.stderr)
            continue

        summary = clean_summary(entry.get("summary") or entry.get("description") or "")
        combined_text = f"{title} {summary}"

        if not passes_energy_gate(combined_text, feed):
            continue

        edition = feed["edition"]
        results.append({
            "title": title,
            "summary": summary,
            "link": link,
            "source": feed["source"],
            "date": parse_entry_date(entry),
            "edition": edition,
            "region": classify_region(combined_text, edition),
            "category": classify_category(combined_text, feed["category"]),
            "major": is_major(combined_text),
        })

    print(f"  {feed['source']}: kept {len(results)} of {len(parsed.entries)} entries")
    return results


def fetch_all():
    collected = []
    for feed in FEEDS:
        print(f"Fetching {feed['source']} ({feed['url']}) ...")
        collected.extend(fetch_feed(feed))
    return collected


# =============================================================================
# MERGE + PERSIST
# =============================================================================

def load_existing():
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"! {DATA_PATH} was not valid JSON -- starting fresh", file=sys.stderr)
    return []


def merge(existing, new_items):
    # Keyed by link so re-fetching the same story (possibly with refined
    # classification, since keyword rules may improve over time) replaces
    # the old copy rather than duplicating it.
    by_link = {item["link"]: item for item in existing}
    for item in new_items:
        by_link[item["link"]] = item
    merged = list(by_link.values())
    merged.sort(key=lambda s: s["date"], reverse=True)
    return merged


def trim_old(items):
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS)).isoformat()
    return [item for item in items if item["date"] >= cutoff]


def main():
    existing = load_existing()
    new_items = fetch_all()
    merged = trim_old(merge(existing, new_items))

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\nWrote {len(merged)} total stories ({len(new_items)} fetched this run) to {DATA_PATH}")
    by_edition = {}
    for item in merged:
        by_edition[item["edition"]] = by_edition.get(item["edition"], 0) + 1
    print(f"By edition: {by_edition}")


if __name__ == "__main__":
    main()
