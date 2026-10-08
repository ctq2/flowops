"""Recursive-descent parser with precedence climbing.

Grammar (highest binding power last)::

    expr        := or_expr
    or_expr     := and_expr (("or" | "||") and_expr)*
    and_expr    := not_expr (("and" | "&&") not_expr)*
    not_expr    := ("not" | "!") not_expr | comparison
    comparison  := additive (("==" | "!=" | "<" | "<=" | ">" | ">=" | "in") additive)?
    additive    := multiplicative (("+" | "-") multiplicative)*
    multiplicative := unary (("*" | "/" | "%") unary)*
    unary       := "-" unary | postfix
    postfix     := primary ("." IDENT | "(" args ")")*
    primary     := NUMBER | STRING | "true" | "false" | "null"
                 | IDENT | "[" (expr ("," expr)*)? "]" | "(" expr ")"
"""

from __future__ import annotations

from .ast import Binary, Call, Field, ListLiteral, Literal, Node, Unary
from .lexer import tokenize
from .tokens import Kind, Token
from ..errors import RuleSyntaxError

_COMPARISON = {Kind.EQ, Kind.NEQ, Kind.LT, Kind.LTE, Kind.GT, Kind.GTE, Kind.IN}
_ADDITIVE = {Kind.ADD, Kind.SUB}
_MULTIPLICATIVE = {Kind.MUL, Kind.DIV, Kind.MOD}
_NOT = {Kind.NOT}

#: Function names the runtime knows about; the parser validates eagerly so a typo
#: is a save-time error instead of a production surprise.
KNOWN_FUNCTIONS = frozenset(
    {
        "now",
        "age_hours",
        "hours_since",
        "hours_until",
        "business_hours_between",
        "start_of_day",
        "end_of_day",
        "add_business_hours",
        "lower",
        "upper",
        "contains",
        "startswith",
        "endswith",
        "matches",
        "len",
        "coalesce",
        "int",
        "float",
        "str",
        "min",
        "max",
        "abs",
        "round",
        "date_part",
        "weekday",
        "is_business_day",
        "in_quiet_hours",
    }
)


class Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = tokenize(source)
        self.index = 0

    # -- token plumbing --------------------------------------------------
    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def _at(self, *kinds: Kind) -> bool:
        return self.current.kind in kinds

    def _advance(self) -> Token:
        token = self.current
        if token.kind is not Kind.EOF:
            self.index += 1
        return token

    def _expect(self, kind: Kind, what: str) -> Token:
        if not self._at(kind):
            raise self._fail(f"expected {what}, found {self.current.text or 'end of input'!r}")
        return self._advance()

    def _fail(self, message: str) -> RuleSyntaxError:
        return RuleSyntaxError(message, source=self.source, offset=self.current.offset)

    # -- grammar ---------------------------------------------------------
    def parse(self) -> Node:
        node = self._or_expr()
        if not self._at(Kind.EOF):
            raise self._fail(f"unexpected trailing input {self.current.text!r}")
        return node

    def _or_expr(self) -> Node:
        node = self._and_expr()
        while self._at(Kind.OR):
            pos = self.current.offset
            self._advance()
            node = Binary("or", node, self._and_expr(), pos)
        return node

    def _and_expr(self) -> Node:
        node = self._not_expr()
        while self._at(Kind.AND):
            pos = self.current.offset
            self._advance()
            node = Binary("and", node, self._not_expr(), pos)
        return node

    def _not_expr(self) -> Node:
        if self._at(*_NOT):
            pos = self.current.offset
            self._advance()
            return Unary("not", self._not_expr(), pos)
        return self._comparison()

    def _comparison(self) -> Node:
        node = self._additive()
        if self._at(*_COMPARISON):
            token = self._advance()
            right = self._additive()
            return Binary(token.value if token.kind is not Kind.IN else "in", node, right, token.offset)
        return node

    def _additive(self) -> Node:
        node = self._multiplicative()
        while self._at(*_ADDITIVE):
            token = self._advance()
            node = Binary("+" if token.kind is Kind.ADD else "-", node, self._multiplicative(), token.offset)
        return node

    def _multiplicative(self) -> Node:
        node = self._unary()
        while self._at(*_MULTIPLICATIVE):
            token = self._advance()
            op = {Kind.MUL: "*", Kind.DIV: "/", Kind.MOD: "%"}[token.kind]
            node = Binary(op, node, self._unary(), token.offset)
        return node

    def _unary(self) -> Node:
        if self._at(Kind.SUB):
            pos = self.current.offset
            self._advance()
            return Unary("-", self._unary(), pos)
        if self._at(Kind.ADD):
            self._advance()
            return self._unary()
        return self._postfix()

    def _postfix(self) -> Node:
        node = self._primary()
        while True:
            if self._at(Kind.DOT):
                self._advance()
                name = self._expect(Kind.IDENT, "a field name")
                if isinstance(node, Field):
                    node = Field((*node.path, str(name.value)))
                elif isinstance(node, Call):
                    raise self._fail("field access on a function result is not supported")
                else:
                    raise self._fail("field access requires a field name on the left")
                continue
            if self._at(Kind.LPAREN):
                if not isinstance(node, Field):
                    raise self._fail("only named functions can be called")
                name = node.dotted
                if name not in KNOWN_FUNCTIONS:
                    raise self._fail(f"unknown function {name!r}")
                pos = self.current.offset
                args = self._arguments()
                node = Call(name, args, pos)
                continue
            return node

    def _arguments(self) -> tuple[Node, ...]:
        self._expect(Kind.LPAREN, "'('")
        args: list[Node] = []
        if self._at(Kind.RPAREN):
            self._advance()
            return tuple(args)
        while True:
            args.append(self._or_expr())
            if self._at(Kind.COMMA):
                self._advance()
                if self._at(Kind.RPAREN):  # tolerate trailing comma
                    break
                continue
            break
        self._expect(Kind.RPAREN, "')'")
        return tuple(args)

    def _primary(self) -> Node:
        token = self.current
        if token.kind is Kind.NUMBER:
            self._advance()
            return Literal(token.value)
        if token.kind is Kind.STRING:
            self._advance()
            return Literal(token.value)
        if token.kind in (Kind.TRUE, Kind.FALSE, Kind.NULL):
            self._advance()
            return Literal(token.value)
        if token.kind is Kind.LBRACKET:
            self._advance()
            items: list[Node] = []
            if not self._at(Kind.RBRACKET):
                while True:
                    items.append(self._or_expr())
                    if self._at(Kind.COMMA):
                        self._advance()
                        if self._at(Kind.RBRACKET):
                            break
                        continue
                    break
            self._expect(Kind.RBRACKET, "']'")
            return ListLiteral(tuple(items))
        if token.kind is Kind.LPAREN:
            self._advance()
            node = self._or_expr()
            self._expect(Kind.RPAREN, "')'")
            return node
        if token.kind is Kind.IDENT:
            self._advance()
            return Field((str(token.value),))
        if token.kind is Kind.EOF:
            raise self._fail("unexpected end of expression")
        raise self._fail(f"unexpected token {token.text!r}")


def parse(source: str) -> Node:
    """Parse ``source`` into an AST, raising :class:`RuleSyntaxError` on failure."""
    if not source or not source.strip():
        raise RuleSyntaxError("expression is empty", source=source, offset=0)
    return Parser(source).parse()
