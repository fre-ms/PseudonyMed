"""What PseudonyMed's tables over the stores of the lex libraries share.

The protected vocabulary (:mod:`pseudonymed.detection.vocabulary`, lexterm's store) and the
identifiers (:mod:`pseudonymed.detection.identifiers`, lexident's store) are each a table of
PseudonyMed's over a store of lexcor's.  Both keep their store the same way and owe it the
same on first use: what a pseudonym database still holds of their kind, then the bundled
lists.

Both stores are shared with other programs, which may install whole terminologies there
(ICD-10-GM, SNOMED CT).  PseudonyMed's are only the store's own lists (:func:`own_lists`):
the packages that brought no URL and got one from the store.  A package with a URL of its
own is another program's: it is no category and no list of identifiers, it protects nothing
and is not looked for, and PseudonyMed never hides, rewrites or removes anything of it.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Self

from lexcor import PackageInfo, Store, StoreError

LOCAL = "urn:uuid:"  # how the URL begins that a store gives a list without one


def _is_own(store: Store, info: PackageInfo) -> bool:
    """Whether the URL of a package is the one this store gives a list of its name: the
    name is the title for every list PseudonyMed writes, and is read from the package only
    where the two differ."""
    if not info.url.startswith(LOCAL):
        return False
    try:
        if info.title and info.url == store.local_url(info.title):
            return True
        name = store.package(info.url).name
        return bool(name) and info.url == store.local_url(name)
    except StoreError:
        return False


def own_lists(store: Store) -> list[PackageInfo]:
    """The store's own lists: the packages whose URL the store gave them from their name
    (``Store.local_url``), by title.  Only these are PseudonyMed's; a package that brought
    a URL of its own is a terminology of another program and is left alone."""
    return [info for info in store.packages() if _is_own(store, info)]


class StoreTable(ABC):
    """A table over one store: its file, the opened store, and what first use owes it."""

    path: Path
    store: Store

    def _open(self, store: Store, seed: bool) -> None:
        """Keep the opened store.  One that may be written is restricted to its owner, and
        with ``seed`` it gets the bundled lists.  A store that cannot be prepared is closed
        again."""
        self.store = store
        if store.readonly:
            return
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        if not seed:
            return
        try:
            self.seed()
        except BaseException:
            self.close()
            raise

    @abstractmethod
    def seed(self) -> dict[str, int]:
        """Install the bundled lists the store is to hold; returns the number of terms of
        each list installed."""

    def close(self) -> None:
        """Close the store."""
        self.store.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def version(self) -> int:
        """Counter that every change raises; the matcher rebuilds when it moved."""
        return self.store.version()
