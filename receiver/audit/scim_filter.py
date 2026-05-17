"""SCIM 2.0 filter expression parser.

Implements the subset of RFC 7644 §3.4.2.2 that Strathon's audit log
needs. The grammar handled here::

    FILTER     = attrExp / logExp / "not" "(" FILTER ")" / "(" FILTER ")"
    attrExp    = attrPath SP compareOp SP compValue
    compareOp  = "eq" / "ne" / "co" / "sw" / "ew" / "gt" / "lt" / "ge" / "le"
    compValue  = false / null / true / number / string
    logExp     = FILTER SP ("and" / "or") SP FILTER

Operator precedence: NOT > AND > OR (matches RFC's left-to-right
with NOT binding tightest). Parentheses override.

Not supported (return ``ParseError``):

- ``pr`` (presence) operator. Filterable columns are all NOT NULL
  in the schema or have explicit nullability semantics that ``pr``
  wouldn't help with anyway. Add if a real query case appears.
- Value-path filters (``addresses[type eq "work"]``). Audit events
  have no nested JSON attributes worth deep-filtering in Stage 1.
- Schema URI prefixes (``urn:ietf:params:scim:schemas:Core:1.0:User``).
  Strathon's audit log is a flat schema; URIs are noise.

Compiled output is a tuple ``(where_clause, params)`` where
``where_clause`` is a parameterized SQL string suitable for
embedding in ``... WHERE {where_clause} ...`` and ``params`` is a
list of values to bind. The compiler ensures every comparison
references an allowlisted column; an attempt to filter on a
non-allowlisted attribute raises ``ParseError``.

Tests live in ``tests/test_audit_scim_filter.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class ParseError(ValueError):
    """A SCIM filter that cannot be parsed or compiled."""


# --- Allowlist: filterable columns and their SQL types ------------------

# Maps SCIM attribute name → (sql_column, type_hint). The type hint
# drives coercion of the right-hand value in the compare expression.
_FILTERABLE: dict[str, tuple[str, str]] = {
    "occurred_at": ("occurred_at", "datetime"),
    "ingested_at": ("ingested_at", "datetime"),
    "project_id": ("project_id", "uuid"),
    "actor_type": ("actor_type", "text"),
    "actor_id": ("actor_id", "text"),
    "actor_display": ("actor_display", "text"),
    "action": ("action", "text"),
    "action_category": ("action_category", "text"),
    "outcome": ("outcome", "text"),
    "reason": ("reason", "text"),
    "resource_type": ("resource_type", "text"),
    "resource_id": ("resource_id", "text"),
    "resource_parent": ("resource_parent", "text"),
    "cascade_root_id": ("cascade_root_id", "uuid"),
    "request_id": ("request_id", "uuid"),
    "api_key_id": ("api_key_id", "text"),
    "auth_method": ("auth_method", "text"),
}


_COMPARE_OPS: dict[str, str] = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "ge": ">=",
    "lt": "<",
    "le": "<=",
}
# co/sw/ew compile to LIKE with specific anchors.
_TEXT_OPS: frozenset[str] = frozenset({"co", "sw", "ew"})


# --- Tokenizer ----------------------------------------------------------

@dataclass(frozen=True)
class Token:
    kind: str  # IDENT | STRING | NUMBER | LPAREN | RPAREN | AND | OR | NOT | OP | EOF
    value: Any
    pos: int


_TOKEN_PATTERN = re.compile(
    r"""
    (?P<WS>\s+)
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<STRING>"(?:[^"\\]|\\.)*")
    | (?P<NUMBER>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)

_KEYWORDS: dict[str, str] = {
    "and": "AND",
    "or": "OR",
    "not": "NOT",
    "true": "TRUE",
    "false": "FALSE",
    "null": "NULL",
}


def _tokenize(s: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i < len(s):
        m = _TOKEN_PATTERN.match(s, i)
        if not m:
            raise ParseError(f"unexpected character at position {i}: {s[i]!r}")
        kind = m.lastgroup
        text = m.group()
        end = m.end()
        if kind == "WS":
            i = end
            continue
        if kind == "STRING":
            # Strip surrounding quotes and unescape minimal subset.
            unquoted = text[1:-1].encode("utf-8").decode("unicode_escape")
            tokens.append(Token("STRING", unquoted, i))
        elif kind == "NUMBER":
            num: float | int = float(text) if "." in text or "e" in text or "E" in text else int(text)
            tokens.append(Token("NUMBER", num, i))
        elif kind == "IDENT":
            low = text.lower()
            if low in _KEYWORDS:
                tokens.append(Token(_KEYWORDS[low], low, i))
            elif low in _COMPARE_OPS or low in _TEXT_OPS:
                tokens.append(Token("OP", low, i))
            else:
                # Preserve original case for attribute lookup; the
                # allowlist keys are lowercase so we lowercase here.
                tokens.append(Token("IDENT", text, i))
        elif kind == "LPAREN":
            tokens.append(Token("LPAREN", "(", i))
        elif kind == "RPAREN":
            tokens.append(Token("RPAREN", ")", i))
        i = end
    tokens.append(Token("EOF", None, len(s)))
    return tokens


# --- AST ----------------------------------------------------------------

@dataclass(frozen=True)
class Compare:
    attr: str
    op: str
    value: Any


@dataclass(frozen=True)
class And:
    left: Any
    right: Any


@dataclass(frozen=True)
class Or:
    left: Any
    right: Any


@dataclass(frozen=True)
class Not:
    inner: Any


# --- Parser (recursive descent) -----------------------------------------

class _Parser:
    def __init__(self, tokens: list[Token]):
        self._tokens = tokens
        self._i = 0

    def _peek(self) -> Token:
        return self._tokens[self._i]

    def _advance(self) -> Token:
        tok = self._tokens[self._i]
        self._i += 1
        return tok

    def _expect(self, kind: str) -> Token:
        tok = self._advance()
        if tok.kind != kind:
            raise ParseError(
                f"expected {kind} at position {tok.pos}, got {tok.kind} "
                f"({tok.value!r})"
            )
        return tok

    def parse(self) -> Any:
        expr = self._parse_or()
        self._expect("EOF")
        return expr

    def _parse_or(self) -> Any:
        left = self._parse_and()
        while self._peek().kind == "OR":
            self._advance()
            right = self._parse_and()
            left = Or(left, right)
        return left

    def _parse_and(self) -> Any:
        left = self._parse_not()
        while self._peek().kind == "AND":
            self._advance()
            right = self._parse_not()
            left = And(left, right)
        return left

    def _parse_not(self) -> Any:
        if self._peek().kind == "NOT":
            self._advance()
            self._expect("LPAREN")
            inner = self._parse_or()
            self._expect("RPAREN")
            return Not(inner)
        return self._parse_atom()

    def _parse_atom(self) -> Any:
        tok = self._peek()
        if tok.kind == "LPAREN":
            self._advance()
            inner = self._parse_or()
            self._expect("RPAREN")
            return inner
        if tok.kind != "IDENT":
            raise ParseError(
                f"expected attribute name at position {tok.pos}, "
                f"got {tok.kind} ({tok.value!r})"
            )
        attr = self._advance().value
        op_tok = self._expect("OP")
        op = op_tok.value
        val_tok = self._advance()
        if val_tok.kind == "STRING":
            value: Any = val_tok.value
        elif val_tok.kind == "NUMBER":
            value = val_tok.value
        elif val_tok.kind == "TRUE":
            value = True
        elif val_tok.kind == "FALSE":
            value = False
        elif val_tok.kind == "NULL":
            value = None
        else:
            raise ParseError(
                f"expected literal at position {val_tok.pos}, "
                f"got {val_tok.kind} ({val_tok.value!r})"
            )
        return Compare(attr, op, value)


# --- Compiler: AST → parameterized SQL ----------------------------------

def parse(filter_expr: str) -> Any:
    """Parse a filter expression into an AST. Raises ParseError."""
    if not filter_expr or not filter_expr.strip():
        raise ParseError("empty filter expression")
    tokens = _tokenize(filter_expr)
    return _Parser(tokens).parse()


def compile_to_sql(filter_expr: str) -> tuple[str, list[Any]]:
    """Parse and compile a filter to ``(where_clause, params)``.

    The WHERE clause uses ``%s`` placeholders matching psycopg's
    positional binding. The caller is responsible for combining it
    with the rest of the query.
    """
    ast = parse(filter_expr)
    params: list[Any] = []
    where = _compile_node(ast, params)
    return where, params


def _compile_node(node: Any, params: list[Any]) -> str:
    if isinstance(node, Compare):
        return _compile_compare(node, params)
    if isinstance(node, And):
        return f"({_compile_node(node.left, params)} AND {_compile_node(node.right, params)})"
    if isinstance(node, Or):
        return f"({_compile_node(node.left, params)} OR {_compile_node(node.right, params)})"
    if isinstance(node, Not):
        return f"(NOT {_compile_node(node.inner, params)})"
    raise ParseError(f"internal: unknown AST node {type(node).__name__}")


def _compile_compare(node: Compare, params: list[Any]) -> str:
    if node.attr not in _FILTERABLE:
        raise ParseError(
            f"attribute {node.attr!r} is not filterable. "
            f"Filterable attributes: {sorted(_FILTERABLE)}"
        )
    column, type_hint = _FILTERABLE[node.attr]
    value = _coerce(node.value, type_hint, node.op)

    if node.op in _COMPARE_OPS:
        params.append(value)
        return f"{column} {_COMPARE_OPS[node.op]} %s"

    if node.op == "co":
        if not isinstance(value, str):
            raise ParseError(f"'co' operator requires string, got {type(value).__name__}")
        params.append(f"%{_escape_like(value)}%")
        return f"{column} LIKE %s"
    if node.op == "sw":
        if not isinstance(value, str):
            raise ParseError(f"'sw' operator requires string, got {type(value).__name__}")
        params.append(f"{_escape_like(value)}%")
        return f"{column} LIKE %s"
    if node.op == "ew":
        if not isinstance(value, str):
            raise ParseError(f"'ew' operator requires string, got {type(value).__name__}")
        params.append(f"%{_escape_like(value)}")
        return f"{column} LIKE %s"

    raise ParseError(f"unknown operator {node.op!r}")


def _coerce(value: Any, type_hint: str, op: str) -> Any:
    """Coerce a literal value to the column's expected Python type.

    Datetimes accept ISO 8601 strings; UUIDs accept hyphenated hex
    strings. Other types pass through. Coercion failure raises
    ParseError.
    """
    if value is None:
        return None
    if type_hint == "datetime":
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ParseError(f"invalid datetime literal {value!r}: {exc}") from exc
        raise ParseError(f"datetime column requires ISO 8601 string, got {type(value).__name__}")
    if type_hint == "uuid":
        import uuid as _uuid
        if isinstance(value, _uuid.UUID):
            return value
        if isinstance(value, str):
            try:
                return _uuid.UUID(value)
            except ValueError as exc:
                raise ParseError(f"invalid uuid literal {value!r}: {exc}") from exc
        raise ParseError(f"uuid column requires hex-string literal, got {type(value).__name__}")
    if type_hint == "text":
        if isinstance(value, str):
            return value
        raise ParseError(f"text column requires string literal, got {type(value).__name__}")
    return value


def _escape_like(value: str) -> str:
    """Escape SQL LIKE special characters in a literal substring."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
