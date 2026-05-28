"""HTTP server with API routes for the competition frontend.

SECURITY NOTE: Submitted strategy code is executed with full Python privileges
during simulation. There is currently no sandboxing beyond AST checks. Only run
this server in trusted environments. See pmamm_sim/sandbox.py for details.
"""

import ast
import json
import re
import threading
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

from pmamm_sim.batch import (
    load_manifest, sanitize_filename, _run_one_market,
)
from pmamm_sim.data_loader import load_polymarket_trades
from pmamm_sim.loader import load_submissions
from pmamm_sim.sandbox import validate_code_safety
from pmamm_sim.strategies import STRATEGY_REGISTRY


def preload_market_data(data_folder: str | Path) -> tuple[dict, list[dict]]:
    """Load manifest and all trade data once. Returns (manifest, market_specs)."""
    data_folder = Path(data_folder)
    manifest = load_manifest(data_folder / "manifest.json")

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

    print(f"Preloaded {len(market_specs)} markets from {data_folder}")
    return manifest, market_specs


class CompetitionHandler(SimpleHTTPRequestHandler):
    """Extends static file serving with competition API routes.

    Class-level config (set before starting the server):
        serve_root:      directory to serve static files from
        data_folder:     path to data/ with manifest.json
        results_dir:     path to results/ output
        submissions_dir: path to submissions/
        liquidity:       initial LP deposit per market
        include_builtins: whether to include built-in strategies in runs
        manifest:        preloaded manifest dict
        market_specs:    preloaded market specs list
    """

    serve_root: str = "."
    data_folder: str = "data"
    results_dir: str = "results"
    submissions_dir: str = "submissions"
    liquidity: float = 10_000
    include_builtins: bool = False
    run_timeout: int = 120
    manifest: dict = {}
    market_specs: list = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=self.serve_root, **kwargs)

    def log_message(self, format, *args):
        if "/api/" in str(args[0]) if args else False:
            super().log_message(format, *args)

    # ── routing ──────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/api/leaderboard":
            self._handle_leaderboard()
        elif self.path == "/api/submissions":
            self._handle_list_submissions()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/submit":
            self._handle_submit()
        else:
            self._send_json({"error": "Not found"}, 404)

    # ── API handlers ─────────────────────────────────────────

    def _handle_leaderboard(self):
        index_path = Path(self.results_dir) / "index.json"
        if not index_path.exists():
            self._send_json({"strategies": [], "markets": [], "config": {}})
            return

        with open(index_path) as f:
            index = json.load(f)

        agg = index.get("aggregate", {})

        def sort_key(kv):
            return kv[1].get("category_score", kv[1]["avg_return"])

        ranked = sorted(agg.items(), key=sort_key, reverse=True)

        strategies = []
        for rank, (name, stats) in enumerate(ranked, 1):
            strategies.append({
                "rank": rank,
                "name": name,
                "author": stats.get("author", ""),
                "avg_return": stats["avg_return"],
                "category_score": stats.get("category_score"),
                "category_averages": stats.get("category_averages"),
                "total_fees": stats["total_fees"],
                "avg_skip_rate": stats["avg_skip_rate"],
                "per_market": stats["per_market"],
            })

        self._send_json({
            "strategies": strategies,
            "markets": index.get("markets", []),
            "config": index.get("config", {}),
        })

    def _handle_list_submissions(self):
        sdir = Path(self.submissions_dir)
        if not sdir.is_dir():
            self._send_json({"submissions": [], "errors": []})
            return

        entries, errors = load_submissions(sdir)
        self._send_json({
            "submissions": [
                {"name": e["name"], "author": e["author"],
                 "description": e["description"], "source": e["source"]}
                for e in entries
            ],
            "errors": [
                {"filename": e.filename, "reason": e.reason}
                for e in errors
            ],
        })

    def _handle_submit(self):
        # Parse request body
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json({"error": "Invalid JSON body"}, 400)
            return

        name = (body.get("name") or "").strip()
        author = (body.get("author") or "anonymous").strip()
        description = (body.get("description") or "").strip()
        code = body.get("code", "")

        if not name:
            self._send_json({"error": "Strategy name is required"}, 400)
            return
        if not code.strip():
            self._send_json({"error": "Strategy code is required"}, 400)
            return

        # Syntax check
        try:
            ast.parse(code)
        except SyntaxError as e:
            self._send_json({
                "error": f"Syntax error: {e.msg} (line {e.lineno})",
            }, 400)
            return

        # Safety check
        violations = validate_code_safety(code)
        if violations:
            self._send_json({
                "error": "Code blocked: " + "; ".join(violations),
            }, 400)
            return

        # Build and save the file
        file_content = (
            f'"""Submitted via web UI."""\n\n'
            f"from pmamm_sim.types import FeeQuote, PendingTrade, TradeInfo\n\n"
            f"STRATEGY_NAME = {name!r}\n"
            f"AUTHOR = {author!r}\n"
            f"DESCRIPTION = {description!r}\n\n\n"
            f"{code}\n"
        )

        try:
            ast.parse(file_content)
        except SyntaxError as e:
            self._send_json({
                "error": f"Syntax error in generated file: {e.msg} (line {e.lineno})",
            }, 400)
            return

        sdir = Path(self.submissions_dir)
        sdir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        safe_name = re.sub(r"_+", "_", safe_name).strip("_").lower()
        filepath = sdir / f"{safe_name}.py"
        filepath.write_text(file_content)

        # Run sweep in-process with preloaded data
        try:
            error = self._run_sweep_with_timeout()
            if error:
                self._send_json({"error": error}, 400)
                return
        except Exception as e:
            self._send_json({"error": f"Run failed: {e}"}, 500)
            return

        self._handle_leaderboard_with_extras(submitted_name=name, load_errors=[])

    def _run_sweep_with_timeout(self) -> str | None:
        """Run only the NEW strategy and merge into existing index. Returns error string or None."""
        from concurrent.futures import ProcessPoolExecutor
        from datetime import datetime, timezone

        results_dir = Path(self.results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        market_specs = self.market_specs
        liquidity = self.liquidity

        # Load existing index (if any) to preserve previous results
        index_path = results_dir / "index.json"
        if index_path.exists():
            with open(index_path) as f:
                index = json.load(f)
        else:
            index = {
                "config": {
                    "initial_liquidity": liquidity,
                    "data_folder": self.data_folder,
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

        # Find which strategies need to run (new submissions not already in index)
        sdir = Path(self.submissions_dir)
        entries, errors = load_submissions(sdir)
        all_strategies = list(STRATEGY_REGISTRY) if self.include_builtins else []
        seen = {s["name"] for s in all_strategies}
        for s in entries:
            if s["name"] not in seen:
                seen.add(s["name"])
                all_strategies.append(s)

        # Only run strategies not already in the index
        existing_names = set(index.get("strategies", []))
        new_strategies = [s for s in all_strategies if s["name"] not in existing_names]

        if not new_strategies and not all_strategies:
            return "No valid strategies to run"

        # Run only new strategies
        for strat_spec in new_strategies:
            strat_name = strat_spec["name"]
            strat_kwargs = strat_spec["kwargs"]
            strat_ref = strat_spec.get("source", strat_spec["class"])

            strat_dir_name = sanitize_filename(strat_name)
            strat_dir = results_dir / strat_dir_name
            strat_dir.mkdir(parents=True, exist_ok=True)

            tasks = []
            for i, ms in enumerate(market_specs):
                if not ms["has_trades"]:
                    continue
                hidden = ms["spec"].get("hidden", False)
                market_output = None if hidden else str(strat_dir / f"market_{i:04d}.json")
                question = "Hidden Market" if hidden else ms["spec"]["question"]
                tasks.append((
                    strat_ref, strat_kwargs,
                    ms["trade_file"], liquidity,
                    ms["spec"]["outcome"],
                    question, ms["spec"].get("category", "other"),
                    market_output,
                ))

            with ProcessPoolExecutor() as pool:
                results = list(pool.map(_run_one_market, tasks))

            per_market_agg = [r for r in results if r is not None]

            # Write strategy index
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

            # Aggregate and merge into index
            n = len(per_market_agg)
            avg_ret = sum(m["return_on_liquidity"] for m in per_market_agg) / n if n else 0.0
            total_fees = sum(m["fee_revenue"] for m in per_market_agg)
            total_executed = sum(m["num_trades_executed"] for m in per_market_agg)
            total_skipped = sum(m["num_trades_skipped"] for m in per_market_agg)
            total_signals = total_executed + total_skipped
            avg_skip_rate = total_skipped / total_signals if total_signals > 0 else 0.0

            cat_returns: dict[str, list[float]] = {}
            for m in per_market_agg:
                cat_returns.setdefault(m["category"], []).append(m["return_on_liquidity"])
            from statistics import median
            cat_medians = {cat: median(rets) for cat, rets in cat_returns.items()}
            category_score = median(cat_medians.values()) if cat_medians else 0.0

            index["strategies"].append(strat_name)
            index["strategy_files"][strat_name] = strat_dir_name + ".json"
            index["aggregate"][strat_name] = {
                "author": strat_spec.get("author", ""),
                "avg_return": avg_ret,
                "category_score": category_score,
                "category_averages": cat_medians,
                "total_fees": total_fees,
                "total_executed": total_executed,
                "total_skipped": total_skipped,
                "avg_skip_rate": avg_skip_rate,
                "per_market": per_market_agg,
            }

        # Update timestamp and write index
        index["config"]["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(index_path, "w") as f:
            json.dump(index, f)

        return None

    def _handle_leaderboard_with_extras(self, submitted_name, load_errors):
        """Return leaderboard JSON after a submission, with extra metadata."""
        index_path = Path(self.results_dir) / "index.json"
        if not index_path.exists():
            self._send_json({"error": "Results not generated"}, 500)
            return

        with open(index_path) as f:
            index = json.load(f)

        agg = index.get("aggregate", {})

        def sort_key(kv):
            return kv[1].get("category_score", kv[1]["avg_return"])

        ranked = sorted(agg.items(), key=sort_key, reverse=True)

        strategies = []
        for rank, (name, stats) in enumerate(ranked, 1):
            strategies.append({
                "rank": rank,
                "name": name,
                "author": stats.get("author", ""),
                "avg_return": stats["avg_return"],
                "category_score": stats.get("category_score"),
                "category_averages": stats.get("category_averages"),
                "total_fees": stats["total_fees"],
                "avg_skip_rate": stats["avg_skip_rate"],
                "per_market": stats["per_market"],
            })

        self._send_json({
            "ok": True,
            "submitted": submitted_name,
            "strategies": strategies,
            "markets": index.get("markets", []),
            "config": index.get("config", {}),
            "load_errors": load_errors,
        })

    # ── helpers ──────────────────────────────────────────────

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
