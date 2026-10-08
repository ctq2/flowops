"""Token model shared by the lexer and the parser."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    NUMBER = "NUMBER"
    STRING = "STRING"
    IDENT = "IDENT"
    TRUE = "TRUE"
    FALSE = "FALSE"
    NULL = "NULL"

    # operators
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    EQ = "EQ"
    NEQ = "NEQ"
    LT = "LT"
    LTE = "LTE"
    GT = "GT"
    GTE = "GTE"
    IN = "IN"
    ADD = "ADD"
    SUB = "SUB"
    MUL = "MUL"
    DIV = "DIV"
    MOD = "MOD"

    # punctuation
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    LBRACKET = "LBRACKET"
    RBRACKET = "RBRACKET"
    COMMA = "COMMA"
    DOT = "DOT"

    EOF = "EOF"


KEYWORDS: dict[str, Kind] = {
    "and": Kind.AND,
    "or": Kind.OR,
    "not": Kind.NOT,
    "in": Kind.IN,
    "true": Kind.TRUE,
    "false": Kind.FALSE,
    "null": Kind.NULL,
}

# Longest-first so ">=" never lexes as ">" followed by "=".
OPERATORS: tuple[tuple[str, Kind], ...] = (
    ("==", Kind.EQ),
    ("!=", Kind.NEQ),
    ("<=", Kind.LTE),
    (">=", Kind.GTE),
    ("&&", Kind.AND),
    ("||", Kind.OR),
    ("<", Kind.LT),
    (">", Kind.GT),
    ("=", Kind.EQ),
    ("+", Kind.ADD),
    ("-", Kind.SUB),
    ("*", Kind.MUL),
    ("/", Kind.DIV),
    ("%", Kind.MOD),
    ("!", Kind.NOT),
)

PUNCTUATION: dict[str, Kind] = {
    "(": Kind.LPAREN,
    ")": Kind.RPAREN,
    "[": Kind.LBRACKET,
    "]": Kind.RBRACKET,
    ",": Kind.COMMA,
    ".": Kind.DOT,
}


@dataclass(frozen=True, slots=True)
class Token:
    kind: Kind
    value: object
    text: str
    offset: int
    line: int

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.kind.value}({self.text!r})@{self.line}"
