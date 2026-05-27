#!/usr/bin/env python3
"""
Convert ob_cache market folders into pmamm-sim trade JSON files and add to manifest.

Reads outcome from a Polymarket CSV (slug -> outcome_prices) and cross-checks
against the last trade's yes_price to verify consistency.

ob_cache format (per market folder):
    meta.json              — slug, side_a_id, side_b_id
    side_a_trades.json.gz  — trades for the YES/first outcome
    side_b_trades.json.gz  — trades for the NO/second outcome

Usage:
    # Convert all markets, look up outcomes from CSV, add to manifest
    python utils/convert_ob_cache.py ob_cache/ --csv polymarket_top_2000_markets.csv

    # Convert a single market
    python utils/convert_ob_cache.py ob_cache/bitcoin-up-or-down-on-february-2 --csv markets.csv

    # Dry run
    python utils/convert_ob_cache.py ob_cache/ --csv markets.csv --dry-run
"""

import argparse
import csv
import gzip
import json
from pathlib import Path


def load_csv_outcomes(csv_path: str) -> dict:
    """Load slug -> {outcome, question, closed} from the Polymarket CSV."""
    lookup = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            slug = row.get("slug", "").strip()
            if not slug:
                continue

            closed = row.get("closed", "").strip().lower() == "true"
            question = row.get("question", "").strip()

            # Parse outcome from outcome_prices: ["1","0"] = YES won, ["0","1"] = NO won
            outcome = None
            try:
                prices = json.loads(row.get("outcome_prices", ""))
                if prices[0] == "1":
                    outcome = 1
                elif prices[1] == "1":
                    outcome = 0
            except (json.JSONDecodeError, IndexError, TypeError):
                pass

            lookup[slug] = {
                "outcome": outcome,
                "question": question,
                "closed": closed,
            }
    return lookup


def convert_market(market_dir: Path, output_dir: Path) -> tuple[str, list[dict]] | None:
    """Convert one ob_cache market folder to a trades JSON file.

    Returns (slug, all_trades) or None on error.
    """
    meta_path = market_dir / "meta.json"
    if not meta_path.exists():
        return None

    with open(meta_path) as f:
        meta = json.load(f)

    slug = meta.get("slug", market_dir.name)

    side_a_path = market_dir / "side_a_trades.json.gz"
    side_b_path = market_dir / "side_b_trades.json.gz"

    side_a_trades = []
    side_b_trades = []

    if side_a_path.exists():
        with gzip.open(side_a_path, "rt") as f:
            side_a_trades = json.load(f)

    if side_b_path.exists():
        with gzip.open(side_b_path, "rt") as f:
            side_b_trades = json.load(f)

    if not side_a_trades and not side_b_trades:
        return None

    all_trades = []

    for t in side_a_trades:
        price = t.get("price", 0)
        shares = t.get("shares_normalized", 0)
        original_side = t.get("side", "")
        side = "buy_yes" if original_side == "BUY" else "sell_yes"

        all_trades.append({
            "timestamp": t.get("timestamp", 0),
            "datetime": "",
            "side": side,
            "yes_price": price,
            "size_shares": shares,
            "usd_size": price * shares,
            "original_outcome": t.get("token_label", "Yes"),
            "original_side": original_side,
            "original_price": price,
            "tx_hash": t.get("tx_hash", ""),
        })

    for t in side_b_trades:
        price = t.get("price", 0)
        yes_price = 1.0 - price
        shares = t.get("shares_normalized", 0)
        original_side = t.get("side", "")
        side = "sell_yes" if original_side == "BUY" else "buy_yes"

        all_trades.append({
            "timestamp": t.get("timestamp", 0),
            "datetime": "",
            "side": side,
            "yes_price": yes_price,
            "size_shares": shares,
            "usd_size": price * shares,
            "original_outcome": t.get("token_label", "No"),
            "original_side": original_side,
            "original_price": price,
            "tx_hash": t.get("tx_hash", ""),
        })

    all_trades.sort(key=lambda t: (t["timestamp"], t["tx_hash"]))
    return slug, all_trades


def main():
    parser = argparse.ArgumentParser(description="Convert ob_cache markets to pmamm-sim format")
    parser.add_argument("path", help="ob_cache/ directory or a single market folder")
    parser.add_argument("--csv", required=True,
                        help="Polymarket CSV with slug, outcome_prices, question columns")
    parser.add_argument("--output-dir", type=str, default="data",
                        help="Output directory for trade files (default: data)")
    parser.add_argument("--category", type=str, default="other",
                        help="Category to assign (default: other)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be converted without writing")
    args = parser.parse_args()

    source = Path(args.path)
    output_dir = Path(args.output_dir)

    # Load CSV lookup
    csv_lookup = load_csv_outcomes(args.csv)
    print(f"CSV loaded: {len(csv_lookup)} markets")

    # Load existing manifest
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
    else:
        manifest = {"markets": []}

    existing_files = {m["file"] for m in manifest["markets"]}

    # Find market folders
    if (source / "meta.json").exists():
        market_dirs = [source]
    else:
        market_dirs = sorted([d for d in source.iterdir() if d.is_dir() and (d / "meta.json").exists()])

    print(f"Found {len(market_dirs)} market folders")
    print()

    converted = 0
    skipped = 0
    mismatches = 0

    for market_dir in market_dirs:
        result = convert_market(market_dir, output_dir)
        if result is None:
            skipped += 1
            continue

        slug, all_trades = result
        filename = f"trades_{slug}.json"

        # Skip if already in manifest
        if filename in existing_files:
            skipped += 1
            continue

        # Look up outcome in CSV
        csv_info = csv_lookup.get(slug)
        if csv_info is None:
            print(f"  SKIP  {slug} (not in CSV)")
            skipped += 1
            continue

        if not csv_info["closed"]:
            print(f"  SKIP  {slug} (not closed)")
            skipped += 1
            continue

        outcome = csv_info["outcome"]
        if outcome is None:
            print(f"  SKIP  {slug} (no resolved outcome in CSV)")
            skipped += 1
            continue

        # Verify: last trade's yes_price should be near 1.0 if YES won, near 0.0 if NO won
        last_price = all_trades[-1]["yes_price"]
        expected_high = outcome == 1  # YES won → last price should be high
        price_agrees = (last_price > 0.5) == expected_high

        if not price_agrees:
            label = "YES" if outcome == 1 else "NO"
            print(f"  WARN  {slug}: CSV says {label} won but last yes_price={last_price:.4f}")
            mismatches += 1

        question = csv_info["question"]

        if args.dry_run:
            label = "YES" if outcome == 1 else "NO"
            print(f"  WOULD CONVERT  {slug} ({len(all_trades):,} trades, outcome={label})")
            converted += 1
            continue

        # Write trade file
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / filename, "w") as f:
            json.dump(all_trades, f)

        # Compute volume
        volume = sum(t["usd_size"] for t in all_trades)

        # Add to manifest
        manifest["markets"].append({
            "file": filename,
            "outcome": outcome,
            "question": question,
            "category": args.category,
            "trades": len(all_trades),
            "volume_usd": round(volume),
        })
        existing_files.add(filename)
        converted += 1

        label = "YES" if outcome == 1 else "NO"
        check = "OK" if price_agrees else "WARN"
        print(f"  {check:4}  {filename} ({len(all_trades):,} trades, {label}, last_price={last_price:.4f})")

    # Write manifest
    if not args.dry_run and converted > 0:
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"\nManifest updated: {len(manifest['markets'])} markets total")

    print(f"\nDone: {converted} converted, {skipped} skipped, {mismatches} outcome warnings")


if __name__ == "__main__":
    main()
