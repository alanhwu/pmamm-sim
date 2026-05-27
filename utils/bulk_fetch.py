#!/usr/bin/env python3
"""
Bulk-fetch markets from a CSV and add them to the manifest.

Usage:
    python bulk_fetch.py markets.csv [--category politics] [--output-dir data]

The CSV must have at minimum: slug, question, outcome_prices, closed
Outcome is inferred from outcome_prices: ["1","0"] = YES won, ["0","1"] = NO won.
Markets that aren't closed are skipped.

Each market is fetched via fetch_trades.py, then appended to manifest.json.
"""

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path


def parse_outcome(outcome_prices_str: str) -> int | None:
    """Parse outcome from outcome_prices like '["1", "0"]'. Returns 0 or 1, or None if unresolved."""
    try:
        prices = json.loads(outcome_prices_str)
        if prices[0] == "1":
            return 1  # YES won
        elif prices[1] == "1":
            return 0  # NO won
        else:
            return None  # Not resolved
    except (json.JSONDecodeError, IndexError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Bulk-fetch Polymarket trades from a CSV")
    parser.add_argument("csv_file", help="CSV with slug, question, outcome_prices columns")
    parser.add_argument("--category", type=str, default="other",
                        help="Category to assign to all markets (default: other)")
    parser.add_argument("--output-dir", type=str, default="data",
                        help="Output directory for trade files (default: data)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched without fetching")
    parser.add_argument("--skip-existing", action="store_true", default=True,
                        help="Skip markets already in manifest (default: true)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    manifest_path = output_dir / "manifest.json"

    # Load existing manifest
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
    else:
        manifest = {"markets": []}

    existing_slugs = set()
    for m in manifest["markets"]:
        # Extract slug from filename
        fname = m.get("file", "")
        slug = fname.replace("trades_", "").replace(".json", "")
        existing_slugs.add(slug)

    # Read CSV (reversed — start from the bottom)
    with open(args.csv_file) as f:
        reader = csv.DictReader(f)
        rows = list(reversed(list(reader)))

    print(f"CSV has {len(rows)} markets (processing bottom-up)")
    print()

    fetched = 0
    skipped = 0
    errors = 0

    for row in rows:
        slug = row.get("slug", "").strip()
        question = row.get("question", "").strip()
        closed = row.get("closed", "").strip().lower()
        outcome_prices = row.get("outcome_prices", "")

        if not slug:
            continue

        # Skip if not closed
        if closed != "true":
            print(f"  SKIP  {slug} (not closed)")
            skipped += 1
            continue

        # Parse outcome
        outcome = parse_outcome(outcome_prices)
        if outcome is None:
            print(f"  SKIP  {slug} (outcome not resolved: {outcome_prices})")
            skipped += 1
            continue

        # Skip if already in manifest
        if args.skip_existing and slug in existing_slugs:
            print(f"  SKIP  {slug} (already in manifest)")
            skipped += 1
            continue

        outcome_label = "YES" if outcome == 1 else "NO"
        print(f"  FETCH {slug} (outcome={outcome_label})")

        if args.dry_run:
            fetched += 1
            continue

        # Run fetch_trades.py — use condition_id if available (skips Gamma lookup)
        condition_id = row.get("condition_id", "").strip()
        fetch_script = str(Path(__file__).parent / "fetch_trades.py")
        if condition_id:
            cmd = [sys.executable, fetch_script, "--condition-id", condition_id, "--output-dir", str(output_dir)]
        else:
            cmd = [sys.executable, fetch_script, slug, "--output-dir", str(output_dir)]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                print(f"  ERROR {slug}: {result.stderr.strip().split(chr(10))[-1]}")
                errors += 1
                continue
        except subprocess.TimeoutExpired:
            print(f"  ERROR {slug}: fetch timed out")
            errors += 1
            continue

        # Find the output file — try slug first, then condition_id prefix
        existing_files = {m["file"] for m in manifest["markets"]}
        trade_file = None
        for pattern in [f"trades_*{slug}*.json", f"trades_{condition_id[:16]}*.json"]:
            for f in output_dir.glob(pattern):
                if f.name not in existing_files:
                    trade_file = f
                    break
            if trade_file:
                break

        if trade_file is None:
            # Last resort: any new file not in manifest
            for f in sorted(output_dir.glob("trades_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                if f.name not in existing_files:
                    trade_file = f
                    break

        if trade_file is None:
            print(f"  ERROR {slug}: could not find output file")
            errors += 1
            continue

        # Count trades
        with open(trade_file) as f:
            trades = json.load(f)
        num_trades = len(trades)

        # Compute approximate volume
        volume = sum(t.get("usd_size", 0) for t in trades)

        # Add to manifest
        entry = {
            "file": trade_file.name,
            "outcome": outcome,
            "question": question,
            "category": args.category,
            "trades": num_trades,
            "volume_usd": round(volume),
        }
        manifest["markets"].append(entry)
        existing_slugs.add(slug)
        fetched += 1

        print(f"        -> {trade_file.name} ({num_trades:,} trades, ${volume:,.0f} vol)")

    # Write updated manifest
    if not args.dry_run and fetched > 0:
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"\nManifest updated: {manifest_path} ({len(manifest['markets'])} markets total)")

    print(f"\nDone: {fetched} fetched, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
