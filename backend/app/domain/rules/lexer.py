"""Hand-written lexer for the FlowOps rule expression language.

The language is intentionally tiny: comparisons, boolean logic, arithmetic,
function calls, field access and list literals.  There is no assignment, no
looping construct and no way to reach the host process, which is what makes it
safe to evaluate operator-authored policies on the request path.
"""

from __future__ import annotations

from .tokens import KEYWORDS, OPERATORS, PUNCTUATION, Kind, Token
from ..errors import RuleSyntaxError

_WHITESPACE = (" ", "\t", "\r", "\n")
_DIGITS = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")
# NOTE: these are tuples, not strings, on purpose.  `"" in "0123456789"` is True
# in Python (substring containment), so a string membership test against a
# single-character class silently matches end-of-input and the scanner never
# advances.  Tuples compare elements, so `"" in _DIGITS` is correctly False.


class Lexer:
    def __init__(self, source: str) -> None:
        self.source = source
        self.length = len(source)
        self.pos = 0
        self.line = 1

    # -- helpers ---------------------------------------------------------
    def _peek(self, ahead: int = 0) -> str:
        index = self.pos + ahead
        return self.source[index] if index < self.length else ""

    def _advance(self, count: int = 1) -> None:
        for _ in range(count):
            if self.pos < self.length:
                if self.source[self.pos] == "\n":
                    self.line += 1
                self.pos += 1

    def _fail(self, message: str, offset: int | None = None) -> RuleSyntaxError:
        return RuleSyntaxError(message, source=self.source, offset=offset if offset is not None else self.pos)

    # -- scanners --------------------------------------------------------
    def _skip_trivia(self) -> None:
        while self.pos < self.length:
            char = self.source[self.pos]
            if char in _WHITESPACE:
                self._advance()
                continue
            # `#` and `--` start a comment that runs to end of line.
            if char == "#" or (char == "-" and self._peek(1) == "-"):
                while self.pos < self.length and self.source[self.pos] != "\n":
                    self._advance()
                continue
            break

    def _scan_number(self) -> Token:
        start = self.pos
        while self._peek() in _DIGITS:
            self._advance()
        if self._peek() == "." and self._peek(1) in _DIGITS:
            self._advance()
            while self._peek() in _DIGITS:
                self._advance()
        text = self.source[start:self.pos]
        value: object = float(text) if "." in text else int(text)
        return Token(Kind.NUMBER, value, text, start, self.line)

    def _scan_string(self) -> Token:
        quote = self._peek()
        start = self.pos
        line = self.line
        self._advance()
        chunks: list[str] = []
        escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}
        while True:
            if self.pos >= self.length:
                raise self._fail("unterminated string literal", start)
            char = self._peek()
            if char == "\\":
                nxt = self._peek(1)
                if nxt not in escapes:
                    raise self._fail(f"unknown escape sequence '\\{nxt}'")
                chunks.append(escapes[nxt])
                self._advance(2)
                continue
            if char == quote:
                self._advance()
                break
            if char == "\n":
                raise self._fail("unterminated string literal", start)
            chunks.append(char)
            self._advance()
        text = self.source[start:self.pos]
        return Token(Kind.STRING, "".join(chunks), text, start, line)

    def _scan_ident(self) -> Token:
        start = self.pos
        while self._peek().isalnum() or self._peek() == "_":
            self._advance()
        text = self.source[start:self.pos]
        kind = KEYWORDS.get(text.lower())
        if kind is Kind.TRUE:
            return Token(kind, True, text, start, self.line)
        if kind is Kind.FALSE:
            return Token(kind, False, text, start, self.line)
        if kind is Kind.NULL:
            return Token(kind, None, text, start, self.line)
        if kind is not None:
            return Token(kind, text.lower(), text, start, self.line)
        return Token(Kind.IDENT, text, text, start, self.line)

    def _scan_operator(self) -> Token:
        start = self.pos
        for symbol, kind in OPERATORS:
            if self.source.startswith(symbol, self.pos):
                self._advance(len(symbol))
                return Token(kind, symbol, symbol, start, self.line)
        raise self._fail(f"unexpected character {self._peek()!r}")

    # -- public API ------------------------------------------------------
    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while True:
            self._skip_trivia()
            if self.pos >= self.length:
                tokens.append(Token(Kind.EOF, None, "", self.pos, self.line))
                return tokens
            char = self._peek()
            if char in _DIGITS:
                tokens.append(self._scan_number())
            elif char in ("'", '"'):
                tokens.append(self._scan_string())
            elif char.isalpha() or char == "_":
                tokens.append(self._scan_ident())
            elif char in PUNCTUATION:
                start = self.pos
                self._advance()
                tokens.append(Token(PUNCTUATION[char], char, char, start, self.line))
            else:
                tokens.append(self._scan_operator())


def tokenize(source: str) -> list[Token]:
    """Convenience wrapper used by the parser and by tests."""
    return Lexer(source).tokenize()
