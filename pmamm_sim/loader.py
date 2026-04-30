"""Discover, load, and validate strategy submissions from a directory."""

import importlib.util
from pathlib import Path

from pmamm_sim.types import validate_strategy


class SubmissionError(Exception):
    """Raised when a submission file fails to load or validate."""

    def __init__(self, filename: str, reason: str):
        self.filename = filename
        self.reason = reason
        super().__init__(f"{filename}: {reason}")


def load_submission(filepath: Path) -> dict:
    """Load a single submission .py file and return a registry-compatible dict.

    Returns:
        {"name": str, "class": type, "kwargs": {}, "author": str,
         "description": str, "source": str}

    Raises:
        SubmissionError on any load/validation failure.
    """
    filename = filepath.name
    module_name = f"submission_{filepath.stem}"

    try:
        spec = importlib.util.spec_from_file_location(module_name, str(filepath))
        if spec is None or spec.loader is None:
            raise SubmissionError(filename, "Could not create module spec")

        module = importlib.util.module_from_spec(spec)
        # Do NOT add to sys.modules — keeps submissions isolated
        spec.loader.exec_module(module)
    except SubmissionError:
        raise
    except Exception as e:
        raise SubmissionError(filename, f"Import failed: {e}") from e

    # Find the Strategy class — try explicit name first, then auto-detect
    strategy_cls = getattr(module, "Strategy", None)
    if strategy_cls is not None and not isinstance(strategy_cls, type):
        strategy_cls = None

    if strategy_cls is None:
        # Fallback: find any class with both before_swap and after_swap
        import inspect
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module_name:
                continue  # skip imported classes (e.g. FeeQuote)
            if hasattr(obj, "before_swap") and hasattr(obj, "after_swap"):
                strategy_cls = obj
                break

    if strategy_cls is None:
        raise SubmissionError(
            filename,
            "No strategy class found. Define a class named 'Strategy' "
            "or any class with before_swap() and after_swap() methods."
        )

    # Instantiate with no args
    try:
        instance = strategy_cls()
    except Exception as e:
        raise SubmissionError(filename, f"Strategy() instantiation failed: {e}") from e

    # Validate the interface
    try:
        validate_strategy(instance, label=f"Strategy in {filename}")
    except TypeError as e:
        raise SubmissionError(filename, str(e)) from e

    # Read optional metadata
    name = getattr(module, "STRATEGY_NAME", filepath.stem)
    author = getattr(module, "AUTHOR", "anonymous")
    description = getattr(module, "DESCRIPTION", "")

    return {
        "name": str(name),
        "class": strategy_cls,
        "kwargs": {},
        "author": str(author),
        "description": str(description),
        "source": str(filepath),
    }


def load_submissions(
    directory: Path,
    *,
    fail_fast: bool = False,
) -> tuple[list[dict], list[SubmissionError]]:
    """Scan a directory for .py files, load each as a submission.

    Skips files starting with '_'.

    Returns:
        (strategies, errors) — list of registry-compatible dicts and list of errors.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Submissions directory not found: {directory}")

    strategies: list[dict] = []
    errors: list[SubmissionError] = []

    py_files = sorted(directory.glob("*.py"))
    if not py_files:
        return strategies, errors

    for filepath in py_files:
        if filepath.name.startswith("_"):
            continue
        try:
            entry = load_submission(filepath)
            strategies.append(entry)
        except SubmissionError as e:
            if fail_fast:
                raise
            errors.append(e)

    # Deduplicate names
    seen: dict[str, int] = {}
    for entry in strategies:
        name = entry["name"]
        if name in seen:
            seen[name] += 1
            entry["name"] = f"{name} ({seen[name]})"
        else:
            seen[name] = 1

    return strategies, errors
