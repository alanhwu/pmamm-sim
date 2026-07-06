"""HTTP server with API routes for the competition frontend.

SECURITY NOTE: Submitted strategy code is executed with full Python privileges
during simulation. There is currently no sandboxing beyond AST checks. Only run
this server in trusted environments. See pmamm_sim/sandbox.py for details.
"""

import ast
import hashlib
import json
import multiprocessing as mp
import os
import queue
import re
import socket
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

from pmamm_sim.batch import (
    load_manifest, sanitize_filename, _run_one_market, compute_competition_metrics,
)
from pmamm_sim.data_loader import load_polymarket_trades
from pmamm_sim.loader import load_submissions
from pmamm_sim.sandbox import validate_code_safety
from pmamm_sim.strategies import STRATEGY_REGISTRY


def preload_market_data(
    data_folder: str | Path,
    *,
    include_hidden: bool = False,
) -> tuple[dict, list[dict]]:
    """Load manifest and all trade data once. Returns (manifest, market_specs)."""
    data_folder = Path(data_folder)
    source_manifest = load_manifest(data_folder / "manifest.json")

    market_specs = []
    selected_markets = []
    for spec in source_manifest["markets"]:
        if spec.get("hidden", False) and not include_hidden:
            continue
        selected_markets.append(spec)
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

    manifest = dict(source_manifest)
    manifest["markets"] = selected_markets
    total = len(source_manifest.get("markets", []))
    hidden = sum(1 for s in source_manifest.get("markets", []) if s.get("hidden", False))
    print(
        f"Preloaded {len(market_specs)} markets from {data_folder} "
        f"(include_hidden={include_hidden}, total={total}, hidden={hidden})"
    )
    return manifest, market_specs


def sanitize_index_for_serving(index: dict) -> dict:
    """Return a copy of an index dict safe to send to competitors.

    Hidden markets stay in the ON-DISK index (so _refresh_aggregate_scores can
    fold them into the score), but must never be served: this strips them from
    the top-level market list and from every strategy's per_market. Aggregate
    scores are left untouched — they were computed over the full set including
    hidden markets. Market list and per_market are filtered by the same
    `hidden` predicate, preserving the positional alignment the visualizer
    relies on.
    """
    out = dict(index)
    out["markets"] = [m for m in index.get("markets", []) if not m.get("hidden")]
    agg = index.get("aggregate", {})
    sanitized_agg = {}
    for name, stats in agg.items():
        s = dict(stats)
        s["per_market"] = [m for m in (stats.get("per_market") or []) if not m.get("hidden")]
        sanitized_agg[name] = s
    out["aggregate"] = sanitized_agg
    return out


def sanitize_strategy_file_for_serving(doc: dict) -> dict:
    """Strip hidden market rows from a per-strategy index file before serving."""
    out = dict(doc)
    out["markets"] = [m for m in doc.get("markets", []) if not m.get("hidden")]
    return out


class CompetitionHandler(SimpleHTTPRequestHandler):
    """Extends static file serving with competition API routes.

    Class-level config (set before starting the server):
        serve_root:      directory to serve static files from
        data_folder:     path to data/ with manifest.json
        results_dir:     path to results/ output
        submissions_dir: path to submissions/
        liquidity:       initial LP deposit per market
        include_builtins: whether to include built-in strategies in runs
        include_hidden:  whether to include manifest hidden markets
        manifest:        preloaded manifest dict
        market_specs:    preloaded market specs list
    """

    serve_root: str = "."
    data_folder: str = "data"
    results_dir: str = "results"
    submissions_dir: str = "submissions"
    liquidity: float = 10_000
    include_builtins: bool = False
    include_hidden: bool = False
    run_timeout: int = 120
    manifest: dict = {}
    market_specs: list = []
    _submit_lock = threading.RLock()
    _jobs_lock = threading.RLock()
    _job_queue: queue.Queue[str] = queue.Queue()
    _jobs: dict[str, dict] = {}
    _worker_thread: threading.Thread | None = None
    _active_job_id: str | None = None
    _default_poll_ms: int = 1500

    def __init__(self, *args, **kwargs):
        self._ensure_job_worker()
        super().__init__(*args, directory=self.serve_root, **kwargs)

    def log_message(self, format, *args):
        if "/api/" in str(args[0]) if args else False:
            super().log_message(format, *args)

    # ── routing ──────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/leaderboard":
            self._handle_leaderboard()
        elif path == "/api/submissions":
            self._handle_list_submissions()
        elif path.startswith("/api/jobs/"):
            self._handle_job_status(path.removeprefix("/api/jobs/"))
        elif self._is_results_index_json(path):
            self._serve_sanitized_results_json(path)
        else:
            super().do_GET()

    def _is_results_index_json(self, path: str) -> bool:
        """True for the served index.json / per-strategy index files under the
        results dir (the raw files the visualizer fetches). Deep per-market
        files (results/<strat>/market_XXXX.json) are already hidden-free and
        pass straight through."""
        prefix = f"/{Path(self.results_dir).name}/"
        if not path.startswith(prefix) or not path.endswith(".json"):
            return False
        rest = path[len(prefix):]
        return "/" not in rest  # top-level only: index.json or <strategy>.json

    def _serve_sanitized_results_json(self, path: str):
        rest = path[len(f"/{Path(self.results_dir).name}/"):]
        fs_path = Path(self.results_dir) / rest
        if not fs_path.is_file():
            super().do_GET()
            return
        try:
            with open(fs_path) as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            super().do_GET()
            return
        if isinstance(doc, dict) and "aggregate" in doc:
            doc = sanitize_index_for_serving(doc)
        elif isinstance(doc, dict) and "markets" in doc:
            doc = sanitize_strategy_file_for_serving(doc)
        self._send_json(doc)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/submit":
            self._handle_submit()
        else:
            self._send_json({"error": "Not found"}, 404)

    # ── API handlers ─────────────────────────────────────────

    def _handle_leaderboard(self):
        self._send_json(self._build_leaderboard_payload())

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

        self._ensure_job_worker()
        job_id = uuid.uuid4().hex
        submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        submitted_mono = time.monotonic()
        request_ip = self.client_address[0] if self.client_address else ""
        code_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()

        with self._jobs_lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "requested_name": name,
                "author": author,
                "description": description,
                "submitted_at": submitted_at,
                "started_at": None,
                "finished_at": None,
                "submitted_mono": submitted_mono,
                "started_mono": None,
                "queue_wait_ms": None,
                "run_duration_ms": None,
                "total_latency_ms": None,
                "error_type": None,
                "error": None,
                "result": None,
                "request_ip": request_ip,
                "code_sha256": code_sha256,
                "payload": {
                    "name": name,
                    "author": author,
                    "description": description,
                    "code": code,
                },
            }

        self._job_queue.put(job_id)
        queue_position = self._get_queue_position(job_id, "queued")
        self._append_job_event(
            job_id,
            "queued",
            queue_position=queue_position,
            requested_name=name,
            author=author,
            request_ip=request_ip,
            code_sha256=code_sha256,
        )

        self._send_json({
            "ok": True,
            "job_id": job_id,
            "status": "queued",
            "queue_position": queue_position,
            "poll_after_ms": self._default_poll_ms,
            "submitted_at": submitted_at,
            "submitted_name": name,
        }, status=202)

    def _handle_job_status(self, job_id: str):
        if not job_id:
            self._send_json({"error": "Job id is required"}, 400)
            return

        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                self._send_json({"error": "Job not found"}, 404)
                return
            payload = {
                "ok": True,
                "job_id": job["job_id"],
                "status": job["status"],
                "requested_name": job["requested_name"],
                "author": job["author"],
                "submitted_at": job["submitted_at"],
                "started_at": job["started_at"],
                "finished_at": job["finished_at"],
                "queue_wait_ms": job.get("queue_wait_ms"),
                "run_duration_ms": job.get("run_duration_ms"),
                "total_latency_ms": job.get("total_latency_ms"),
                "error_type": job.get("error_type"),
                "error": job["error"],
                "result": job["result"],
                "poll_after_ms": self._default_poll_ms,
            }

        payload["queue_position"] = self._get_queue_position(job_id, payload["status"])
        self._send_json(payload)

    @classmethod
    def _ensure_job_worker(cls):
        with cls._jobs_lock:
            if cls._worker_thread is not None and cls._worker_thread.is_alive():
                return
            cls._worker_thread = threading.Thread(
                target=cls._job_worker_loop,
                name="submission-job-worker",
                daemon=True,
            )
            cls._worker_thread.start()

    @classmethod
    def _job_worker_loop(cls):
        while True:
            job_id = cls._job_queue.get()
            try:
                cls._run_one_queued_job(job_id)
            finally:
                cls._job_queue.task_done()

    @classmethod
    def _run_one_queued_job(cls, job_id: str):
        with cls._jobs_lock:
            job = cls._jobs.get(job_id)
            if not job:
                return
            payload = job.get("payload", {})
            started_mono = time.monotonic()
            submitted_mono = job.get("submitted_mono")
            queue_wait_ms = None
            if isinstance(submitted_mono, (int, float)):
                queue_wait_ms = int((started_mono - submitted_mono) * 1000)
            job["status"] = "running"
            job["started_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            job["started_mono"] = started_mono
            job["queue_wait_ms"] = queue_wait_ms
            cls._active_job_id = job_id

        cls._append_job_event(
            job_id,
            "running",
            queue_wait_ms=queue_wait_ms,
            requested_name=job.get("requested_name"),
            author=job.get("author"),
            request_ip=job.get("request_ip", ""),
            code_sha256=job.get("code_sha256"),
        )

        handler = cls.__new__(cls)

        try:
            result = handler._execute_submission(
                name=payload.get("name", ""),
                author=payload.get("author", "anonymous"),
                description=payload.get("description", ""),
                code=payload.get("code", ""),
                job_id=job_id,
            )
            finished_mono = time.monotonic()
            finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with cls._jobs_lock:
                current = cls._jobs.get(job_id)
                if current:
                    current["status"] = "succeeded"
                    current["finished_at"] = finished_at
                    current["error_type"] = None
                    current["result"] = result
                    current["error"] = None
                    cls._set_timing_fields(current, finished_mono)
                    current.pop("payload", None)
            submitted = result.get("submitted_result") or {}
            cls._append_job_event(
                job_id,
                "succeeded",
                requested_name=current.get("requested_name") if current else payload.get("name", ""),
                submitted_name=result.get("submitted", ""),
                author=current.get("author") if current else payload.get("author", ""),
                queue_wait_ms=current.get("queue_wait_ms") if current else None,
                run_duration_ms=current.get("run_duration_ms") if current else None,
                total_latency_ms=current.get("total_latency_ms") if current else None,
                category_score=submitted.get("category_score"),
                category_score_raw=submitted.get("category_score_raw"),
                avg_return=submitted.get("avg_return"),
                avg_skip_rate=submitted.get("avg_skip_rate"),
                avg_fill_rate=submitted.get("avg_fill_rate"),
                disqualified=submitted.get("disqualified"),
                kept_on_leaderboard=submitted.get("kept_on_leaderboard"),
                author_rank=submitted.get("author_rank"),
                author_total=submitted.get("author_total"),
            )
        except TimeoutError as e:
            finished_mono = time.monotonic()
            finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            error_message = cls._trim_text(str(e), 500)
            with cls._jobs_lock:
                current = cls._jobs.get(job_id)
                if current:
                    current["status"] = "timed_out"
                    current["finished_at"] = finished_at
                    current["error_type"] = "TimeoutError"
                    current["error"] = error_message
                    cls._set_timing_fields(current, finished_mono)
                    current.pop("payload", None)
            cls._append_job_event(
                job_id,
                "timed_out",
                requested_name=current.get("requested_name") if current else payload.get("name", ""),
                author=current.get("author") if current else payload.get("author", ""),
                queue_wait_ms=current.get("queue_wait_ms") if current else None,
                run_duration_ms=current.get("run_duration_ms") if current else None,
                total_latency_ms=current.get("total_latency_ms") if current else None,
                error_type="TimeoutError",
                error=error_message,
            )
        except Exception as e:
            finished_mono = time.monotonic()
            finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            error_type = type(e).__name__
            error_message = cls._trim_text(str(e), 500)
            with cls._jobs_lock:
                current = cls._jobs.get(job_id)
                if current:
                    current["status"] = "failed"
                    current["finished_at"] = finished_at
                    current["error_type"] = error_type
                    current["error"] = error_message
                    cls._set_timing_fields(current, finished_mono)
                    current.pop("payload", None)
            cls._append_job_event(
                job_id,
                "failed",
                requested_name=current.get("requested_name") if current else payload.get("name", ""),
                author=current.get("author") if current else payload.get("author", ""),
                queue_wait_ms=current.get("queue_wait_ms") if current else None,
                run_duration_ms=current.get("run_duration_ms") if current else None,
                total_latency_ms=current.get("total_latency_ms") if current else None,
                error_type=error_type,
                error=error_message,
            )
        finally:
            with cls._jobs_lock:
                if cls._active_job_id == job_id:
                    cls._active_job_id = None

    def _execute_submission(
        self,
        *,
        name: str,
        author: str,
        description: str,
        code: str,
        job_id: str | None = None,
    ) -> dict:
        """Run one queued submission end-to-end and return API payload."""
        file_content = (
            f'"""Submitted via web UI."""\n\n'
            f"from pmamm_sim.types import FeeQuote, PendingTrade, TradeInfo\n\n"
            f"{code}\n\n"
            f"STRATEGY_NAME = {name!r}\n"
            f"AUTHOR = {author!r}\n"
            f"DESCRIPTION = {description!r}\n"
        )
        ast.parse(file_content)

        with self._submit_lock:
            sdir = Path(self.submissions_dir)
            sdir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
            safe_name = re.sub(r"_+", "_", safe_name).strip("_").lower()

            # If this name already exists in the index, make it unique.
            index_path = Path(self.results_dir) / "index.json"
            if index_path.exists():
                with open(index_path) as f:
                    existing = set(json.load(f).get("strategies", []))
                if name in existing:
                    i = 2
                    while f"{name} ({i})" in existing:
                        i += 1
                    name = f"{name} ({i})"
                    file_content = (
                        f'"""Submitted via web UI."""\n\n'
                        f"from pmamm_sim.types import FeeQuote, PendingTrade, TradeInfo\n\n"
                        f"{code}\n\n"
                        f"STRATEGY_NAME = {name!r}\n"
                        f"AUTHOR = {author!r}\n"
                        f"DESCRIPTION = {description!r}\n"
                    )
                    safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
                    safe_name = re.sub(r"_+", "_", safe_name).strip("_").lower()

            filepath = sdir / f"{safe_name}.py"
            filepath.write_text(file_content)
            if job_id:
                self._append_job_event(
                    job_id,
                    "submission_saved",
                    requested_name=name,
                    submitted_name=name,
                    strategy_path=str(filepath),
                )

            error = self._run_sweep_with_timeout(rerun_name=name)
            if error:
                if error.startswith("Timed out"):
                    raise TimeoutError(error)
                raise RuntimeError(error)

            submitted_result = self._get_submission_result(name)
            pruned_names = self._prune_submissions(author=None, keep=2)
            if job_id and pruned_names:
                self._append_job_event(
                    job_id,
                    "prune_completed",
                    pruned_count=len(pruned_names),
                    pruned_names=pruned_names[:20],
                )
            if submitted_result is not None:
                submitted_result["kept_on_leaderboard"] = self._strategy_exists(name)

            leaderboard = self._build_leaderboard_payload()

        return {
            "submitted": name,
            "submitted_result": submitted_result,
            "leaderboard": leaderboard,
            "load_errors": [],
        }

    def _run_sweep_with_timeout(self, rerun_name: str | None = None) -> str | None:
        """Run only the NEW strategy and merge into existing index. Returns error string or None."""
        results_dir = Path(self.results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)
        market_specs = self.market_specs
        liquidity = self.liquidity

        # Load existing index (if any) to preserve previous results
        index_path = results_dir / "index.json"
        if index_path.exists():
            with open(index_path) as f:
                index = json.load(f)
            self._refresh_aggregate_scores(index)
            index_include_hidden = index.get("config", {}).get("include_hidden")
            if index_include_hidden is None:
                index_include_hidden = True
            if index_include_hidden != self.include_hidden:
                mode = "include hidden markets" if self.include_hidden else "exclude hidden markets"
                return (
                    "Results mode mismatch for this results directory. "
                    f"Current server is configured to {mode}, but existing index "
                    "was generated with a different hidden-market setting. "
                    "Use a separate --results-dir for public vs final scoring."
                )
        else:
            index = {
                "config": {
                    "initial_liquidity": liquidity,
                    "data_folder": self.data_folder,
                    "include_hidden": self.include_hidden,
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
        if rerun_name is not None:
            new_strategies = [s for s in new_strategies if s["name"] == rerun_name]

        if not new_strategies and not all_strategies:
            return "No valid strategies to run"
        if rerun_name is not None and not new_strategies:
            return f'Submitted strategy "{rerun_name}" failed to load'

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

            deadline = time.monotonic() + self.run_timeout
            worker_count = max(1, min(len(tasks), os.cpu_count() or 1))
            pool = mp.Pool(processes=worker_count)
            timed_out = False
            try:
                async_results = [pool.apply_async(_run_one_market, (task,)) for task in tasks]
                results = []
                for async_result in async_results:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        results.append(async_result.get(timeout=remaining))
                    except mp.TimeoutError:
                        timed_out = True
                        break
            finally:
                if timed_out:
                    pool.terminate()
                else:
                    pool.close()
                pool.join()

            if timed_out:
                return f"Timed out after {self.run_timeout}s"

            per_market_agg = [r for r in results if r is not None]

            # Write strategy index. Every market is stored (tagged `hidden`) so
            # the score recompute in _refresh_aggregate_scores sees the full
            # set. Hidden markets are stripped at SERVE time so competitors
            # never see their per-market performance on the hidden set.
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
                r["hidden"] = hidden  # tag per_market_agg entry (same object)
                entry = {
                    "question": r["question"],
                    "hidden": hidden,
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
            metrics = compute_competition_metrics(per_market_agg)

            index["strategies"].append(strat_name)
            index["strategy_files"][strat_name] = strat_dir_name + ".json"
            index["aggregate"][strat_name] = {
                "author": strat_spec.get("author", ""),
                "avg_return": metrics["avg_return"],
                "category_score": metrics["category_score"],
                "category_score_raw": metrics["category_score_raw"],
                "category_averages": metrics["category_averages"],
                "total_fees": metrics["total_fees"],
                "total_executed": metrics["total_executed"],
                "total_skipped": metrics["total_skipped"],
                "avg_skip_rate": metrics["avg_skip_rate"],
                "avg_fill_rate": metrics["avg_fill_rate"],
                "disqualified": metrics["disqualified"],
                "per_market": per_market_agg,
            }

        # Update timestamp and write index
        index["config"]["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_json_atomic(index_path, index)

        return None

    def _prune_submissions(self, author: str | None = None, keep: int = 2) -> list[str]:
        """Keep only the top N submissions per author.

        If author is provided, prunes only that author. If author is None, prunes all
        authors in one pass.
        """
        import shutil

        target_author_key = (author or "").strip().casefold() if author is not None else None
        if author is not None and not target_author_key:
            return []

        index_path = Path(self.results_dir) / "index.json"
        if not index_path.exists():
            return []

        with open(index_path) as f:
            index = json.load(f)
        self._refresh_aggregate_scores(index)

        agg = index.setdefault("aggregate", {})
        strategies = index.setdefault("strategies", [])
        strategy_files = index.setdefault("strategy_files", {})

        # Bucket strategies by author
        by_author: dict[str, list[tuple[str, float]]] = {}
        for strat_name, stats in agg.items():
            stats_author = (stats.get("author", "") or "").strip().casefold()
            if target_author_key is not None and stats_author != target_author_key:
                continue
            score = stats.get("category_score", stats.get("avg_return", 0))
            by_author.setdefault(stats_author, []).append((strat_name, score))

        to_delete: list[str] = []
        for author_group in by_author.values():
            if len(author_group) <= keep:
                continue
            # Sort by score descending, mark the bottom ones for deletion.
            author_group.sort(key=lambda x: x[1], reverse=True)
            to_delete.extend([name for name, _ in author_group[keep:]])

        if not to_delete:
            return []

        for strat_name in to_delete:
            # Remove from index
            strat_file = strategy_files.pop(strat_name, "")
            agg.pop(strat_name, None)
            while strat_name in strategies:
                strategies.remove(strat_name)

            # Delete strategy results directory and strategy index file
            if strat_file:
                strat_dir = Path(self.results_dir) / strat_file.replace(".json", "")
                shutil.rmtree(strat_dir, ignore_errors=True)

                strat_index = Path(self.results_dir) / strat_file
                strat_index.unlink(missing_ok=True)

            # Delete submission file
            sdir = Path(self.submissions_dir)
            safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", strat_name)
            safe_name = re.sub(r"_+", "_", safe_name).strip("_").lower()
            sub_file = sdir / f"{safe_name}.py"
            sub_file.unlink(missing_ok=True)

        # Write updated index
        self._write_json_atomic(index_path, index)
        return to_delete

    def _handle_leaderboard_with_extras(
        self,
        submitted_name,
        load_errors,
        submitted_result=None,
    ):
        """Return leaderboard JSON after a submission, with extra metadata."""
        payload = self._build_leaderboard_payload()
        payload.update({
            "ok": True,
            "submitted": submitted_name,
            "submitted_result": submitted_result,
            "load_errors": load_errors,
        })
        self._send_json(payload)

    # ── helpers ──────────────────────────────────────────────

    def _build_leaderboard_payload(self) -> dict:
        index_path = Path(self.results_dir) / "index.json"
        if not index_path.exists():
            return {"strategies": [], "markets": [], "config": {}}

        with open(index_path) as f:
            index = json.load(f)
        self._refresh_aggregate_scores(index)

        agg = index.get("aggregate", {})
        ranked = sorted(
            agg.items(),
            key=lambda kv: kv[1].get("category_score", kv[1]["avg_return"]),
            reverse=True,
        )

        strategies = []
        for rank, (name, stats) in enumerate(ranked, 1):
            # Strip hidden markets from served per-market detail. Scores above
            # already include hidden markets; competitors must not see their
            # per-market performance on the hidden set.
            served_per_market = [
                m for m in (stats.get("per_market") or []) if not m.get("hidden")
            ]
            strategies.append({
                "rank": rank,
                "name": name,
                "author": stats.get("author", ""),
                "avg_return": stats["avg_return"],
                "category_score": stats.get("category_score"),
                "category_score_raw": stats.get("category_score_raw"),
                "category_averages": stats.get("category_averages"),
                "total_fees": stats["total_fees"],
                "avg_skip_rate": stats["avg_skip_rate"],
                "avg_fill_rate": stats.get("avg_fill_rate"),
                "disqualified": stats.get("disqualified", False),
                "per_market": served_per_market,
            })

        served_markets = [m for m in index.get("markets", []) if not m.get("hidden")]
        return {
            "strategies": strategies,
            "markets": served_markets,
            "config": index.get("config", {}),
        }

    @classmethod
    def _get_queue_position(cls, job_id: str, status: str) -> int | None:
        if status == "running":
            return 0
        if status != "queued":
            return None
        with cls._job_queue.mutex:
            queued = list(cls._job_queue.queue)
        if job_id in queued:
            return queued.index(job_id) + 1
        return None

    @classmethod
    def _append_job_event(cls, job_id: str, event: str, **extra):
        row = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "job_id": job_id,
            "event": event,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
        }
        row.update(extra)

        logs_path = Path(cls.results_dir) / "job_events.jsonl"
        logs_path.parent.mkdir(parents=True, exist_ok=True)
        with cls._jobs_lock:
            with open(logs_path, "a") as f:
                f.write(json.dumps(row) + "\n")

    @classmethod
    def _set_timing_fields(cls, job: dict, finished_mono: float):
        submitted_mono = job.get("submitted_mono")
        started_mono = job.get("started_mono")
        if isinstance(started_mono, (int, float)):
            job["run_duration_ms"] = int((finished_mono - started_mono) * 1000)
        if isinstance(submitted_mono, (int, float)):
            job["total_latency_ms"] = int((finished_mono - submitted_mono) * 1000)

    @staticmethod
    def _trim_text(value: str, max_len: int = 500) -> str:
        value = str(value)
        if len(value) <= max_len:
            return value
        return value[: max_len - 3] + "..."

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refresh_aggregate_scores(self, index: dict):
        """Recompute derived scoring fields from per-market summaries."""
        agg = index.get("aggregate", {})
        for stats in agg.values():
            per_market = stats.get("per_market") or []
            if not per_market:
                continue
            metrics = compute_competition_metrics(per_market)
            stats["avg_return"] = metrics["avg_return"]
            stats["category_score"] = metrics["category_score"]
            stats["category_score_raw"] = metrics["category_score_raw"]
            stats["category_averages"] = metrics["category_averages"]
            stats["total_fees"] = metrics["total_fees"]
            stats["total_executed"] = metrics["total_executed"]
            stats["total_skipped"] = metrics["total_skipped"]
            stats["avg_skip_rate"] = metrics["avg_skip_rate"]
            stats["avg_fill_rate"] = metrics["avg_fill_rate"]
            stats["disqualified"] = metrics["disqualified"]

    def _strategy_exists(self, strategy_name: str) -> bool:
        index_path = Path(self.results_dir) / "index.json"
        if not index_path.exists():
            return False
        with open(index_path) as f:
            index = json.load(f)
        return strategy_name in index.get("aggregate", {})

    def _get_submission_result(self, strategy_name: str) -> dict | None:
        """Return score details for a just-run strategy."""
        index_path = Path(self.results_dir) / "index.json"
        if not index_path.exists():
            return None

        with open(index_path) as f:
            index = json.load(f)
        self._refresh_aggregate_scores(index)

        agg = index.get("aggregate", {})
        stats = agg.get(strategy_name)
        if not stats:
            return None

        def score_of(item):
            return item[1].get("category_score", item[1].get("avg_return", 0.0))

        ranked = sorted(agg.items(), key=score_of, reverse=True)
        overall_rank = None
        for i, (name, _stats) in enumerate(ranked, 1):
            if name == strategy_name:
                overall_rank = i
                break

        author_key = (stats.get("author", "") or "").strip().casefold()
        by_author = [
            item for item in ranked
            if (item[1].get("author", "") or "").strip().casefold() == author_key
        ]
        author_rank = None
        for i, (name, _stats) in enumerate(by_author, 1):
            if name == strategy_name:
                author_rank = i
                break

        return {
            "name": strategy_name,
            "author": stats.get("author", ""),
            "avg_return": stats.get("avg_return", 0.0),
            "category_score": stats.get("category_score", stats.get("avg_return", 0.0)),
            "category_score_raw": stats.get("category_score_raw"),
            "avg_skip_rate": stats.get("avg_skip_rate", 0.0),
            "avg_fill_rate": stats.get("avg_fill_rate", 0.0),
            "disqualified": stats.get("disqualified", False),
            "overall_rank": overall_rank,
            "overall_total": len(ranked),
            "author_rank": author_rank,
            "author_total": len(by_author),
        }

    def _write_json_atomic(self, path: Path, payload: dict):
        """Atomically write JSON to avoid partial reads during concurrent access."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            Path(tmp_path).replace(path)
        finally:
            if Path(tmp_path).exists():
                Path(tmp_path).unlink(missing_ok=True)
