import json
import os
import re
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


def run_full_sweep(
    data_folder: str | Path,
    results_dir: str | Path,
    liquidity: float,
    strategy_filter: set[str] | None = None,
    extra_strategies: list[dict] | None = None,
    include_builtins: bool = True,
):
    """Sweep all strategies across all markets, write per-strategy files + index.json."""
    data_folder = Path(data_folder)
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(data_folder / "manifest.json")
    engine = SimulationEngine()

    strategies = list(STRATEGY_REGISTRY) if include_builtins else []
    if extra_strategies:
        strategies.extend(extra_strategies)
    if strategy_filter:
        strategies = [s for s in strategies if s["name"] in strategy_filter]

    if not strategies:
        print("No strategies to run.")
        return

    num_markets = len(manifest["markets"])
    print(f"\n=== Strategy Sweep: {len(strategies)} strategies x {num_markets} markets ===")
    print(f"Liquidity: ${liquidity:,.0f} per market\n")

    # Load all trade data once (shared across strategies)
    market_data = []
    for spec in manifest["markets"]:
        trades, metadata = load_polymarket_trades(str(data_folder / spec["file"]))
        initial_prob = spec.get("initial_prob", trades[0].yes_price if trades else 0.5)
        initial_prob = max(0.001, min(0.999, initial_prob))
        market_start = spec.get("market_start", trades[0].timestamp if trades else 0)
        market_end = spec.get("market_end", trades[-1].timestamp if trades else 1)
        market_data.append({
            "spec": spec,
            "trades": trades,
            "metadata": metadata,
            "initial_prob": initial_prob,
            "market_start": market_start,
            "market_end": market_end,
        })

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

    # Populate markets list in index (shared across all strategies)
    for md in market_data:
        index["markets"].append({
            "question": md["spec"]["question"],
            "outcome": md["spec"]["outcome"],
            "category": md["spec"].get("category", "other"),
            "num_trades": md["metadata"]["total_collapsed_trades"],
            "initial_prob": md["initial_prob"],
            "market_start": md["market_start"],
            "market_end": md["market_end"],
        })

    file_sizes = {}

    for strat_spec in strategies:
        strat_name = strat_spec["name"]
        strat_cls = strat_spec["class"]
        strat_kwargs = strat_spec["kwargs"]

        strategy_markets = []
        per_market_agg = []

        for md in market_data:
            if not md["trades"]:
                continue

            # Fresh strategy instance per market
            strategy = strat_cls(**strat_kwargs)

            result = engine.run_single(
                trades=md["trades"],
                strategy=strategy,
                initial_liquidity=liquidity,
                initial_prob=md["initial_prob"],
                outcome=md["spec"]["outcome"],
                market_start=md["market_start"],
                market_end=md["market_end"],
            )

            total_signals = result.num_trades + result.num_trades_skipped

            strategy_markets.append({
                "question": md["spec"]["question"],
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
            })

            per_market_agg.append({
                "question": md["spec"]["question"],
                "category": md["spec"].get("category", "other"),
                "return_on_liquidity": result.return_on_liquidity,
                "fee_revenue": result.fee_revenue,
                "total_pnl": result.total_pnl,
                "num_trades_executed": result.num_trades,
                "num_trades_skipped": result.num_trades_skipped,
                "skip_rate": result.skip_rate,
            })

        # Write per-strategy file
        filename = sanitize_filename(strat_name) + ".json"
        filepath = results_dir / filename
        with open(filepath, "w") as f:
            json.dump({"strategy_name": strat_name, "markets": strategy_markets}, f)
        fsize = os.path.getsize(filepath)
        file_sizes[filename] = fsize

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
        index["strategy_files"][strat_name] = filename
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
