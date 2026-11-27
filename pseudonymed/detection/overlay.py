"""Hiding many terms of a store at once.

A deletion in the vocabulary or among the identifiers is a line in the overlay of lexcor's
store (:mod:`pseudonymed.detection.vocabulary`, :mod:`pseudonymed.detection.identifiers`).
The store writes every single change to the disk on its own, so a whole category would take
minutes: from :data:`MANY` lines on they are taken in at once (:meth:`lexcor.Store.apply`).
The lines are the same either way.
"""

from __future__ import annotations

from lexcor import Change, Store

MANY = 100  # from this many lines on, they are written to the overlay in one step


def hide(store: Store, lines: list[Change], known: bool = True) -> None:
    """Put hiding lines into the overlay of a store: one by one, or from :data:`MANY` on all
    at once.  With ``known`` false the concepts need not be in the
    store yet (the lines come into force when their package does), which only the overlay
    taken in at once allows."""
    if known and len(lines) < MANY:
        for line in lines:
            store.hide(line.url, line.code, line.value, line.language)
        return
    take(store, lines)


def take(store: Store, lines: list[Change]) -> None:
    """Take lines of any kind into the overlay of a store at once; the concepts they speak
    of need not be in the store."""
    if lines:
        store.apply(lines)
