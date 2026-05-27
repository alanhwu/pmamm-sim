import json
import os
import re
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pmamm_sim.data_loader import load_polymarket_trades
from pmamm_sim.engine import SimulationEngine
from pmamm_sim.strategies import STRATEGY_REGISTRY


def load_manifest(manifest_path: str | Path) -> dict:
    with open(manifest_path, "r") as f:
        return json.load(f)


def sanitize_filename(name: str) -> str:
    """Turn a strategy name like 'FixedFee(100bps)' into 'strategy_FixedFee_100bps'."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return f"strategy_{cleaned}"


def _run_one_market(args: tuple) -> dict:
    """Worker function: run one strategy against one market.

    Loads trade data from disk and writes per-market results to disk,
    avoiding pickling large trade lists across processes.
    """
    strat_cls, strat_kwargs, trade_file, liquidity, outcome, question, category, output_path = args

    from pmamm_sim.data_loader import load_polymarket_trades

    trades, metadata = load_polymarket_trades(trade_file)
    if not trades:
        return None

    initial_prob = max(0.001, min(0.999, trades[0].yes_price))
    market_start = trades[0].timestamp
    market_end = trades[-1].timestamp

    engine = SimulationEngine()
    strategy = strat_cls(**strat_kwargs)

    result = engine.run_single(
        trades=trades,
        strategy=strategy,
        initial_liquidity=liquidity,
        initial_prob=initial_prob,
        outcome=outcome,
        market_start=market_start,
        market_end=market_end,
    )

    market_result = {
        "question": question,
        "summary": {
            "fee_revenue": result.fee_revenue,
            "resolution_pnl": result.resolution_pnl,
            "total_pnl": result.total_pnl,
            "return_on_liquidity": result.return_on_liquidity,
            "num_trades_executed": result.num_trades,
            "num_trades_skipped": result.num_trades_skipped,
            "skip_rate": result.skip_rate,
        },
        "trades": result.trade_log,
    }

    # Write per-market file in the worker (parallel writes)
    if output_path:
        with open(output_path, "w") as f:
            json.dump(market_result, f)

    # Return only the summary (no trade log) to the main process
    return {
        "question": question,
        "category": category,
        "return_on_liquidity": result.return_on_liquidity,
        "fee_revenue": result.fee_revenue,
        "total_pnl": result.total_pnl,
        "num_trades_executed": result.num_trades,
        "num_trades_skipped": result.num_trades_skipped,
        "skip_rate": result.skip_rate,
    }


def run_full_sweep(
    data_folder: str | Path,
    results_dir: str | Path,
    liquidity: float,
    strategy_filter: set[str] | None = None,
    extra_strategies: list[dict] | None = None,
    include_builtins: bool = True,
    max_workers: int | None = None,
):
    """Sweep all strategies across all markets, write per-strategy files + index.json."""
    data_folder = Path(data_folder)
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(data_folder / "manifest.json")

    strategies = list(STRATEGY_REGISTRY) if include_builtins else []
    if extra_strategies:
        seen = {s["name"] for s in strategies}
        for s in extra_strategies:
            if s["name"] in seen:
                print(f"  SKIP  {s['name']} (duplicate of existing strategy)")
                continue
            seen.add(s["name"])
            strategies.append(s)
    if strategy_filter:
        strategies = [s for s in strategies if s["name"] in strategy_filter]

    if not strategies:
        print("No strategies to run.")
        return

    # Load metadata for the index (lightweight — just need trade counts and initial probs)
    market_specs = []
    for spec in manifest["markets"]:
        trade_file = str(data_folder / spec["file"])
        trades, metadata = load_polymarket_trades(trade_file)
        initial_prob = spec.get("initial_prob", trades[0].yes_price if trades else 0.5)
        initial_prob = max(0.001, min(0.999, initial_prob))
        market_start = spec.get("market_start", trades[0].timestamp if trades else 0)
        market_end = spec.get("market_end", trades[-1].timestamp if trades else 1)
        market_specs.append({
            "spec": spec,
            "trade_file": trade_file,
            "has_trades": len(trades) > 0,
            "metadata": metadata,
            "initial_prob": initial_prob,
            "market_start": market_start,
            "market_end": market_end,
        })

    num_markets = len(manifest["markets"])
    print(f"\n=== Strategy Sweep: {len(strategies)} strategies x {num_markets} markets ===")
    print(f"Liquidity: ${liquidity:,.0f} per market\n")

    # Build index skeleton
    index = {
        "config": {
            "initial_liquidity": liquidity,
            "data_folder": str(data_folder),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "strategies": [],
        "strategy_files": {},
        "markets": [],
        "aggregate": {},
    }

    for ms in market_specs:
        hidden = ms["spec"].get("hidden", False)
        index["markets"].append({
            "question": "Hidden Market" if hidden else ms["spec"]["question"],
            "outcome": ms["spec"]["outcome"],
            "category": ms["spec"].get("category", "other"),
            "hidden": hidden,
            "num_trades": ms["metadata"]["total_collapsed_trades"],
            "initial_prob": ms["initial_prob"],
            "market_start": ms["market_start"] if not hidden else 0,
            "market_end": ms["market_end"] if not hidden else 0,
        })

    file_sizes = {}
    use_parallel = num_markets > 10

    for strat_spec in strategies:
        strat_name = strat_spec["name"]
        strat_cls = strat_spec["class"]
        strat_kwargs = strat_spec["kwargs"]

        # Per-strategy output directory for per-market files
        strat_dir_name = sanitize_filename(strat_name)
        strat_dir = results_dir / strat_dir_name
        strat_dir.mkdir(parents=True, exist_ok=True)

        # Build task args for each market
        tasks = []
        for i, ms in enumerate(market_specs):
            if not ms["has_trades"]:
                continue
            hidden = ms["spec"].get("hidden", False)
            market_output = None if hidden else str(strat_dir / f"market_{i:04d}.json")
            question = "Hidden Market" if hidden else ms["spec"]["question"]
            tasks.append((
                strat_cls, strat_kwargs,
                ms["trade_file"], liquidity,
                ms["spec"]["outcome"],
                question, ms["spec"].get("category", "other"),
                market_output,
            ))

        # Run markets in parallel or sequential
        if use_parallel:
            with ProcessPoolExecutor(max_workers=max_workers) as pool:
                results = list(pool.map(_run_one_market, tasks))
        else:
            results = [_run_one_market(t) for t in tasks]

        per_market_agg = [r for r in results if r is not None]

        # Write strategy index (lightweight — points to per-market files)
        # Hidden markets get summary but no file pointer (no trade data to drill into)
        market_files = []
        task_idx = 0
        for i, ms in enumerate(market_specs):
            if not ms["has_trades"]:
                continue
            r = results[task_idx]
            task_idx += 1
            if r is None:
                continue
            hidden = ms["spec"].get("hidden", False)
            entry = {
                "question": r["question"],
                "summary": {
                    "fee_revenue": r["fee_revenue"],
                    "resolution_pnl": 0,
                    "total_pnl": r["total_pnl"],
                    "return_on_liquidity": r["return_on_liquidity"],
                    "num_trades_executed": r["num_trades_executed"],
                    "num_trades_skipped": r["num_trades_skipped"],
                    "skip_rate": r["skip_rate"],
                },
            }
            if not hidden:
                entry["file"] = f"market_{i:04d}.json"
            market_files.append(entry)

        strat_index_path = results_dir / (strat_dir_name + ".json")
        with open(strat_index_path, "w") as f:
            json.dump({"strategy_name": strat_name, "markets": market_files}, f)
        fsize = os.path.getsize(strat_index_path)
        file_sizes[strat_dir_name + ".json"] = fsize

        # Aggregate for index
        n = len(per_market_agg)
        avg_ret = sum(m["return_on_liquidity"] for m in per_market_agg) / n if n else 0.0
        total_fees = sum(m["fee_revenue"] for m in per_market_agg)
        total_executed = sum(m["num_trades_executed"] for m in per_market_agg)
        total_skipped = sum(m["num_trades_skipped"] for m in per_market_agg)
        total_signals = total_executed + total_skipped
        avg_skip_rate = total_skipped / total_signals if total_signals > 0 else 0.0

        # Category-weighted score: avg within each category, then avg across categories
        cat_returns: dict[str, list[float]] = {}
        for m in per_market_agg:
            cat_returns.setdefault(m["category"], []).append(m["return_on_liquidity"])
        cat_avgs = {cat: sum(rets) / len(rets) for cat, rets in cat_returns.items()}
        category_score = sum(cat_avgs.values()) / len(cat_avgs) if cat_avgs else 0.0

        index["strategies"].append(strat_name)
        index["strategy_files"][strat_name] = strat_dir_name + ".json"
        index["aggregate"][strat_name] = {
            "author": strat_spec.get("author", ""),
            "avg_return": avg_ret,
            "category_score": category_score,
            "category_averages": cat_avgs,
            "total_fees": total_fees,
            "total_executed": total_executed,
            "total_skipped": total_skipped,
            "avg_skip_rate": avg_skip_rate,
            "per_market": per_market_agg,
        }

        print(
            f"  {strat_name + '...':<30} "
            f"score: {category_score:+.1%}  "
            f"fees: ${total_fees:,.0f}  "
            f"(skip {avg_skip_rate:.1%})"
        )

    # Write index
    index_path = results_dir / "index.json"
    with open(index_path, "w") as f:
        json.dump(index, f)
    file_sizes["index.json"] = os.path.getsize(index_path)

    # Print file listing
    print(f"\nResults written to {results_dir}/")
    for fname, fsize in file_sizes.items():
        if fsize >= 1024 * 1024:
            size_str = f"{fsize / (1024 * 1024):.1f} MB"
        else:
            size_str = f"{fsize / 1024:.1f} KB"
        print(f"  {fname} ({size_str})")
