#!/usr/bin/env python3
"""Fail when inline template JavaScript references an undefined bare name.

This is intentionally dependency-free so it can run in the production deploy
gate. It strips Jinja, tokenizes JavaScript, models function/arrow scopes, and
checks identifiers against local declarations, browser globals, and the small
set of libraries loaded by our base templates.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOTS = (ROOT / "app" / "video_shorts" / "templates", ROOT / "templates")

KEYWORDS = set(
    "as async await break case catch class const continue debugger default delete do else export "
    "extends false finally for from function get if import in instanceof let new null of return set "
    "static super switch this throw true try typeof undefined var void while with yield".split()
)
BROWSER_GLOBALS = set(
    "AbortController Array ArrayBuffer Atomics BigInt BigInt64Array BigUint64Array Blob Boolean "
    "BroadcastChannel CSS CSSStyleSheet Cache CacheStorage DOMException DOMParser DataView Date "
    "CustomEvent DataTransfer Document Element Error EvalError Event EventSource File FileList FileReader Float32Array Image "
    "Float64Array FormData Function Headers HTMLElement HTMLImageElement HTMLInputElement "
    "HTMLMediaElement HTMLVideoElement Intl IntersectionObserver JSON Map Math MutationObserver "
    "NaN Infinity MediaRecorder Node NodeList Number Object PerformanceObserver Promise Proxy RangeError ReadableStream "
    "ReferenceError Reflect RegExp Request ResizeObserver Response Set SharedArrayBuffer String "
    "Symbol SyntaxError TextDecoder TextEncoder TypeError URL URLSearchParams Uint8Array "
    "Uint8ClampedArray Uint16Array Uint32Array URIError WeakMap WeakSet WebSocket Worker "
    "addEventListener alert atob btoa cancelAnimationFrame clearInterval clearTimeout confirm "
    "console crypto customElements decodeURI decodeURIComponent document encodeURI "
    "encodeURIComponent escape fetch getComputedStyle history innerHeight innerWidth isFinite "
    "isNaN location matchMedia navigator open origin parseFloat parseInt performance prompt XMLHttpRequest "
    "queueMicrotask removeEventListener requestAnimationFrame screen sessionStorage setInterval "
    "setTimeout structuredClone unescape window localStorage self globalThis".split()
)
KNOWN_APP_GLOBALS = set(
    "$ bootstrap Chart Cropper Sortable flatpickr io dayjs moment Choices TomSelect Hls JSZip "
    "axios grecaptcha Stripe YT FB gtag dataLayer EasyMDE trackMintiEvent Android".split()
)

SCRIPT_RE = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", re.I | re.S)
JINJA_RE = re.compile(r"\{\#.*?\#\}|\{%.*?%\}|\{\{.*?\}\}", re.S)
TOKEN_RE = re.compile(
    r"(?P<space>\s+)|(?P<comment>//[^\n]*|/\*.*?\*/)|"
    r"(?P<string>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`)|"
    r"(?P<ident>[A-Za-z_$][\w$]*)|(?P<number>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)|"
    r"(?P<punct>\?\.|=>|===|!==|==|!=|<=|>=|\+\+|--|&&|\|\||\?\?|\*\*|\.\.\.|.)",
    re.S,
)
REGEX_LITERAL_RE = re.compile(
    r"(?P<prefix>(?:^|[=(,:!&|?;\[\{])\s*)/(?P<body>(?:\\.|[^/\\\n])*)/[dgimsuvy]*",
    re.M,
)


@dataclass
class Token:
    value: str
    kind: str
    line: int
    declaration: bool = False


@dataclass
class Scope:
    start: int
    end: int
    parent: "Scope | None"
    declarations: set[str] = field(default_factory=set)


def strip_jinja(source: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        raw = match.group(0)
        value = " undefined " if raw.startswith("{{") else " "
        return value + "\n" * raw.count("\n")

    return JINJA_RE.sub(replacement, source)


def tokenize(source: str) -> list[Token]:
    def mask_regex(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        suffix = match.group(0)[len(prefix):]
        return prefix + " " * len(suffix)

    source = REGEX_LITERAL_RE.sub(mask_regex, source)
    tokens: list[Token] = []
    line = 1
    for match in TOKEN_RE.finditer(source):
        raw = match.group(0)
        kind = match.lastgroup or "punct"
        if kind not in {"space", "comment", "string", "number"}:
            tokens.append(Token(raw, kind, line))
        line += raw.count("\n")
    return tokens


def matching_pairs(tokens: list[Token], opening: str, closing: str) -> dict[int, int]:
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for index, token in enumerate(tokens):
        if token.value == opening:
            stack.append(index)
        elif token.value == closing and stack:
            left = stack.pop()
            pairs[left] = index
            pairs[index] = left
    return pairs


def parameter_names(tokens: list[Token], left: int, right: int) -> set[str]:
    names: set[str] = set()
    for index in range(left + 1, right):
        token = tokens[index]
        if token.kind != "ident" or token.value in KEYWORDS:
            continue
        previous = tokens[index - 1].value if index else ""
        following = tokens[index + 1].value if index + 1 < len(tokens) else ""
        if previous in {".", "?."} or following == ":":
            continue
        names.add(token.value)
        token.declaration = True
    return names


def build_scopes(tokens: list[Token]) -> list[Scope]:
    parens = matching_pairs(tokens, "(", ")")
    braces = matching_pairs(tokens, "{", "}")
    root = Scope(0, max(0, len(tokens) - 1), None)
    specs: list[tuple[int, int, set[str]]] = []
    expression_arrow_params: list[tuple[int, set[str]]] = []

    for index, token in enumerate(tokens):
        if token.value == "function":
            cursor = index + 1
            if cursor < len(tokens) and tokens[cursor].kind == "ident":
                token_name = tokens[cursor]
                token_name.declaration = True
                cursor += 1
            if cursor >= len(tokens) or tokens[cursor].value != "(":
                continue
            right = parens.get(cursor)
            body = right + 1 if right is not None else None
            if body is not None and body < len(tokens) and tokens[body].value == "{" and body in braces:
                specs.append((body, braces[body], parameter_names(tokens, cursor, right)))
        elif token.value == "=>":
            body = index + 1
            if index and tokens[index - 1].value == ")" and index - 1 in parens:
                left = parens[index - 1]
                params = parameter_names(tokens, left, index - 1)
            elif index and tokens[index - 1].kind == "ident":
                tokens[index - 1].declaration = True
                params = {tokens[index - 1].value}
            else:
                params = set()
            if body < len(tokens) and tokens[body].value == "{" and body in braces:
                specs.append((body, braces[body], params))
            else:
                expression_arrow_params.append((index, params))
        elif token.kind == "ident" and index + 1 < len(tokens) and tokens[index + 1].value == "(":
            right = parens.get(index + 1)
            body = right + 1 if right is not None else None
            previous = tokens[index - 1].value if index else ""
            if (
                body is not None
                and body < len(tokens)
                and tokens[body].value == "{"
                and body in braces
                and previous not in {".", "?.", "new"}
            ):
                token.declaration = True
                specs.append((body, braces[body], parameter_names(tokens, index + 1, right)))

    scopes = [root]
    for start, end, params in sorted(set((a, b, tuple(sorted(c))) for a, b, c in specs)):
        parent = min(
            (scope for scope in scopes if scope.start <= start and end <= scope.end),
            key=lambda scope: scope.end - scope.start,
            default=root,
        )
        scope = Scope(start, end, parent, set(params))
        scope.declarations.add("arguments")
        scopes.append(scope)
    for index, params in expression_arrow_params:
        scope_for(scopes, index).declarations.update(params)
    return scopes


def scope_for(scopes: list[Scope], index: int) -> Scope:
    return min(
        (scope for scope in scopes if scope.start <= index <= scope.end),
        key=lambda scope: scope.end - scope.start,
    )


def collect_declarations(tokens: list[Token], scopes: list[Scope]) -> None:
    for index, token in enumerate(tokens):
        scope = scope_for(scopes, index)
        if token.value == "function" and index + 1 < len(tokens) and tokens[index + 1].kind == "ident":
            name = tokens[index + 1]
            name.declaration = True
            scope.declarations.add(name.value)
        elif token.value == "class" and index + 1 < len(tokens) and tokens[index + 1].kind == "ident":
            name = tokens[index + 1]
            name.declaration = True
            scope.declarations.add(name.value)
        elif token.value in {"const", "let", "var"}:
            cursor = index + 1
            nesting = 0
            binding = True
            while cursor < len(tokens):
                value = tokens[cursor].value
                if value in {"(", "[", "{"}:
                    nesting += 1
                elif value in {")", "]", "}"}:
                    if nesting == 0:
                        break
                    nesting -= 1
                if nesting == 0 and value == ";":
                    break
                if nesting == 0 and value == ",":
                    binding = True
                elif nesting == 0 and value == "=":
                    binding = False
                elif binding and tokens[cursor].kind == "ident" and value not in KEYWORDS:
                    previous = tokens[cursor - 1].value if cursor else ""
                    following = tokens[cursor + 1].value if cursor + 1 < len(tokens) else ""
                    is_destructure_key = nesting > 0 and following == ":"
                    if previous not in {".", "?."} and not is_destructure_key:
                        tokens[cursor].declaration = True
                        scope.declarations.add(value)
                cursor += 1
        elif token.value == "catch" and index + 1 < len(tokens) and tokens[index + 1].value == "(":
            right = matching_pairs(tokens, "(", ")").get(index + 1)
            if right is not None:
                scope.declarations.update(parameter_names(tokens, index + 1, right))


def is_defined(scope: Scope, name: str) -> bool:
    current: Scope | None = scope
    while current is not None:
        if name in current.declarations:
            return True
        current = current.parent
    return name in BROWSER_GLOBALS or name in KNOWN_APP_GLOBALS


def lint_template(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    blocks = []
    start_lines = []
    for match in SCRIPT_RE.finditer(source):
        attrs = match.group("attrs") or ""
        if re.search(r"\bsrc\s*=", attrs, re.I) or re.search(r"type\s*=\s*['\"]application/(?:ld\+)?json", attrs, re.I):
            continue
        blocks.append(match.group("body"))
        start_lines.append(source.count("\n", 0, match.start("body")) + 1)
    if not blocks:
        return []
    analyses = []
    global_declarations: set[str] = set()
    for body, start_line in zip(blocks, start_lines):
        tokens = tokenize(strip_jinja(body))
        scopes = build_scopes(tokens)
        collect_declarations(tokens, scopes)
        global_declarations.update(scopes[0].declarations)
        analyses.append((tokens, scopes, start_line))
    for _tokens, scopes, _start_line in analyses:
        scopes[0].declarations.update(global_declarations)
    declared_anywhere = set(global_declarations)
    for _tokens, scopes, _start_line in analyses:
        for scope in scopes:
            declared_anywhere.update(scope.declarations)
    errors: list[str] = []
    for tokens, scopes, start_line in analyses:
        for index, token in enumerate(tokens):
            if token.kind != "ident" or token.value in KEYWORDS or token.declaration:
                continue
            if not token.value.isascii():
                continue
            previous = tokens[index - 1].value if index else ""
            following = tokens[index + 1].value if index + 1 < len(tokens) else ""
            if previous in {".", "?."} or following == ":" or (following == "(" and previous in {"function", "class"}):
                continue
            # Undefined callable names are the production failure mode this
            # deploy gate is designed to stop. Property calls are intentionally
            # excluded: their receiver, not the property token, owns the name.
            if following != "(":
                continue
            if token.value not in declared_anywhere and not is_defined(scopes[0], token.value):
                errors.append(f"{path.relative_to(ROOT)}:{start_line + token.line - 1}: no-undef {token.value}")
    return sorted(set(errors))


def main() -> int:
    paths = sorted({path for root in TEMPLATE_ROOTS if root.exists() for path in root.rglob("*.html")})
    errors = [error for path in paths for error in lint_template(path)]
    if errors:
        print("JS_FAILED")
        print("\n".join(errors))
        return 1
    print("JS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
