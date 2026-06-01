import argparse
import json
import os
from datetime import datetime, timezone

from pmamm_sim.data_loader import load_polymarket_trades
from pmamm_sim.engine import SimulationEngine
from pmamm_sim.strategies import STRATEGIES, FixedFee


def main():
    import sys

    # Detect subcommand before argparse — if first positional arg isn't "batch",
    # treat the entire argv as single-market mode (backwards compatible).
    if len(sys.argv) > 1 and sys.argv[1] == "batch":
        parser = argparse.ArgumentParser(
            prog="pmamm_sim batch",
            description="Sweep all strategies across all markets in a manifest",
        )
        parser.add_argument("folder", help="Path to folder containing manifest.json and trade files")
        parser.add_argument("--liquidity", type=float, default=10000,
                            help="Initial LP deposit in USDC per market")
        parser.add_argument("--results-dir", type=str, required=True, metavar="DIR",
                            help="Output directory for results (created if needed)")
        parser.add_argument("--strategies", type=str, default=None,
                            help="Comma-separated strategy names to run (default: all)")
        args = parser.parse_args(sys.argv[2:])
        _run_batch(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "serve":
        parser = argparse.ArgumentParser(
            prog="pmamm_sim serve",
            description="Serve the visualizer, competition UI, and API",
        )
        parser.add_argument("--port", type=int, default=8080, help="Port to serve on (default: 8080)")
        parser.add_argument("--data-folder", type=str, default="data",
                            help="Path to folder with manifest.json and trade files (default: data)")
        parser.add_argument("--results-dir", type=str, default="results",
                            help="Output directory for results (default: results)")
        parser.add_argument("--submissions-dir", type=str, default="submissions",
                            help="Directory for strategy submissions (default: submissions)")
        parser.add_argument("--liquidity", type=float, default=10000,
                            help="Initial LP deposit in USDC per market (default: 10000)")
        parser.add_argument("--no-builtins", action="store_true",
                            help="Exclude built-in strategies from competition runs")
        args = parser.parse_args(sys.argv[2:])
        args.include_builtins = not args.no_builtins
        _run_serve(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "compete":
        parser = argparse.ArgumentParser(
            prog="pmamm_sim compete",
            description="Run a strategy competition from submission files",
        )
        parser.add_argument("submissions_dir",
                            help="Directory containing strategy .py files")
        parser.add_argument("--data-folder", type=str, default="data",
                            help="Path to folder with manifest.json and trade files (default: data)")
        parser.add_argument("--results-dir", type=str, default="results",
                            help="Output directory for results (default: results)")
        parser.add_argument("--liquidity", type=float, default=10000,
                            help="Initial LP deposit in USDC per market (default: 10000)")
        parser.add_argument("--no-builtins", action="store_true",
                            help="Exclude built-in strategies from STRATEGY_REGISTRY")
        parser.add_argument("--strategies", type=str, default=None,
                            help="Comma-separated strategy names to run (default: all)")
        parser.add_argument("--fail-fast", action="store_true",
                            help="Stop on first submission load error")
        args = parser.parse_args(sys.argv[2:])
        args.include_builtins = not args.no_builtins
        _run_compete(args)
    else:
        parser = argparse.ArgumentParser(description="Replay Polymarket trades through a simulated AMM")
        _add_single_args(parser)
        args = parser.parse_args()
        _run_single(args)


def _add_single_args(parser):
    parser.add_argument("trades_json", help="Path to preprocessed Polymarket trades JSON")
    parser.add_argument("--liquidity", type=float, default=10000, help="Initial LP deposit in USDC")
    parser.add_argument("--prob", type=float, default=None,
                        help="Initial YES probability (default: infer from first trade)")
    parser.add_argument("--outcome", type=int, required=True, choices=[0, 1],
                        help="Market resolution: 0 (NO wins) or 1 (YES wins)")
    parser.add_argument("--baseline-fee", type=float, default=100,
                        help="Baseline strategy fee in bps (default: 100)")
    parser.add_argument("--test-strategy", type=str, default="time_decay",
                        choices=list(STRATEGIES.keys()),
                        help="Test strategy to run (default: time_decay)")
    parser.add_argument("--market-start", type=int, default=None,
                        help="Unix timestamp of market creation (default: infer)")
    parser.add_argument("--market-end", type=int, default=None,
                        help="Unix timestamp of market resolution (default: infer)")
    parser.add_argument("--export", type=str, default=None, metavar="PATH",
                        help="Export full results (including per-trade time series) to JSON")
    parser.add_argument("--question", type=str, default="",
                        help="Market question/title for export metadata")


def _run_single(args):
    # Load data
    trades, metadata = load_polymarket_trades(args.trades_json)
    if not trades:
        print("No trades loaded. Exiting.")
        return

    # Infer defaults
    initial_prob = args.prob if args.prob is not None else trades[0].yes_price
    initial_prob = max(0.001, min(0.999, initial_prob))

    market_start = args.market_start if args.market_start is not None else metadata["date_range"][0]
    market_end = args.market_end if args.market_end is not None else metadata["date_range"][1]

    # Create strategies (separate instances for test and baseline)
    baseline_strategy = FixedFee(fee_bps=args.baseline_fee)
    test_strategy_cls = STRATEGIES[args.test_strategy]
    test_strategy = test_strategy_cls()

    # Run simulation
    engine = SimulationEngine()
    results = engine.run_replay(
        trades=trades,
        test_strategy=test_strategy,
        baseline_strategy=baseline_strategy,
        initial_liquidity=args.liquidity,
        initial_prob=initial_prob,
        outcome=args.outcome,
        market_start=market_start,
        market_end=market_end,
    )

    # Print results
    start_dt = datetime.fromtimestamp(market_start, tz=timezone.utc).strftime("%Y-%m-%d")
    end_dt = datetime.fromtimestamp(market_end, tz=timezone.utc).strftime("%Y-%m-%d")
    price_min, price_max = metadata["yes_price_range"]
    outcome_label = "YES" if args.outcome == 1 else "NO"

    print()
    print("=" * 50)
    print("        Market Replay Simulation")
    print("=" * 50)
    print(f"Trades loaded: {metadata['total_collapsed_trades']:,} "
          f"(collapsed from {metadata['total_raw_trades']:,} raw legs)")
    print(f"Skipped neutral txs: {metadata['skipped_neutral_txs']:,}")
    print(f"Date range: {start_dt} -> {end_dt}")
    print(f"Price range: {price_min:.4f} -> {price_max:.4f}")
    print(f"Initial prob: {initial_prob:.4f}")
    print(f"Resolution: {outcome_label} (outcome={args.outcome})")

    baseline = results["baseline"]
    test = results["test"]
    comp = results["comparison"]

    print()
    print(f"--- Baseline: FixedFee({args.baseline_fee:.0f} bps) ---")
    _print_result(baseline)

    print()
    print(f"--- Test: {args.test_strategy} (defaults) ---")
    _print_result(test)

    print()
    print("--- Comparison ---")
    print(f"Baseline return:  {comp['baseline_return']:+.4%}")
    print(f"Test return:      {comp['test_return']:+.4%}")
    print(f"Improvement:      {comp['improvement']:+.4%} ({comp['improvement_pct']:+.1f}% relative)")

    # Export JSON if requested
    if args.export:
        baseline_name = f"FixedFee({args.baseline_fee:.0f}bps)"
        test_name = f"{args.test_strategy}(defaults)"

        export_data = {
            "market": {
                "question": args.question,
                "outcome": args.outcome,
                "market_start": market_start,
                "market_end": market_end,
                "initial_liquidity": args.liquidity,
                "initial_prob": initial_prob,
                "total_raw_trades": metadata["total_raw_trades"],
                "total_collapsed_trades": metadata["total_collapsed_trades"],
            },
            "baseline": {
                "name": baseline_name,
                "summary": {
                    "fee_revenue": baseline.fee_revenue,
                    "resolution_pnl": baseline.resolution_pnl,
                    "total_pnl": baseline.total_pnl,
                    "return_on_liquidity": baseline.return_on_liquidity,
                },
                "trades": baseline.trade_log,
            },
            "test": {
                "name": test_name,
                "summary": {
                    "fee_revenue": test.fee_revenue,
                    "resolution_pnl": test.resolution_pnl,
                    "total_pnl": test.total_pnl,
                    "return_on_liquidity": test.return_on_liquidity,
                },
                "trades": test.trade_log,
            },
        }

        with open(args.export, "w") as f:
            json.dump(export_data, f)

        size_mb = os.path.getsize(args.export) / (1024 * 1024)
        print(f"\nExported to {args.export} ({size_mb:.1f} MB)")


def _run_batch(args):
    from pmamm_sim.batch import run_full_sweep

    strategy_filter = None
    if args.strategies:
        strategy_filter = set(s.strip() for s in args.strategies.split(","))

    run_full_sweep(
        data_folder=args.folder,
        results_dir=args.results_dir,
        liquidity=args.liquidity,
        strategy_filter=strategy_filter,
    )


def _run_serve(args):
    import http.server
    from pmamm_sim.server import CompetitionHandler, preload_market_data

    # Serve from project root so both visualizer.html and results/ are accessible
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Preload all market data once at startup
    print("Loading market data...")
    manifest, market_specs = preload_market_data(args.data_folder)

    CompetitionHandler.serve_root = root
    CompetitionHandler.data_folder = args.data_folder
    CompetitionHandler.results_dir = args.results_dir
    CompetitionHandler.submissions_dir = args.submissions_dir
    CompetitionHandler.liquidity = args.liquidity
    CompetitionHandler.include_builtins = args.include_builtins
    CompetitionHandler.manifest = manifest
    CompetitionHandler.market_specs = market_specs

    with http.server.ThreadingHTTPServer(("", args.port), CompetitionHandler) as httpd:
        print(f"Serving at http://localhost:{args.port}")
        print(f"  Visualizer:  http://localhost:{args.port}/visualizer.html")
        print(f"  Competition: http://localhost:{args.port}/compete.html")
        print("Press Ctrl+C to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


def _print_result(r):
    total_signals = r.num_trades + r.num_trades_skipped
    print(f"  Initial liquidity: ${r.initial_liquidity:,.2f}")
    print(f"  Fee revenue:       ${r.fee_revenue:,.2f}")
    print(f"  Resolution PnL:    ${r.resolution_pnl:,.2f}")
    print(f"  Total PnL:         ${r.total_pnl:,.2f}")
    print(f"  Return on liq:     {r.return_on_liquidity:+.4%}")
    print(f"  Trades executed:   {r.num_trades:,} / {total_signals:,} ({1.0 - r.skip_rate:.1%})")
    print(f"  Trades skipped:    {r.num_trades_skipped:,} ({r.skip_rate:.1%}) — inside no-arb band")


def _run_compete(args):
    from pathlib import Path
    from pmamm_sim.batch import run_full_sweep
    from pmamm_sim.loader import load_submissions

    submissions_dir = Path(args.submissions_dir)

    print("\n" + "=" * 60)
    print("  WARNING: Strategy files contain arbitrary Python code.")
    print("  Only run submissions you trust.")
    print("=" * 60)

    print(f"\nLoading submissions from {submissions_dir}/")
    strategies, errors = load_submissions(
        submissions_dir, fail_fast=args.fail_fast,
    )

    for err in errors:
        print(f"  SKIP  {err.filename}: {err.reason}")
    for entry in strategies:
        author = entry.get("author", "anonymous")
        print(f"  OK    {entry['name']} (by {author})")

    if not strategies and not args.include_builtins:
        print("\nNo valid submissions found. Nothing to run.")
        return

    strategy_filter = None
    if args.strategies:
        strategy_filter = set(s.strip() for s in args.strategies.split(","))

    run_full_sweep(
        data_folder=args.data_folder,
        results_dir=args.results_dir,
        liquidity=args.liquidity,
        strategy_filter=strategy_filter,
        extra_strategies=strategies if strategies else None,
        include_builtins=args.include_builtins,
    )

    _print_leaderboard(args.results_dir)


def _print_leaderboard(results_dir: str):
    import json
    from pathlib import Path

    index_path = Path(results_dir) / "index.json"
    if not index_path.exists():
        return

    with open(index_path) as f:
        index = json.load(f)

    agg = index.get("aggregate", {})
    if not agg:
        return

    ranked = sorted(agg.items(), key=lambda kv: kv[1].get("category_score", kv[1]["avg_return"]), reverse=True)

    n_markets = len(index.get("markets", []))
    categories = sorted(set(m.get("category", "other") for m in index.get("markets", [])))

    print(f"\n{'=' * 80}")
    print(f"  LEADERBOARD  ({n_markets} markets, categories: {', '.join(categories)})")
    print(f"{'=' * 80}")
    print(f"  {'Rank':<6} {'Strategy':<28} {'Score':>14} {'Avg Return':>12} {'Skip':>7} {'Fill':>7}")
    print(f"  {'----':<6} {'--------':<28} {'----------':>14} {'----------':>12} {'----':>7} {'----':>7}")

    for i, (name, stats) in enumerate(ranked, 1):
        score = stats.get("category_score", stats["avg_return"])
        disqualified = stats.get("disqualified", False)
        avg_ret = stats["avg_return"]
        skip_rate = stats["avg_skip_rate"]
        fill_rate = stats.get("avg_fill_rate", 1.0 - skip_rate)
        score_label = "DQ" if disqualified else f"{score:+.2%}"
        medal = {1: "1st", 2: "2nd", 3: "3rd"}.get(i, f"{i}th")
        print(
            f"  {medal:<6} {name:<28} {score_label:>14} {avg_ret:>+11.2%} {skip_rate:>6.1%} {fill_rate:>6.1%}"
        )

    # Per-category breakdown
    if len(categories) > 1:
        print(f"\n  {'':6} {'Per-category:':<28}", end="")
        for cat in categories:
            print(f" {cat:>12}", end="")
        print()
        for i, (name, stats) in enumerate(ranked, 1):
            cat_avgs = stats.get("category_averages", {})
            print(f"  {'':6} {name:<28}", end="")
            for cat in categories:
                v = cat_avgs.get(cat)
                print(f" {v:>+11.1%}" if v is not None else f" {'n/a':>12}", end="")
            print()

    print(f"{'=' * 80}")
    print(f"  Results: {results_dir}/  |  Visualizer: python -m pmamm_sim serve")
    print()


if __name__ == "__main__":
    main()
