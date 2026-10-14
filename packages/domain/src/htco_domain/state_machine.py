"""Explicit state machines: every lifecycle has a transition table; anything else is an error."""

from collections.abc import Mapping, Set
from enum import Enum


class IllegalTransition(Exception):  # noqa: N818 - a domain error named for what happened
    """A lifecycle was asked to move along an edge its transition table does not have."""

    def __init__(self, kind: str, current: Enum, target: Enum) -> None:
        super().__init__(f"{kind}: {current.value} -> {target.value} is not allowed")


def check_transition[S: Enum](kind: str, table: Mapping[S, Set[S]], current: S, target: S) -> None:
    if target not in table.get(current, frozenset()):
        raise IllegalTransition(kind, current, target)
