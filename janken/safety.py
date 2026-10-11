"""Static safety screening for submitted bots.

Scans a bot's source with the `ast` module before it is ever executed and
reports violations: imports outside a stdlib allowlist, dangerous builtins
(open/eval/exec/...), dunder-attribute tricks used for sandbox escapes, and
dunder names hidden inside string literals (the attrgetter / str.format
attribute-walk escapes, which the plain attribute check can't see).

This is a lint-level gate, not a bulletproof sandbox — process isolation and
rlimits in the wrapper remain the real containment. Its job is to reject the
obvious: file access, network, subprocesses, and introspection escapes.
"""

import ast
import dataclasses
import re
from typing import List

# Modules a rock-paper-scissors bot could legitimately want. Everything else
# (os, sys, subprocess, socket, pathlib, ctypes, pickle, importlib, ...) is
# rejected by omission.
#
# `operator` is deliberately absent: nothing a bot needs from it lacks a
# one-line lambda/builtin equivalent, while attrgetter/methodcaller take a
# string argument that names a dunder and so sidesteps the attribute check
# (operator.attrgetter("__globals__")(play) reaches __builtins__.__import__).
ALLOWED_MODULES = {
    "abc", "array", "bisect", "collections", "copy", "dataclasses",
    "datetime", "decimal", "enum", "fractions", "functools", "heapq",
    "itertools", "json", "math", "numbers", "random", "re",
    "statistics", "string", "time", "typing",
}

# Builtins that read/write/execute things a bot has no business touching.
BANNED_NAMES = {
    "open": "file access",
    "eval": "dynamic code execution",
    "exec": "dynamic code execution",
    "compile": "dynamic code execution",
    "__import__": "dynamic imports",
    "input": "reading stdin breaks the match protocol",
    "breakpoint": "starts a debugger",
    "globals": "introspection escape",
    "locals": "introspection escape",
    "vars": "introspection escape",
    "getattr": "dynamic attribute access (dunder escape)",
    "setattr": "dynamic attribute access",
    "delattr": "dynamic attribute access",
    "exit": "kills the bot process",
    "quit": "kills the bot process",
    "help": "starts an interactive pager on stdin / imports every module",
}

# Dunder names that are fine to reference (bots often keep a test harness
# behind `if __name__ == "__main__":`).
ALLOWED_DUNDER_NAMES = {"__name__"}

# Dunder names allowed to appear *inside a string literal*. Only "__main__"
# earns a pass, for the ubiquitous `if __name__ == "__main__":` guard. Every
# other dunder in a string is a reflection escape the AST attribute check can't
# see: operator.attrgetter("__globals__")(fn) and the str.format attribute walk
# "{0.__class__.__mro__}".format(()) both smuggle the dunder as string data.
ALLOWED_DUNDER_STRINGS = {"__main__"}

_DUNDER_RE = re.compile(r"__\w+__")


@dataclasses.dataclass
class Violation:
    line: int
    message: str

    def __str__(self):
        return "line %d: %s" % (self.line, self.message)


def scan_source(source: str) -> List[Violation]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Violation(exc.lineno or 0, "syntax error: %s" % exc.msg)]

    violations = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)

        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_MODULES:
                    violations.append(Violation(
                        line, "import of '%s' is not allowed" % root))

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                violations.append(Violation(line, "relative imports are not allowed"))
            else:
                root = (node.module or "").split(".")[0]
                if root not in ALLOWED_MODULES:
                    violations.append(Violation(
                        line, "import from '%s' is not allowed" % root))

        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                violations.append(Violation(
                    line, "use of '%s' is not allowed (%s)"
                    % (node.id, BANNED_NAMES[node.id])))
            elif (node.id.startswith("__") and node.id.endswith("__")
                  and node.id not in ALLOWED_DUNDER_NAMES):
                violations.append(Violation(
                    line, "reference to '%s' is not allowed" % node.id))

        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                violations.append(Violation(
                    line, "dunder attribute access '.%s' is not allowed"
                    % node.attr))

        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _DUNDER_RE.findall(node.value):
                if match not in ALLOWED_DUNDER_STRINGS:
                    violations.append(Violation(
                        line, "string literal contains dunder '%s' "
                        "(reflection / attribute-walk escape)" % match))
                    break

    violations.sort(key=lambda v: v.line)
    return violations


def scan_file(path: str) -> List[Violation]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return scan_source(f.read())
