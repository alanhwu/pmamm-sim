"""Build manifest.json from trade files + CSV market data.

Usage:
    python build_manifest.py [--dry-run]
"""

import csv
import json
import os
import re
import sys


def categorize(question: str, slug: str) -> str:
    """Classify a market question into a category."""
    q = question.lower()
    s = slug.lower()

    # Sports — team vs team, leagues, championships
    sports_patterns = [
        r"\bvs\.?\b",           # "vs" or "vs."
        r"\bnba\b", r"\bnfl\b", r"\bnhl\b", r"\bmlb\b", r"\bmls\b",
        r"\bepl\b", r"\bla liga\b", r"\bserie a\b", r"\bbundesliga\b", r"\bligue 1\b",
        r"\bchampions league\b", r"\buefa\b", r"\beuropa league\b",
        r"\bworld cup\b", r"\bworld series\b", r"\bsuper bowl\b",
        r"\bpremier league\b",
        r"\bwin the\b.*\b(finals|championship|cup|title|series|bowl)\b",
        r"\bwin on \d{4}-\d{2}-\d{2}\b",  # "will X win on 2025-01-18?"
        r"\bboxing\b", r"\bufc\b", r"\bmma\b", r"\bfight\b",
        r"\bf1\b", r"\bformula [12]\b", r"\bgrand prix\b", r"\bdrivers champion\b",
        r"\batp\b", r"\bwta\b", r"\btennism\b", r"\bgrand slam\b",
        r"\bcs2\b", r"\besports\b", r"\blol\b", r"\bdota\b", r"\bvalorant\b",
        r"\ble mans\b", r"\bnascar\b", r"\bindycar\b",
    ]
    # Slug-based sports patterns
    sports_slug_patterns = [
        r"^(nfl|nba|nhl|mlb|cfb|epl|mls)-",
        r"^atp-", r"^wta-",
        r"^cs2-", r"^lol-", r"^valorant-",
        r"-vs-", r"-(spread|over-under|total)-",
    ]
    for pat in sports_patterns:
        if re.search(pat, q):
            return "sports"
    for pat in sports_slug_patterns:
        if re.search(pat, s):
            return "sports"

    # Price / crypto / commodities / tech
    price_patterns = [
        r"\bbitcoin\b", r"\bbtc\b", r"\bethereum\b", r"\beth\b",
        r"\bsolana\b", r"\bsol\b", r"\bdogecoin\b", r"\bdoge\b",
        r"\bxrp\b", r"\bcrypto\b",
        r"\bhit \$", r"\babove \$", r"\bbelow \$", r"\breach \$",
        r"\bprice\b.*\$",
        r"\bmarket cap\b",
        r"\bgold\b.*\$", r"\bsilver\b.*\$", r"\boil\b.*\$",
        r"\bsilver \(si\)", r"\bgold \(gc\)",
        r"\bs&p\b", r"\bnasdaq\b", r"\bdow\b",
        r"\bup or down\b",
        r"\bairdrop\b", r"\bpublic sale\b", r"\bcommitted to\b",
        r"\btesla\b", r"\bfull self driving\b", r"\bfsd\b",
        r"\bmonad\b",
    ]
    price_slug_patterns = [
        r"bitcoin", r"btc-", r"ethereum", r"eth-",
        r"solana", r"sol-", r"doge", r"xrp",
    ]
    for pat in price_patterns:
        if re.search(pat, q):
            return "price"
    for pat in price_slug_patterns:
        if re.search(pat, s):
            return "price"

    # Politics / geopolitics / elections / government
    politics_patterns = [
        r"\belection\b", r"\bpresident\b", r"\bsenate\b", r"\bhouse\b",
        r"\bcongress\b", r"\bgovernor\b", r"\bmayor\b", r"\bminister\b",
        r"\bvote\b", r"\bballot\b", r"\bnominate\b",
        r"\bparty\b.*\b(win|lead|majority)\b",
        r"\bdemocrat\b", r"\brepublican\b", r"\bgop\b",
        r"\btrump\b", r"\bbiden\b", r"\bharris\b",
        r"\bceasefire\b", r"\bwar\b", r"\binvasion\b", r"\bstrike\b",
        r"\bsanction\b", r"\btariff\b", r"\btrade war\b",
        r"\bnato\b", r"\bun\b", r"\beu\b",
        r"\bimpeach\b", r"\bresign\b", r"\bout as\b", r"\bout by\b",
        r"\bsupreme court\b", r"\bcourt\b",
        r"\bfed\b.*\b(rate|chair|cut|hike|decrease|increase|change)\b",
        r"\bfed decreases\b", r"\bfed increases\b", r"\bno change in fed\b",
        r"\binterest rate\b",
        r"\bgovernment\b", r"\bcabinet\b", r"\bparliament\b",
        r"\bannex\b", r"\binvade\b",
        r"\bshutdown\b",
        r"\bmaduro\b", r"\bepstein\b", r"\bcustody\b",
        r"\bnormalize relations\b", r"\bstrikes?\b.*\biran\b",
        r"\bisrael\b.*\b(strike|attack|bomb)\b",
        r"\biranian regime\b", r"\bstrait of hormuz\b",
        r"\banti-cartel\b", r"\bground operation\b",
        r"\bpeace plan\b", r"\bmilitary engagement\b",
        r"\bu\.?s\.?\b.*\b(forces|strikes?)\b.*\b(venezuela|iran|mexico)\b",
        r"\bweed\b.*\brescheduled\b",
        r"\bdrop out\b", r"\bfirst leader out\b",
        r"\baliens\b.*\bexist\b",
        r"\bstate of the union\b",
    ]
    for pat in politics_patterns:
        if re.search(pat, q):
            return "politics"

    # Pop culture / social media / entertainment / tech
    pop_patterns = [
        r"\btweet\b", r"\btweets\b", r"\btweeting\b",
        r"\byoutube\b", r"\btiktok\b", r"\binstagram\b",
        r"\bsubscriber\b", r"\bfollower\b",
        r"\boscar\b", r"\bemmy\b", r"\bgrammy\b",
        r"\bmovie\b", r"\bfilm\b", r"\bbox office\b",
        r"\balbum\b", r"\bsong\b", r"\bspotify\b",
        r"\bmusk\b.*\btweet\b",
        r"\belon\b.*\btweet\b",
        r"\bgame of the year\b", r"\bgame awards\b",
        r"\btime.s person of the year\b",
        r"\bsearched person on google\b",
        r"\brelationship\b.*\bconfirmed\b", r"\bconfirmed relationship\b",
        r"\bmrbeast\b",
        r"\bai model\b", r"\bfrontier model\b", r"\btop ai\b",
        r"\bopenai\b", r"\bgemini\b", r"\bmistral\b",
        r"\blaunch a token\b", r"\bbase launch\b",
        r"\binsider trading\b",
    ]
    for pat in pop_patterns:
        if re.search(pat, q):
            return "pop_culture"

    return "other"


def get_outcome(outcome_prices: list[str]) -> int | None:
    """Determine outcome from outcome_prices. Returns 0 (NO) or 1 (YES), or None if unresolved."""
    try:
        yes_price = float(outcome_prices[0])
        no_price = float(outcome_prices[1])
    except (ValueError, IndexError):
        return None

    if yes_price > 0.5:
        return 1
    elif no_price > 0.5:
        return 0
    return None


def main():
    dry_run = "--dry-run" in sys.argv

    # Load CSV
    csv_markets = {}
    with open("polymarket_top_2000_markets.csv") as f:
        for row in csv.DictReader(f):
            csv_markets[row["slug"]] = row

    # Get all trade files
    trade_files = sorted(f for f in os.listdir("data") if f.startswith("trades_") and f.endswith(".json"))

    # Existing manifest entries (preserve manually-set fields)
    existing = {}
    if os.path.exists("data/manifest.json"):
        with open("data/manifest.json") as f:
            for m in json.load(f)["markets"]:
                existing[m["file"]] = m

    manifest_markets = []
    skipped = []
    category_counts = {}

    for tf in trade_files:
        slug = tf[len("trades_"):-len(".json")]

        # Try CSV lookup
        csv_row = csv_markets.get(slug)

        if csv_row:
            prices = json.loads(csv_row["outcome_prices"])
            outcome = get_outcome(prices)
            question = csv_row["question"]
        elif tf in existing:
            # Fall back to existing manifest entry
            outcome = existing[tf]["outcome"]
            question = existing[tf]["question"]
        else:
            skipped.append((tf, "no CSV match and not in existing manifest"))
            continue

        if outcome is None:
            skipped.append((tf, "unresolved market"))
            continue

        category = categorize(question, slug)
        category_counts[category] = category_counts.get(category, 0) + 1

        # Get trade count
        try:
            with open(os.path.join("data", tf)) as f:
                trades = json.load(f)
            trade_count = len(trades)
        except Exception:
            trade_count = 0

        manifest_markets.append({
            "file": tf,
            "outcome": outcome,
            "question": question,
            "category": category,
            "trades": trade_count,
        })

    # Sort by category then question
    manifest_markets.sort(key=lambda m: (m["category"], m["question"]))

    print(f"Markets: {len(manifest_markets)}")
    print(f"Skipped: {len(skipped)}")
    print(f"Categories: {json.dumps(category_counts, indent=2)}")
    print()

    if skipped:
        print("Skipped markets:")
        for tf, reason in skipped[:20]:
            print(f"  {tf}: {reason}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")
        print()

    if dry_run:
        print("Dry run — not writing manifest.json")
        # Show a sample per category
        for cat in sorted(category_counts.keys()):
            print(f"\n--- {cat} ({category_counts[cat]}) ---")
            for m in manifest_markets:
                if m["category"] == cat:
                    outcome_label = "YES" if m["outcome"] == 1 else "NO"
                    print(f"  [{outcome_label}] {m['question'][:80]}")
    else:
        with open("data/manifest.json", "w") as f:
            json.dump({"markets": manifest_markets}, f, indent=2)
        print(f"Written to data/manifest.json")


if __name__ == "__main__":
    main()
