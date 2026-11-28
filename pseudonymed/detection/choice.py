"""Which lists of a lex store PseudonyMed applies: the ticks the store keeps.

The reference is the library's.  A store of lexcor keeps which of its packages are ticked
(``Store.choose``), and every program that reads the store applies those: in lexterm's
store a ticked list protects its terms from NER and the fuzzy search (and lexterm codes with
it), in lexident's store a ticked list is searched for (as lexident does).  The lists of the
steps "2 Identifikatoren" and "3 Fachvokabular", PseudonyMed's command line
(``fachbegriffe|identifikatoren listen|ankreuzen|abwaehlen``) and the libraries' own
(``lexterm packages on|off``, the package window) all set the same ticks.  A package is
ticked when it comes into a store, unless whoever takes it in chooses otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from lexcor import PackageInfo, Store, StoreError

from pseudonymed.core import names
from pseudonymed.detection.store_table import own_lists

VOCABULARY, IDENTIFIERS = "vocabulary", "identifiers"


def applied(store: Store) -> list[str]:
    """The URLs of the lists a run applies: those the store has ticked."""
    chosen = store.chosen()
    return chosen if chosen is not None else sorted(info.url for info in store.packages())


def remember(store: Store, ticked: Iterable[str]) -> None:
    """Tick exactly these lists of the store."""
    store.choose(ticked)


def _find(store: Store, name: str) -> str:
    """The URL of the list a person names: by URL or title as typed, else by the English
    title of one of PseudonyMed's lists named in German or by its label (``Berufe``)."""
    try:
        return store.find(name)
    except StoreError:
        english = names.list_name(name)
        if english == " ".join(name.split()):
            raise
        return store.find(english)


def switch(store: Store, wanted: Iterable[str], on: bool) -> list[str]:
    """Tick or untick lists named by URL or title; returns their URLs.  An unknown name
    is lexcor's ``StoreError``, and then nothing has changed."""
    urls = [_find(store, name) for name in wanted]
    for url in urls:
        store.switch(url, on)
    return urls


@dataclass(frozen=True)
class Listed:
    """One list of a store as the person sees it: what the store says of it, whether it
    is ticked, and whether it is PseudonyMed's own or another program's."""

    info: PackageInfo
    ticked: bool
    own: bool


def listing(store: Store) -> list[Listed]:
    """Every list of a store with its tick, by title."""
    on = set(applied(store))
    own = {info.url for info in own_lists(store)}
    return [Listed(info, info.url in on, info.url in own) for info in store.packages()]
