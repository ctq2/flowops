"""Abstract syntax tree for rule expressions.

Nodes are frozen dataclasses: evaluation never mutates the tree, which makes a
compiled rule trivially shareable between requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True, slots=True)
class Literal:
    value: object


@dataclass(frozen=True, slots=True)
class Field:
    """A dotted lookup, e.g. ``ticket.priority`` or ``tags``."""

    path: tuple[str, ...]

    @property
    def dotted(self) -> str:
        return ".".join(self.path)


@dataclass(frozen=True, slots=True)
class ListLiteral:
    items: tuple["Node", ...]


@dataclass(frozen=True, slots=True)
class Unary:
    op: str
    operand: "Node"
    pos: int = 0


@dataclass(frozen=True, slots=True)
class Binary:
    op: str
    left: "Node"
    right: "Node"
    pos: int = 0


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    args: tuple["Node", ...]
    pos: int = 0


Node = Union[Literal, Field, ListLiteral, Unary, Binary, Call]


def describe(node: Node) -> str:
    """Render a node back to source — used in error messages and the audit log."""
    if isinstance(node, Literal):
        return repr(node.value) if not isinstance(node.value, str) else f"'{node.value}'"
    if isinstance(node, Field):
        return node.dotted
    if isinstance(node, ListLiteral):
        return "[" + ", ".join(describe(item) for item in node.items) + "]"
    if isinstance(node, Unary):
        # Render a negative literal as `-1`, not `- 1`, so round-tripped source
        # stays readable in the audit log and in error messages.
        return f"{node.op}{describe(node.operand)}" if node.op == "-" else f"{node.op} {describe(node.operand)}"
    if isinstance(node, Binary):
        return f"({describe(node.left)} {node.op} {describe(node.right)})"
    if isinstance(node, Call):
        return f"{node.name}(" + ", ".join(describe(arg) for arg in node.args) + ")"
    raise TypeError(f"unknown node {node!r}")  # pragma: no cover - defensive
