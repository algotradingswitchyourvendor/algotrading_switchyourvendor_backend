import re
from typing import Any

class ParseError(Exception):
    pass

class TokenType:
    IDENTIFIER = "IDENTIFIER"
    NUMBER = "NUMBER"
    STRING = "STRING"
    OPERATOR = "OPERATOR"
    LOGICAL = "LOGICAL"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    COMMA = "COMMA"
    EOF = "EOF"

class Token:
    __slots__ = ("type", "value", "pos")
    def __init__(self, type: str, value: str, pos: int):
        self.type = type
        self.value = value
        self.pos = pos
    def __repr__(self):
        return f"Token({self.type}, {self.value!r})"

# Shared frontend grammar definition
KEYWORDS = {"AND", "OR", "NOT"}
FUNCTIONS = {"ABS", "MIN", "MAX", "ROUND", "FLOOR", "CEIL", "SQRT", "LOG"}

# Allowed Schema Columns (Extracted from column_service.py)
ALLOWED_COLUMNS = {
    "Open", "High", "Low", "Close", "Last Price", "Average Price", "Current",
    "Net Change", "day_change_pct",
    "Volume", "Total Buy Quantity", "Total Sell Quantity", "AvgVolume",
    "Lower Circuit Limit", "Upper Circuit Limit",
    "Open Interest", "OI Day High", "OI Day Low",
    "Last Trade Time", "Fetch Timestamp",
    "Instrument", "trading_symbol", "exchange", "instrument_key"
}

def _tokenize(query: str) -> list[Token]:
    tokens = []
    pos = 0
    length = len(query)

    while pos < length:
        ch = query[pos]

        if ch.isspace():
            pos += 1
            continue

        start = pos

        # Multi-char operators
        if pos + 1 < length:
            two = query[pos:pos+2]
            if two in {">=", "<=", "==", "!="}:
                tokens.append(Token(TokenType.OPERATOR, two, start))
                pos += 2
                continue

        # Single-char operators
        if ch in {">", "<", "=", "+", "-", "*", "/", "%"}:
            tokens.append(Token(TokenType.OPERATOR, ch, start))
            pos += 1
            continue

        if ch == "(":
            tokens.append(Token(TokenType.LPAREN, "(", start))
            pos += 1
            continue
        if ch == ")":
            tokens.append(Token(TokenType.RPAREN, ")", start))
            pos += 1
            continue
        if ch == ",":
            tokens.append(Token(TokenType.COMMA, ",", start))
            pos += 1
            continue

        # Numbers
        if ch.isdigit() or ch == ".":
            match = re.match(r"^\d*\.?\d+", query[pos:])
            if match:
                val = match.group(0)
                tokens.append(Token(TokenType.NUMBER, val, start))
                pos += len(val)
                continue

        # Strings
        if ch in {"'", '"'}:
            quote = ch
            pos += 1
            str_val = []
            while pos < length and query[pos] != quote:
                str_val.append(query[pos])
                pos += 1
            if pos >= length:
                raise ParseError(f"Unterminated string at {start}")
            pos += 1
            tokens.append(Token(TokenType.STRING, "".join(str_val), start))
            continue

        # Backtick-quoted identifiers (for column names with spaces)
        if ch == "`":
            end = pos + 1
            while end < length and query[end] != "`":
                end += 1
            if end >= length:
                raise ParseError(f"Unterminated backtick identifier at {start}")
            val = query[pos + 1:end]
            tokens.append(Token(TokenType.IDENTIFIER, val, start))
            pos = end + 1
            continue

        # Identifiers & Keywords
        if ch.isalpha() or ch == "_":
            match = re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*", query[pos:])
            if match:
                val = match.group(0)
                upper_val = val.upper()
                if upper_val in KEYWORDS:
                    tokens.append(Token(TokenType.LOGICAL, upper_val, start))
                else:
                    tokens.append(Token(TokenType.IDENTIFIER, val, start))
                pos += len(val)
                continue

        raise ParseError(f"Unexpected character '{ch}' at position {pos}")

    tokens.append(Token(TokenType.EOF, "", pos))
    return tokens

class _ExpressionParser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return self.tokens[-1]

    def advance(self) -> Token:
        tok = self.peek()
        if tok.type != TokenType.EOF:
            self.pos += 1
        return tok

    def match(self, token_type: str, values: set[str] = None) -> bool:
        tok = self.peek()
        if tok.type == token_type:
            if values is None or tok.value.upper() in values:
                self.advance()
                return True
        return False

    def parse(self) -> dict:
        expr = self._parse_logical_or()
        if self.peek().type != TokenType.EOF:
            raise ParseError(f"Unexpected token '{self.peek().value}' at {self.peek().pos}")
        return expr

    def _parse_logical_or(self) -> dict:
        expr = self._parse_logical_and()
        while self.match(TokenType.LOGICAL, {"OR"}):
            op = "OR"
            right = self._parse_logical_and()
            expr = {"type": "LogicalExpression", "operator": op, "left": expr, "right": right}
        return expr

    def _parse_logical_and(self) -> dict:
        expr = self._parse_comparison()
        while self.match(TokenType.LOGICAL, {"AND"}):
            op = "AND"
            right = self._parse_comparison()
            expr = {"type": "LogicalExpression", "operator": op, "left": expr, "right": right}
        return expr

    def _parse_comparison(self) -> dict:
        expr = self._parse_additive()
        while self.peek().type == TokenType.OPERATOR and self.peek().value in {">", ">=", "<", "<=", "==", "!=", "="}:
            op = self.advance().value
            if op == "=": op = "=="
            right = self._parse_additive()
            expr = {"type": "BinaryExpression", "operator": op, "left": expr, "right": right}
        return expr

    def _parse_additive(self) -> dict:
        expr = self._parse_multiplicative()
        while self.peek().type == TokenType.OPERATOR and self.peek().value in {"+", "-"}:
            op = self.advance().value
            right = self._parse_multiplicative()
            expr = {"type": "BinaryExpression", "operator": op, "left": expr, "right": right}
        return expr

    def _parse_multiplicative(self) -> dict:
        expr = self._parse_unary()
        while self.peek().type == TokenType.OPERATOR and self.peek().value in {"*", "/", "%"}:
            op = self.advance().value
            right = self._parse_unary()
            expr = {"type": "BinaryExpression", "operator": op, "left": expr, "right": right}
        return expr

    def _parse_unary(self) -> dict:
        if self.match(TokenType.LOGICAL, {"NOT"}):
            right = self._parse_unary()
            return {"type": "UnaryExpression", "operator": "NOT", "right": right}
        if self.peek().type == TokenType.OPERATOR and self.peek().value in {"+", "-"}:
            op = self.advance().value
            right = self._parse_unary()
            return {"type": "UnaryExpression", "operator": op, "right": right}
        return self._parse_primary()

    def _parse_primary(self) -> dict:
        tok = self.peek()

        if self.match(TokenType.LPAREN):
            expr = self._parse_logical_or()
            if not self.match(TokenType.RPAREN):
                raise ParseError(f"Expected ')' after expression at {self.peek().pos}")
            return expr

        if tok.type == TokenType.NUMBER:
            self.advance()
            val = float(tok.value) if "." in tok.value else int(tok.value)
            return {"type": "Literal", "value": val}

        if tok.type == TokenType.STRING:
            self.advance()
            return {"type": "Literal", "value": tok.value}

        if tok.type == TokenType.IDENTIFIER:
            self.advance()
            name = tok.value
            if name.upper() in FUNCTIONS and self.match(TokenType.LPAREN):
                args = []
                if self.peek().type != TokenType.RPAREN:
                    args.append(self._parse_logical_or())
                    while self.match(TokenType.COMMA):
                        args.append(self._parse_logical_or())
                if not self.match(TokenType.RPAREN):
                    raise ParseError(f"Expected ')' after function arguments for {name}")
                return {"type": "CallExpression", "callee": {"type": "Identifier", "name": name.upper()}, "arguments": args}
            
            # Identifier validation against the allowed schema
            # We do a case-insensitive check against ALLOWED_COLUMNS to be forgiving
            matched_col = next((c for c in ALLOWED_COLUMNS if c.lower() == name.lower()), None)
            if not matched_col:
                raise ParseError(f"Unknown identifier '{name}'. Not in allowed schema.")
                
            return {"type": "Identifier", "name": matched_col}

        raise ParseError(f"Expected expression but got {tok.type} '{tok.value}' at {tok.pos}")

def parse_expression(query_text: str) -> list[dict]:
    """
    Parses a complex mathematical/logical expression into an AST wrapper.
    Returns: [{"type": "expression", "ast": <AST>}]
    """
    if not query_text or not query_text.strip():
        raise ParseError("Empty expression")
    tokens = _tokenize(query_text)
    parser = _ExpressionParser(tokens)
    ast = parser.parse()
    return [{"type": "expression", "ast": ast}]
