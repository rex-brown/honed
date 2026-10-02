"""Symbols a diff defines or changes, for finding the files that reference them.

Per-language regexes over hunk headers and changed lines (TS/JS, C/C++, Python, plus Go, Java/C#/Kotlin and Rust).
They are deliberately simple; tree-sitter would be more precise and is a later improvement.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass

from honed.core import patches

_ID = r"[A-Za-z_$][\w$]*"

EXTENSIONS = {
    "python": (".py", ".pyi", ".pyx"),
    "typescript": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
    "cpp": (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".ipp", ".inl", ".cu", ".cuh"),
    "go": (".go",),
    "jvm": (".java", ".kt", ".kts", ".scala", ".cs"),
    "rust": (".rs",),
}
HEADER_EXTENSIONS = (".h", ".hh", ".hpp", ".hxx", ".cuh", ".ipp", ".inl")

_DEFINITIONS = {
    "python": [
        rf"^\s*(?:async\s+)?def\s+({_ID})",
        rf"^\s*class\s+({_ID})",
        r"^([A-Z][A-Z0-9_]{2,})\s*(?::[^=]*)?=",  # module constants
    ],
    "typescript": [
        rf"\bfunction\s*\*?\s*({_ID})",
        rf"\bclass\s+({_ID})",
        rf"\b(?:interface|type|enum|namespace)\s+({_ID})",
        rf"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:const|let|var)\s+({_ID})\s*[=:]",
        rf"^\s*(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*({_ID})\s*"
        r"(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^={;]+)?\s*\{",
        rf"^\s*(?:(?:public|private|protected|static|readonly)\s+)*({_ID})\s*[=:]\s*(?:async\s*)?\([^)]*\)\s*(?::[^=]+)?=>",
    ],
    "cpp": [
        r"^\s*#\s*define\s+([A-Za-z_]\w*)",
        r"\b(?:struct|class|enum(?:\s+class)?|union|namespace|concept)\s+([A-Za-z_]\w*)",
        r"\busing\s+([A-Za-z_]\w*)\s*=",
        r"\btypedef\b.*?\b([A-Za-z_]\w*)\s*;",
        # a function definition or declaration: a return type, then name( ; qualified names keep the last part
        r"^\s*(?!(?:return|else|new|delete|throw|case|goto|co_return|co_await|co_yield)\b)"
        r"(?:template\s*<[^>]*>\s*)?(?:[\w:<>,*&~]+\s+)+[*&]*(?:[A-Za-z_]\w*::)*(~?[A-Za-z_]\w*)\s*\(",
    ],
    "go": [rf"^func\s+(?:\([^)]*\)\s*)?({_ID})", rf"^type\s+({_ID})", rf"^\s*({_ID})\s+func\s*\("],
    "jvm": [
        r"\b(?:class|interface|enum|record|struct|object|trait)\s+([A-Za-z_]\w*)",
        r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|synchronized|override|virtual|async|"
        r"open|suspend|fun)\s+)+(?:[\w<>\[\],.?]+\s+)?([A-Za-z_]\w*)\s*\(",
    ],
    "rust": [
        r"\bfn\s+([A-Za-z_]\w*)",
        r"\b(?:struct|enum|trait|type|mod|union)\s+([A-Za-z_]\w*)",
        r"\bmacro_rules!\s*([A-Za-z_]\w*)",
    ],
}
_GENERIC = [rf"\b(?:def|function|func|fn|class|struct|interface|enum|type)\s+({_ID})"]
_COMPILED = {lang: [re.compile(p) for p in ps] for lang, ps in _DEFINITIONS.items()}
_COMPILED_GENERIC = [re.compile(p) for p in _GENERIC]

# Names too common to identify anything: keywords, lifecycle hooks, generic verbs.
STOPWORDS = frozenset(
    [
        "if",
        "else",
        "for",
        "while",
        "do",
        "switch",
        "case",
        "catch",
        "try",
        "return",
        "new",
        "delete",
        "sizeof",
        "typeof",
        "instanceof",
        "await",
        "yield",
        "async",
        "const",
        "let",
        "var",
        "void",
        "int",
        "char",
        "bool",
        "float",
        "double",
        "long",
        "short",
        "unsigned",
        "signed",
        "auto",
        "static",
        "inline",
        "extern",
        "struct",
        "class",
        "enum",
        "union",
        "namespace",
        "template",
        "typename",
        "public",
        "private",
        "protected",
        "virtual",
        "override",
        "final",
        "operator",
        "this",
        "self",
        "cls",
        "super",
        "none",
        "null",
        "true",
        "false",
        "undefined",
        "function",
        "def",
        "fn",
        "func",
        "main",
        "init",
        "__init__",
        "__call__",
        "__repr__",
        "__str__",
        "__eq__",
        "__hash__",
        "constructor",
        "destructor",
        "render",
        "setup",
        "teardown",
        "get",
        "set",
        "run",
        "test",
        "tests",
        "call",
        "apply",
        "update",
        "create",
        "build",
        "make",
        "handle",
        "process",
        "start",
        "stop",
        "close",
        "open",
        "read",
        "write",
        "reset",
        "clear",
        "value",
        "values",
        "data",
        "name",
        "type",
        "key",
        "keys",
        "id",
        "index",
        "item",
        "items",
        "list",
        "dict",
        "map",
        "string",
        "number",
        "object",
        "props",
        "state",
        "config",
        "options",
        "args",
        "kwargs",
        "result",
        "error",
        "err",
        "ctx",
        "context",
        "default",
        "exports",
        "module",
        "require",
        "include",
        "import",
        "from",
        "export",
        "describe",
        "it",
        "expect",
        "assert",
        "then",
    ]
)
_TOO_GENERIC_MODULES = frozenset({"index", "utils", "util", "types", "type", "main", "__init__", "common", "base",
                                  "constants", "helpers", "mod", "lib", "test", "tests", "conftest"})  # fmt: skip


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str  # "definition", "hunk_context", "module" or "header"
    source: str  # the changed file it came from
    pattern: str  # the regex to search for (whole word), in the subset Python and PCRE share

    @property
    def reason(self) -> str:
        if self.kind == "module":
            return f"imports module `{self.name}` ({self.source})"
        if self.kind == "header":
            return f"includes `{self.name}` ({self.source})"
        return f"references `{self.name}`, changed in {self.source}"


def language_of(path: str) -> str | None:
    lowered = path.lower()
    return next((lang for lang, exts in EXTENSIONS.items() if lowered.endswith(exts)), None)


def _definitions(line: str, language: str | None) -> list[str]:
    names = []
    for pattern in _COMPILED.get(language or "", _COMPILED_GENERIC):
        names += [m.group(1) for m in pattern.finditer(line)]
    return names


def _usable(name: str) -> bool:
    return len(name) >= 3 and name.lower() not in STOPWORDS and not name.isdigit()


def _symbol(name: str, kind: str, source: str) -> Symbol:
    return Symbol(name=name, kind=kind, source=source, pattern=re.escape(name))


def from_patch(path: str, patch: str) -> list[Symbol]:
    """Names defined on changed lines, and the enclosing definitions named in hunk headers."""
    language = language_of(path)
    found: list[Symbol] = []
    for hunk in patches.parse_hunks(patch):
        for line in hunk.lines:
            if line[:1] in ("+", "-"):
                found += [_symbol(n, "definition", path) for n in _definitions(line[1:], language) if _usable(n)]
        if hunk.header:
            names = _definitions(hunk.header, language) or re.findall(r"([A-Za-z_]\w*)\s*\(", hunk.header)[:1]
            found += [_symbol(n, "hunk_context", path) for n in names if _usable(n)]
    return found


def from_path(path: str) -> list[Symbol]:
    """How other files name this one: an included header, or an imported module."""
    base = posixpath.basename(path)
    if base.lower().endswith(HEADER_EXTENSIONS):
        return [Symbol(name=base, kind="header", source=path, pattern=re.escape(base))]
    stem = base.split(".", 1)[0]
    if language_of(path) and _usable(stem) and stem.lower().lstrip("_") not in _TOO_GENERIC_MODULES:
        return [_symbol(stem, "module", path)]
    return []


_KIND_ORDER = {"definition": 0, "header": 1, "module": 2, "hunk_context": 3}


def rank(symbols: Iterable[Symbol], limit: int) -> list[Symbol]:
    """Distinct names, most telling first: definitions, then includes and imports, then enclosing functions;
    longer (more specific) names first within a kind."""
    best: dict[str, Symbol] = {}
    for s in symbols:
        if s.name not in best or _KIND_ORDER[s.kind] < _KIND_ORDER[best[s.name].kind]:
            best[s.name] = s
    ordered = sorted(best.values(), key=lambda s: (_KIND_ORDER[s.kind], -len(s.name), s.name))
    return ordered[:limit]
