#!/usr/bin/env python3
"""
Fetch all trade data for a Polymarket market and output a normalized,
simulation-ready trade sequence (all trades expressed in YES/USDC terms).

Uses the Dome API (https://docs.domeapi.io) for complete trade history
with cursor-based pagination (no offset cap).

Requires ``DOME_API_KEY`` (environment variable or entry in a root ``.env`` file).

Usage:
    python fetch_trades.py <event_url_or_slug> [--output-dir DIR]

Writes ``trades_<...>.json`` under ``data/`` by default (override with ``--output-dir``).

Example:
    python fetch_trades.py https://polymarket.com/event/khamenei-out-as-supreme-leader-of-iran-by-january-31
"""

import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# Load `.env` from the repository root (same directory as this script).
_REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(_REPO_ROOT / ".env")

# ── API config ──────────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
DOME_API = "https://api.domeapi.io/v1"

TRADE_PAGE_LIMIT = 1000  # Dome API max per request

# Default dump directory (keeps repo root clean and matches data/manifest.json convention)
DEFAULT_FETCH_OUT = _REPO_ROOT / "data"


def require_dome_api_key() -> str:
    """Dome API key from the environment or `.env` (never hardcode in source)."""
    key = (os.environ.get("DOME_API_KEY") or "").strip()
    if not key:
        print(
            "Error: DOME_API_KEY is not set.\n"
            "  Export it, or create a `.env` file in the project root (see `.env.example`).\n"
            "  https://docs.domeapi.io",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


# ── Step 1: look up the market ──────────────────────────────────────────────
def extract_event_slug(url_or_slug: str) -> str:
    """Pull the slug from a full URL or pass through a bare slug."""
    if url_or_slug.startswith("http"):
        path = urlparse(url_or_slug).path  # /event/<slug>
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "event":
            return parts[1]
        raise ValueError(f"Cannot parse event slug from URL: {url_or_slug}")
    return url_or_slug


def fetch_event(slug: str) -> dict:
    url = f"{GAMMA_API}/events/slug/{slug}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def pick_market(event_data: dict, slug: str) -> dict:
    """Select the single binary market that matches the event slug."""
    markets = event_data.get("markets", [])
    if not markets:
        raise ValueError("No markets found under this event")

    if len(markets) == 1:
        return markets[0]

    # Try matching by slug
    for m in markets:
        if m.get("slug", "") == slug or slug in m.get("slug", ""):
            return m

    print("[warn] Multiple markets found; picking the first one.")
    return markets[0]


def print_market_metadata(market: dict) -> None:
    clob_ids = json.loads(market.get("clobTokenIds", "[]"))
    outcomes = (
        json.loads(market.get("outcomes", "[]"))
        if isinstance(market.get("outcomes"), str)
        else market.get("outcomes", [])
    )
    outcome_prices = (
        json.loads(market.get("outcomePrices", "[]"))
        if isinstance(market.get("outcomePrices"), str)
        else market.get("outcomePrices", [])
    )

    print("=" * 80)
    print("MARKET METADATA")
    print("=" * 80)
    print(f"  Question     : {market.get('question', 'N/A')}")
    print(f"  Condition ID : {market.get('conditionId', 'N/A')}")
    print(f"  End Date     : {market.get('endDate', 'N/A')}")
    print(f"  Outcomes     : {outcomes}")
    print(f"  Prices       : {outcome_prices}")
    print(f"  CLOB tokens  : {clob_ids}")
    print(f"  Volume       : ${float(market.get('volume', 0)):,.2f}")
    print(f"  Closed       : {market.get('closed', 'N/A')}")
    print(f"  Active       : {market.get('active', 'N/A')}")
    print()


# ── Step 2: fetch all trades via Dome API ───────────────────────────────────
def _fetch_page(
    session: requests.Session,
    condition_id: str,
    pagination_key: str | None,
) -> dict:
    """Fetch a single page with retry logic for 429s and 5xx errors."""
    params: dict = {"condition_id": condition_id, "limit": TRADE_PAGE_LIMIT}
    if pagination_key:
        params["pagination_key"] = pagination_key

    for attempt in range(10):
        try:
            resp = session.get(
                f"{DOME_API}/polymarket/orders",
                params=params,
                timeout=60,
            )
        except requests.exceptions.RequestException as exc:
            wait = 0.5 * (2 ** attempt)
            print(f"    ⏳ network error ({exc}), retrying in {wait:.1f}s…")
            time.sleep(wait)
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            label = "rate-limited" if resp.status_code == 429 else f"HTTP {resp.status_code}"
            retry_after = float(resp.headers.get("Retry-After", 0))
            wait = max(retry_after, 0.5 * (2 ** attempt))
            print(f"    ⏳ {label}, retrying in {wait:.1f}s…")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()

    resp.raise_for_status()
    return {}  # unreachable


def fetch_all_trades(condition_id: str, api_key: str) -> list[dict]:
    """
    Paginate through the Dome API /polymarket/orders endpoint using
    cursor-based pagination (pagination_key) for unlimited depth.

    Tuned for Dome Dev tier: 100 QPS / 500 per 10s.
    Uses connection pooling and no inter-request delay.
    """
    all_trades: list[dict] = []
    pagination_key: str | None = None
    page_num = 0

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})

    while True:
        body = _fetch_page(session, condition_id, pagination_key)
        orders = body.get("orders", [])
        pagination = body.get("pagination", {})

        if not orders:
            break

        all_trades.extend(orders)
        page_num += 1
        total = pagination.get("total", "?")
        if page_num % 10 == 0 or not pagination.get("has_more", False):
            print(
                f"  page {page_num}: {len(all_trades):,} / {total:,} fetched"
            )

        if not pagination.get("has_more", False):
            break

        pagination_key = pagination.get("pagination_key")
        if not pagination_key:
            break

    session.close()
    return all_trades


# ── Step 3: normalize to YES/USDC terms ─────────────────────────────────────
def normalize_trade(raw: dict, yes_label: str = "Yes") -> dict:
    """
    Convert a Dome API order into the equivalent YES-side action.

    For standard Yes/No markets, yes_label="Yes".
    For sports/multi-outcome markets (e.g., "Texans"/"Patriots"),
    pass the first outcome name as yes_label so it gets the YES role.

    Normalisation rules:
    | Raw trade               | Equivalent  | YES price after |
    |-------------------------|-------------|-----------------|
    | BUY  <yes_label> @ p    | buy_yes     | p               |
    | SELL <yes_label> @ p    | sell_yes    | p               |
    | BUY  <other>     @ p    | sell_yes    | 1 - p           |
    | SELL <other>     @ p    | buy_yes     | 1 - p           |
    """
    side_raw = raw["side"].upper()
    outcome = raw["token_label"]
    price = float(raw["price"])
    size = float(raw["shares_normalized"])
    ts = int(raw["timestamp"])

    is_yes_side = outcome.lower() == yes_label.lower()

    if is_yes_side:
        yes_price = price
        norm_side = "buy_yes" if side_raw == "BUY" else "sell_yes"
    else:
        yes_price = round(1.0 - price, 10)
        norm_side = "sell_yes" if side_raw == "BUY" else "buy_yes"

    usd_size = round(size * price, 6)

    return {
        "timestamp": ts,
        "datetime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "side": norm_side,
        "yes_price": round(yes_price, 6),
        "size_shares": round(size, 6),
        "usd_size": usd_size,
        "original_outcome": outcome,
        "original_side": side_raw,
        "original_price": price,
        "tx_hash": raw.get("tx_hash", ""),
    }


def normalize_all(trades: list[dict], yes_label: str = "Yes") -> list[dict]:
    normalized = [normalize_trade(t, yes_label=yes_label) for t in trades]
    normalized.sort(key=lambda t: (t["timestamp"], t["tx_hash"]))
    return normalized


# ── Step 4 / 5: output helpers ──────────────────────────────────────────────
COL_WIDTHS = {
    "timestamp": 12,
    "datetime": 26,
    "side": 10,
    "yes_price": 10,
    "size_shares": 14,
    "usd_size": 12,
    "original_outcome": 8,
    "original_side": 6,
    "original_price": 8,
    "tx_hash": 12,
}

HEADERS = {
    "timestamp": "Timestamp",
    "datetime": "DateTime (UTC)",
    "side": "Side",
    "yes_price": "YES Price",
    "size_shares": "Shares",
    "usd_size": "USD Size",
    "original_outcome": "Outcome",
    "original_side": "RawSd",
    "original_price": "RawPx",
    "tx_hash": "TxHash",
}


def fmt_val(key: str, val) -> str:
    if key == "tx_hash":
        return str(val)[:12]
    if isinstance(val, float):
        if key in ("yes_price", "original_price"):
            return f"{val:.4f}"
        return f"{val:.2f}"
    return str(val)


def print_table(rows: list[dict], title: str) -> None:
    if not rows:
        print(f"\n  (no rows for: {title})")
        return

    cols = list(COL_WIDTHS.keys())
    widths = {c: max(COL_WIDTHS[c], len(HEADERS[c])) for c in cols}

    header = " | ".join(HEADERS[c].ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)

    print()
    print(f"── {title} {'─' * max(0, 78 - len(title))}")
    print(f"  {header}")
    print(f"  {sep}")
    for r in rows:
        line = " | ".join(fmt_val(c, r[c]).ljust(widths[c]) for c in cols)
        print(f"  {line}")
    print()


def print_statistics(trades: list[dict]) -> None:
    if not trades:
        print("  No trades to analyse.")
        return

    buy_yes = [t for t in trades if t["side"] == "buy_yes"]
    sell_yes = [t for t in trades if t["side"] == "sell_yes"]
    prices = [t["yes_price"] for t in trades]

    first_dt = trades[0]["datetime"]
    last_dt = trades[-1]["datetime"]

    print("=" * 80)
    print("TRADE STATISTICS")
    print("=" * 80)
    print(f"  Total trades   : {len(trades):,}")
    print(f"  Date range     : {first_dt}  →  {last_dt}")
    print()
    print(
        f"  buy_yes  count : {len(buy_yes):>8,}   "
        f"volume: ${sum(t['usd_size'] for t in buy_yes):>14,.2f}"
    )
    print(
        f"  sell_yes count : {len(sell_yes):>8,}   "
        f"volume: ${sum(t['usd_size'] for t in sell_yes):>14,.2f}"
    )
    print(f"  total USD vol  : ${sum(t['usd_size'] for t in trades):>14,.2f}")
    print()
    print(f"  YES price range: min={min(prices):.4f}  max={max(prices):.4f}")
    print(f"  YES price mean : {statistics.mean(prices):.4f}")
    print(f"  YES price med  : {statistics.median(prices):.4f}")
    print()


def print_price_path(trades: list[dict]) -> None:
    """Show the YES price at 0%, 25%, 50%, 75%, 100% of market lifetime."""
    if len(trades) < 2:
        return

    ts_min = trades[0]["timestamp"]
    ts_max = trades[-1]["timestamp"]
    span = ts_max - ts_min

    print("=" * 80)
    print("PRICE PATH (by market lifetime)")
    print("=" * 80)

    for pct in (0, 25, 50, 75, 100):
        target_ts = ts_min + span * pct / 100
        # find closest trade
        closest = min(trades, key=lambda t: abs(t["timestamp"] - target_ts))
        print(
            f"  {pct:>3}%  ts={closest['timestamp']}  "
            f"({closest['datetime']})  "
            f"YES={closest['yes_price']:.4f}"
        )
    print()


# ── Main ─────────────────────────────────────────────────────────────────────
def resolve_market(args: list[str]) -> tuple[dict, str, Path]:
    """
    Resolve the target market from CLI arguments.

    Supported forms:
      fetch_trades.py <event_url_or_slug>
          → picks the market matching the event slug (works for single-market events)
      fetch_trades.py <event_url_or_slug> --market <substring>
          → picks the market whose question contains <substring>
      fetch_trades.py --condition-id <0x…>
          → uses the condition ID directly (skips Gamma lookup)
    """
    import argparse

    parser = argparse.ArgumentParser(description="Fetch & normalise Polymarket trades")
    parser.add_argument("event", nargs="?", help="Event URL or slug")
    parser.add_argument("--market", "-m", help="Substring to match market question")
    parser.add_argument(
        "--condition-id", "-c", help="Use a conditionId directly (skip Gamma lookup)"
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=DEFAULT_FETCH_OUT,
        help="Directory for trades_<...>.json (default: data/ next to fetch_trades.py)",
    )
    opts = parser.parse_args(args)

    if opts.condition_id:
        # Direct condition ID mode — build a minimal market dict
        cid = opts.condition_id
        print(f"\n[1] Using conditionId directly: {cid}")
        market = {
            "conditionId": cid,
            "question": "(direct conditionId lookup)",
            "endDate": "N/A",
            "outcomes": '["Yes","No"]',
            "outcomePrices": "[]",
            "clobTokenIds": "[]",
            "volume": 0,
            "closed": "N/A",
            "active": "N/A",
        }
        slug = cid[:16]
        out_dir = opts.output_dir.expanduser().resolve()
        return market, slug, out_dir

    if not opts.event:
        parser.print_help()
        sys.exit(1)

    slug = extract_event_slug(opts.event)
    print(f"\n[1] Looking up event slug: {slug}")
    event_data = fetch_event(slug)
    markets = event_data.get("markets", [])

    if not markets:
        raise ValueError("No markets found under this event")

    if opts.market:
        needle = opts.market.lower()
        matches = [m for m in markets if needle in m.get("question", "").lower()]
        if len(matches) == 0:
            print(f"[error] No market question contains '{opts.market}'. Available:")
            for m in markets:
                print(f"  • {m['question']}")
            sys.exit(1)
        if len(matches) > 1:
            print(f"[warn] Multiple markets match '{opts.market}'; picking first:")
            for m in matches:
                print(f"  • {m['question']}")
        market = matches[0]
    else:
        market = pick_market(event_data, slug)

    out_dir = opts.output_dir.expanduser().resolve()
    return market, slug, out_dir


def main() -> None:
    market, slug, out_dir = resolve_market(sys.argv[1:])
    api_key = require_dome_api_key()
    condition_id = market["conditionId"]
    print_market_metadata(market)

    # Determine the "YES" label — first outcome in the outcomes list.
    # Standard markets: ["Yes", "No"]. Sports: ["Texans", "Patriots"], etc.
    raw_outcomes = market.get("outcomes", '["Yes","No"]')
    outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
    yes_label = outcomes[0] if outcomes else "Yes"
    if yes_label.lower() not in ("yes", "no"):
        print(f"  [note] Non-standard outcomes {outcomes}; treating '{yes_label}' as the YES side.\n")

    print(f"[2] Fetching ALL trades via Dome API for conditionId={condition_id}…")
    raw_trades = fetch_all_trades(condition_id, api_key)
    print(f"    → {len(raw_trades):,} raw trades fetched.\n")

    if not raw_trades:
        print("  No trades found. Nothing to normalise.")
        sys.exit(0)

    print(f"[3] Normalising {len(raw_trades):,} trades to YES/USDC terms…")
    trades = normalize_all(raw_trades, yes_label=yes_label)
    print(f"    → {len(trades):,} normalised trades (sorted by timestamp).\n")

    # ── Step 5: output ──────────────────────────────────────────────────────
    print_market_metadata(market)
    print_statistics(trades)

    print_table(trades[:10], "FIRST 10 TRADES (chronological)")
    print_table(trades[-10:], "LAST 10 TRADES (chronological)")

    mid_idx = len(trades) // 2
    print_table(trades[mid_idx - 5 : mid_idx + 5], "10 TRADES FROM THE MIDDLE")

    print_price_path(trades)

    # Write the full normalised dataset to JSON for downstream use
    out_dir.mkdir(parents=True, exist_ok=True)
    # Use a filesystem-safe name derived from the market question or slug
    safe_name = (
        market.get("question", slug)
        .lower()
        .replace("?", "")
        .replace(" ", "-")
        .replace("/", "-")
        .strip("-")[:80]
    )
    out_path = out_dir / f"trades_{safe_name}.json"
    with open(out_path, "w") as f:
        json.dump(trades, f, indent=2)
    print(f"[done] Full normalised trade list written to {out_path} ({len(trades):,} rows)")


if __name__ == "__main__":
    main()
