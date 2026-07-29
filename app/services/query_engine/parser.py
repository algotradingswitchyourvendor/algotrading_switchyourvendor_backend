"""
Backend Query Parser — Converts free-text query strings into condition dicts.

This is the Python equivalent of the frontend tokenizer + parser.
It handles the same grammar:

    Expression  := OrExpr
    OrExpr      := AndExpr ("OR" AndExpr)*
    AndExpr     := NotExpr ("AND" NotExpr)*
    NotExpr     := "NOT" NotExpr | Comparison
    Comparison  := Identifier CompOp Value
                 | Identifier "BETWEEN" Value "AND" Value
                 | Identifier "CONTAINS" Value

Outputs a flat list of QueryCondition dicts suitable for the translator.

Security: No eval(). All parsing is manual character-by-character + recursive descent.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ── Token Types ──────────────────────────────────────────────────────────

class _TokenType:
    IDENTIFIER = "IDENTIFIER"
    NUMBER = "NUMBER"
    STRING = "STRING"
    OPERATOR = "OPERATOR"
    LOGICAL = "LOGICAL"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    BETWEEN = "BETWEEN"
    CONTAINS = "CONTAINS"
    EOF = "EOF"


class _Token:
    __slots__ = ("type", "value", "pos")

    def __init__(self, type: str, value: str, pos: int):
        self.type = type
        self.value = value
        self.pos = pos

    def __repr__(self):
        return f"Token({self.type}, {self.value!r})"


# ── Keywords ─────────────────────────────────────────────────────────────

_KEYWORDS = {
    "AND": _TokenType.LOGICAL,
    "OR": _TokenType.LOGICAL,
    "NOT": _TokenType.LOGICAL,
    "BETWEEN": _TokenType.BETWEEN,
    "CONTAINS": _TokenType.CONTAINS,
}

_COMPARISON_OPS_2CHAR = {">=", "<=", "==", "!="}
_COMPARISON_OPS_1CHAR = {">", "<"}


# ── Tokenizer ────────────────────────────────────────────────────────────

def _tokenize(query: str) -> list[_Token]:
    """Lex a query string into tokens."""
    tokens: list[_Token] = []
    pos = 0
    length = len(query)

    while pos < length:
        ch = query[pos]

        # Skip whitespace
        if ch.isspace():
            pos += 1
            continue

        start = pos

        # Two-char operators
        if pos + 1 < length:
            two = query[pos:pos + 2]
            if two in _COMPARISON_OPS_2CHAR:
                tokens.append(_Token(_TokenType.OPERATOR, two, start))
                pos += 2
                continue

        # Single-char operators
        if ch in _COMPARISON_OPS_1CHAR:
            tokens.append(_Token(_TokenType.OPERATOR, ch, start))
            pos += 1
            continue

        # Parentheses
        if ch == "(":
            tokens.append(_Token(_TokenType.LPAREN, "(", start))
            pos += 1
            continue
        if ch == ")":
            tokens.append(_Token(_TokenType.RPAREN, ")", start))
            pos += 1
            continue

        # Negative numbers (minus followed by digit, after operator/start/lparen)
        if ch == "-" and pos + 1 < length and query[pos + 1].isdigit():
            if not tokens or tokens[-1].type in (
                _TokenType.OPERATOR, _TokenType.LOGICAL,
                _TokenType.LPAREN, _TokenType.BETWEEN,
            ):
                num_str, pos = _read_number(query, pos)
                tokens.append(_Token(_TokenType.NUMBER, num_str, start))
                continue

        # Numbers
        if ch.isdigit() or ch == ".":
            num_str, pos = _read_number(query, pos)
            tokens.append(_Token(_TokenType.NUMBER, num_str, start))
            continue

        # String literals
        if ch in ('"', "'"):
            str_val, pos = _read_string(query, pos)
            tokens.append(_Token(_TokenType.STRING, str_val, start))
            continue

        # Backtick-quoted identifiers
        if ch == "`":
            end = pos + 1
            while end < length and query[end] != "`":
                end += 1
            value = query[pos + 1:end]
            pos = end + 1 if end < length else end
            tokens.append(_Token(_TokenType.IDENTIFIER, value, start))
            continue

        # Identifiers and keywords
        if ch.isalpha() or ch == "_":
            end = pos
            while end < length and (query[end].isalnum() or query[end] == "_"):
                end += 1
            word = query[pos:end]
            upper = word.upper()
            if upper in _KEYWORDS:
                tokens.append(_Token(_KEYWORDS[upper], upper, start))
            else:
                tokens.append(_Token(_TokenType.IDENTIFIER, word, start))
            pos = end
            continue

        # Unknown character — skip
        logger.warning(f"Parser: skipping unexpected character '{ch}' at position {pos}")
        pos += 1

    tokens.append(_Token(_TokenType.EOF, "", pos))
    return tokens


def _read_number(query: str, pos: int) -> tuple[str, int]:
    """Read a numeric literal (int or float, possibly negative)."""
    start = pos
    if query[pos] == "-":
        pos += 1
    has_dot = False
    while pos < len(query):
        if query[pos] == ".":
            if has_dot:
                break
            has_dot = True
            pos += 1
        elif query[pos].isdigit():
            pos += 1
        else:
            break
    return query[start:pos], pos


def _read_string(query: str, pos: int) -> tuple[str, int]:
    """Read a quoted string literal."""
    quote = query[pos]
    pos += 1
    start = pos
    while pos < len(query) and query[pos] != quote:
        if query[pos] == "\\":
            pos += 1  # skip escaped char
        pos += 1
    value = query[start:pos]
    if pos < len(query):
        pos += 1  # consume closing quote
    return value, pos


# ── Parser ───────────────────────────────────────────────────────────────

class ParseError(Exception):
    """Raised when the query text cannot be parsed."""
    pass


class _Parser:
    """Recursive descent parser that produces a list of condition dicts."""

    def __init__(self, tokens: list[_Token]):
        self.tokens = tokens
        self.pos = 0
        self.conditions: list[dict] = []

    def peek(self) -> _Token:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return _Token(_TokenType.EOF, "", 0)

    def advance(self) -> _Token:
        tok = self.peek()
        if tok.type != _TokenType.EOF:
            self.pos += 1
        return tok

    def expect(self, type: str, value: str | None = None) -> _Token:
        tok = self.peek()
        if tok.type != type or (value is not None and tok.value != value):
            raise ParseError(
                f"Expected {value or type} but got '{tok.value}' at position {tok.pos}"
            )
        return self.advance()

    def parse(self) -> list[dict]:
        """Parse the entire expression into a flat condition list."""
        self._parse_or_expr("AND")

        if self.peek().type != _TokenType.EOF:
            raise ParseError(
                f"Unexpected token '{self.peek().value}' at position {self.peek().pos}"
            )

        return self.conditions

    def _parse_or_expr(self, inherited_logical: str):
        self._parse_and_expr(inherited_logical)
        while self.peek().type == _TokenType.LOGICAL and self.peek().value == "OR":
            self.advance()
            self._parse_and_expr("OR")

    def _parse_and_expr(self, inherited_logical: str):
        self._parse_not_expr(inherited_logical)
        while self.peek().type == _TokenType.LOGICAL and self.peek().value == "AND":
            self.advance()
            self._parse_not_expr("AND")

    def _parse_not_expr(self, inherited_logical: str):
        # NOT is treated as a flag — we don't support standalone NOT conditions
        # in the flat output. For now, skip NOT and parse the inner expression.
        if self.peek().type == _TokenType.LOGICAL and self.peek().value == "NOT":
            self.advance()
            # For flat condition list, NOT is not directly representable.
            # We parse the comparison and could negate the operator in the future.
            self._parse_comparison(inherited_logical)
            return
        self._parse_comparison(inherited_logical)

    def _parse_comparison(self, inherited_logical: str):
        tok = self.peek()

        # Parenthesized sub-expression
        if tok.type == _TokenType.LPAREN:
            self.advance()
            self._parse_or_expr(inherited_logical)
            self.expect(_TokenType.RPAREN)
            return

        # Must be an identifier (column name)
        if tok.type != _TokenType.IDENTIFIER:
            raise ParseError(
                f"Expected column name but got '{tok.value}' at position {tok.pos}"
            )

        column = self.advance().value

        # BETWEEN
        if self.peek().type == _TokenType.BETWEEN:
            self.advance()
            low = self._parse_value()
            self.expect(_TokenType.LOGICAL, "AND")
            high = self._parse_value()
            self.conditions.append({
                "column": column,
                "operator": "between",
                "value": [low, high],
                "logical": inherited_logical,
            })
            return

        # CONTAINS
        if self.peek().type == _TokenType.CONTAINS:
            self.advance()
            val = self._parse_value()
            self.conditions.append({
                "column": column,
                "operator": "contains",
                "value": val,
                "logical": inherited_logical,
            })
            return

        # Comparison operator
        if self.peek().type == _TokenType.OPERATOR:
            op = self.advance().value
            val = self._parse_value()
            self.conditions.append({
                "column": column,
                "operator": op,
                "value": val,
                "logical": inherited_logical,
            })
            return

        raise ParseError(
            f"Expected operator after column '{column}' at position {self.peek().pos}"
        )

    def _parse_value(self) -> Any:
        """Parse a literal value (number or string)."""
        tok = self.peek()

        if tok.type == _TokenType.NUMBER:
            self.advance()
            try:
                if "." in tok.value:
                    return float(tok.value)
                return int(tok.value)
            except ValueError:
                return tok.value

        if tok.type == _TokenType.STRING:
            self.advance()
            return tok.value

        if tok.type == _TokenType.IDENTIFIER:
            # Could be an unquoted string value
            self.advance()
            return tok.value

        raise ParseError(
            f"Expected value but got '{tok.value}' at position {tok.pos}"
        )


def _is_expression_query(query: str) -> bool:
    """
    Detects if a query uses expression features.
    We now route ALL comparison queries (e.g. High > Low, High > 100) to the expression engine
    since it properly handles RHS identifiers. The ONLY true legacy queries that must bypass
    the expression engine are those using BETWEEN or CONTAINS.
    """
    upper_q = query.upper()
    if "BETWEEN" in upper_q or "CONTAINS" in upper_q:
        return False
    return True


def parse_query_text(query_text: str) -> list[dict]:
    """
    Parse a free-text query string into a list of condition dicts.

    Args:
        query_text: e.g. "Volume > 500000 AND day_change_pct > 2"

    Returns:
        List of dicts: [{"column": "Volume", "operator": ">", "value": 500000, "logical": "AND"}, ...]
        Or Expression wrapper: [{"type": "expression", "ast": ...}]

    Raises:
        ParseError: If the query cannot be parsed
    """
    if not query_text or not query_text.strip():
        raise ParseError("Empty query")
        
    clean_query = query_text.strip()
    
    # 1. Early routing: Prevent legacy tokenizer from warning on math characters
    if _is_expression_query(clean_query):
        from app.services.query_engine.expression_parser import parse_expression
        return parse_expression(clean_query)

    # 2. True legacy query
    try:
        tokens = _tokenize(clean_query)
        parser = _Parser(tokens)
        return parser.parse()
    except ParseError as e:
        # Fallback to the new robust Expression AST parser in case the heuristic missed something
        # (e.g. deeply nested parens that fail legacy parsing)
        try:
            from app.services.query_engine.expression_parser import parse_expression
            return parse_expression(clean_query)
        except Exception as fallback_error:
            # If the expression parser also fails, raise the original error 
            # to preserve legacy test structures and error expectations
            raise ParseError(f"Failed to parse query: {fallback_error} (Legacy error: {e})")
