"""HTTP server with API routes for the competition frontend.

SECURITY NOTE: Submitted strategy code is executed with full Python privileges
during simulation. There is currently no sandboxing. Only run this server in
trusted environments. Before exposing to untrusted users, add:
  - AST allowlist to reject dangerous imports/builtins (os, subprocess, eval, etc.)
  - Docker container isolation for each simulation run
  - Resource limits (CPU, memory, wall-clock time)
See also the WARNING printed by the CLI compete/serve commands.
"""

import ast
import json
import re
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

from pmamm_sim.batch import run_full_sweep
from pmamm_sim.loader import load_submissions
from pmamm_sim.sandbox import validate_code_safety


class CompetitionHandler(SimpleHTTPRequestHandler):
    """Extends static file serving with competition API routes.

    Class-level config (set before starting the server):
        serve_root:      directory to serve static files from
        data_folder:     path to data/ with manifest.json
        results_dir:     path to results/ output
        submissions_dir: path to submissions/
        liquidity:       initial LP deposit per market
        include_builtins: whether to include built-in strategies in runs
    """

    serve_root: str = "."
    data_folder: str = "data"
    results_dir: str = "results"
    submissions_dir: str = "submissions"
    liquidity: float = 10_000
    include_builtins: bool = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=self.serve_root, **kwargs)

    # Suppress per-request log spam
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

        # Syntax check before saving
        try:
            ast.parse(code)
        except SyntaxError as e:
            self._send_json({
                "error": f"Syntax error: {e.msg} (line {e.lineno})",
            }, 400)
            return

        # Safety check — reject dangerous imports, builtins, dunder access
        violations = validate_code_safety(code)
        if violations:
            self._send_json({
                "error": "Code blocked: " + "; ".join(violations),
            }, 400)
            return

        # Build the full file content
        file_content = (
            f'"""Submitted via web UI."""\n\n'
            f"from pmamm_sim.types import FeeQuote, PendingTrade, TradeInfo\n\n"
            f"STRATEGY_NAME = {name!r}\n"
            f"AUTHOR = {author!r}\n"
            f"DESCRIPTION = {description!r}\n\n\n"
            f"{code}\n"
        )

        # Check the combined file parses cleanly
        try:
            ast.parse(file_content)
        except SyntaxError as e:
            self._send_json({
                "error": f"Syntax error in generated file: {e.msg} (line {e.lineno})",
            }, 400)
            return

        # Save to submissions directory
        sdir = Path(self.submissions_dir)
        sdir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        safe_name = re.sub(r"_+", "_", safe_name).strip("_").lower()
        filepath = sdir / f"{safe_name}.py"
        filepath.write_text(file_content)

        # Run the full sweep with all submissions
        try:
            entries, errors = load_submissions(sdir)
            if not entries and not self.include_builtins:
                self._send_json({"error": "No valid strategies to run"}, 400)
                return

            run_full_sweep(
                data_folder=self.data_folder,
                results_dir=self.results_dir,
                liquidity=self.liquidity,
                extra_strategies=entries if entries else None,
                include_builtins=self.include_builtins,
            )
        except Exception as e:
            self._send_json({"error": f"Run failed: {e}"}, 500)
            return

        # Return the leaderboard
        load_errors = [{"filename": e.filename, "reason": e.reason} for e in errors]
        self._handle_leaderboard_with_extras(submitted_name=name, load_errors=load_errors)

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
