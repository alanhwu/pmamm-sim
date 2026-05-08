"""AST-based code safety validator for strategy submissions.

Walks the syntax tree before execution to reject dangerous patterns:
- Unauthorized imports (only math, statistics, pmamm_sim.types allowed)
- Dangerous builtins (exec, eval, open, __import__, etc.)
- Dunder attribute access (__class__, __subclasses__, __globals__, etc.)

This is NOT a sandbox — a determined attacker can bypass AST checks.
It catches casual misuse and obvious cheating (reading outcomes/trade data).
"""

import ast


ALLOWED_MODULES = {
    "math",
    "statistics",
    "collections",
    "dataclasses",
    "functools",
    "itertools",
    "pmamm_sim.types",
}

# For "from X import Y" — None means all names allowed
ALLOWED_FROM_IMPORTS: dict[str, set[str] | None] = {
    "math": None,
    "statistics": None,
    "collections": None,
    "dataclasses": None,
    "functools": None,
    "itertools": None,
    "pmamm_sim.types": {"FeeQuote", "PendingTrade", "TradeInfo"},
}

BLOCKED_BUILTINS = {
    "exec", "eval", "compile", "__import__", "open",
    "breakpoint", "exit", "quit", "input",
    "globals", "locals", "vars",
}

BLOCKED_ATTRIBUTES = {
    "__builtins__", "__import__", "__class__", "__subclasses__",
    "__globals__", "__code__", "__func__", "__bases__", "__mro__",
    "__dict__",
}


def validate_code_safety(code: str) -> list[str]:
    """Check code for dangerous patterns. Returns list of violation strings.

    Empty list means the code passed all checks.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Syntax error: {e.msg} (line {e.lineno})"]

    violations: list[str] = []

    for node in ast.walk(tree):
        # --- import checks ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    violations.append(
                        f"line {node.lineno}: import '{alias.name}' is not allowed. "
                        f"Allowed: {', '.join(sorted(ALLOWED_MODULES))}"
                    )

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module not in ALLOWED_FROM_IMPORTS:
                violations.append(
                    f"line {node.lineno}: import from '{module}' is not allowed. "
                    f"Allowed: {', '.join(sorted(ALLOWED_MODULES))}"
                )
            else:
                allowed_names = ALLOWED_FROM_IMPORTS[module]
                if allowed_names is not None and node.names:
                    for alias in node.names:
                        if alias.name not in allowed_names:
                            violations.append(
                                f"line {node.lineno}: cannot import '{alias.name}' from '{module}'. "
                                f"Allowed: {', '.join(sorted(allowed_names))}"
                            )

        # --- dangerous builtin calls ---
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_BUILTINS:
                violations.append(
                    f"line {node.lineno}: '{node.func.id}()' is not allowed"
                )

        # --- dunder attribute access (cheating / sandbox escape) ---
        elif isinstance(node, ast.Attribute):
            if node.attr in BLOCKED_ATTRIBUTES:
                violations.append(
                    f"line {node.lineno}: access to '{node.attr}' is not allowed"
                )

    return violations
