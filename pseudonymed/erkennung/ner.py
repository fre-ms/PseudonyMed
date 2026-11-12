"""Named-entity recognition (optional extra ``ner``): unknown people and places.

The deterministic rules only know the identifiers in the patient record.  A
relative, a colleague or a town that nobody entered slips through them.  A
spaCy model trained on German news tags person and place names in free
text; those hits get their own reproducible pseudonyms ("Person K7Q4MX",
"[Ort 2F7QV4]").  NER guesses, so it is off by default, never runs in the
re-check, and medical eponyms (Alzheimer, Parkinson, Babinski ...) are
excluded; everything it finds is shown in the review step for a decision.

Install: ``uv sync --extra ner`` and a model, for example
``uv pip install https://github.com/explosion/spacy-models/releases/download/de_core_news_md-3.8.0/de_core_news_md-3.8.0-py3-none-any.whl``.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.kern.modell import Art, Fundstelle
from pseudonymed.pseudonyme.vergabe import Pseudonymvergabe

if TYPE_CHECKING:
    from spacy.tokens import Span, Token

    from pseudonymed.erkennung.fachbegriffe import Fachbegriffe

MODELLE = ("de_core_news_lg", "de_core_news_md", "de_core_news_sm")
_TITEL = {"dr", "prof", "med", "dent", "phil", "pd", "dipl", "univ", "herr", "herrn", "frau",
          "fr", "hr", "mr", "mrs", "ms", "kollege", "kollegin", "ehefrau", "ehemann", "gattin",
          "gatte", "tochter", "sohn", "mutter", "vater", "schwester", "bruder", "partner",
          "partnerin", "betreuer", "betreuerin", "patient", "patientin", "pat"}
# Diseases, signs, scores and syndromes named after people: not patients.
EPONYME = {
    "alzheimer", "parkinson", "huntington", "crohn", "hodgkin", "basedow", "hashimoto", "wilson",
    "bechterew", "dupuytren", "addison", "cushing", "down", "asperger", "tourette", "korsakow",
    "wernicke", "broca", "babinski", "romberg", "lasègue", "lasegue", "glasgow", "barthel",
    "hamilton", "beck", "epworth", "apgar", "rankin", "trendelenburg", "kernig", "brudzinski",
    "hoffmann", "tinel", "phalen", "schober", "lhermitte", "charcot", "guillain", "barré", "barre",
    "bell", "horner", "menière", "ménière", "meniere", "raynaud", "sjögren", "sjogren", "behçet",
    "behcet", "kaposi", "burkitt", "wegener", "takayasu", "paget", "perthes", "scheuermann",
    "osgood", "schlatter", "marfan", "ehlers", "danlos", "turner", "klinefelter", "prader",
    "willi", "angelman", "rett", "lewy", "pick", "binswanger", "creutzfeldt", "jakob",
    "gerstmann", "landau", "kleffner", "west", "lennox", "gastaut", "dravet", "rolando",
    "jackson", "todd", "wallenberg", "weber", "millard", "gubler", "foville", "brown", "séquard",
    "sequard", "adie", "argyll", "robertson", "marcus", "gunn", "uhthoff", "hallervorden",
    "spatz", "fahr", "steele", "richardson", "olszewski", "shy", "drager", "meige", "sydenham",
    "friedreich", "duchenne", "becker", "erb", "lambert", "eaton", "miller", "fisher",
    "morbus", "syndrom", "zeichen", "test", "score", "skala",
}


def verfuegbares_modell() -> str | None:
    """Name of the first installed German model, largest first, or None without one."""
    for m in MODELLE:
        if importlib.util.find_spec(m) is not None:
            return m
    return None


def verfuegbar() -> bool:
    """Are spaCy and a German model installed, so that :class:`NER` can be built?"""
    return importlib.util.find_spec("spacy") is not None and verfuegbares_modell() is not None


class NER:
    """A loaded spaCy pipeline reduced to tokenisation and entity recognition."""

    def __init__(self, modell: str | None = None) -> None:
        """Load ``modell`` or the best installed one; RuntimeError without spaCy or a model."""
        try:
            import spacy
        except ImportError as e:
            raise RuntimeError("NER braucht das Extra 'ner' (spaCy).") from e
        name = modell or verfuegbares_modell()
        if not name:
            raise RuntimeError("Kein deutsches spaCy-Modell installiert (de_core_news_md).")
        self.name = name
        # the morphologizer stays: its part of speech tells a sentence-initial verb
        # ("Überwiesen hat Sie …") from a place name
        self.nlp = spacy.load(name, exclude=["parser", "lemmatizer", "attribute_ruler", "tagger"])
        self.nlp.max_length = 2_000_000

    def entitaeten(self, text: str) -> list[tuple[int, int, str, str]]:
        """Entities of the model minus the noise German clinical prose produces.

        Dropped: heads tagged as verbs or function words; ordinary words that are
        only capitalised because they start a sentence ("Überwiesen hat Sie …", known
        lower-case but not capitalised); single words the tagger calls common nouns
        ("Konzentration: gut"); and single words the model has never seen (compound
        nouns such as "Nierenlager", "Strichgang") unless the context says person or
        place: a salutation or title before a person, a preposition before a place.
        A lone unknown surname without such context is left to the rules, which
        know the patient's names anyway.
        """
        return [(e.start_char, e.end_char, e.label_, e.text)
                for e in self.nlp(text).ents if not self._rauschen(e, text)]

    def _rauschen(self, e: Span, text: str) -> bool:
        """Is the entity one of the artefacts :meth:`entitaeten` lists?"""
        kopf = e.root
        if kopf.pos_ in _KEIN_NAME or self._nur_am_satzanfang_gross(kopf):
            return True
        if len(e) != 1 or e.label_ not in _NAMENSLABELS:
            return False
        return kopf.pos_ == "NOUN" or (self._unbekannt(kopf) and not _kontext_davor(e, text))

    def _nur_am_satzanfang_gross(self, token: Token) -> bool:
        """A capitalised word the vectors know only in lower case: an ordinary word that
        happens to open the sentence, not a name."""
        vokabular = self.nlp.vocab
        return (token.text[:1].isupper() and not vokabular.has_vector(token.text)
                and vokabular.has_vector(token.text.lower()))

    def _unbekannt(self, token: Token) -> bool:
        """Has the model no vector for the word in either spelling (a compound noun, say)?"""
        vokabular = self.nlp.vocab
        return not vokabular.has_vector(token.text) and not vokabular.has_vector(token.text.lower())


_KEIN_NAME = {"VERB", "AUX", "ADJ", "ADV", "ADP", "DET", "PRON", "SCONJ", "CCONJ", "PART"}
_NAMENSLABELS = ("PER", "LOC", "GPE")
# what must stand right before a lone unknown word for it to count as a person or a place
_PERSONENKONTEXT = re.compile(
    r"(?:\b(?:Herrn?|Frau|Hr\.|Fr\.|Dr\.|Prof\.|PD|med\.|Dipl\.|Patient(?:in)?|Pat\.|Kolleg(?:e|in)"
    r"|Schwester|Pfleger(?:in)?|Ehemann|Ehefrau|Mutter|Vater|Tochter|Sohn|Bruder|Schwester"
    r"|Betreuer(?:in)?|Familie|Fam\.)\s*$)|(?:[A-ZÄÖÜ][a-zäöüß]+\s+$)", re.IGNORECASE)
_ORTSKONTEXT = re.compile(r"\b(?:in|aus|nach|bei|von|wohnhaft in|Ort|Wohnort|Stadt|Gemeinde|PLZ"
                          r"|\d{5})\s*$", re.IGNORECASE)


def _kontext_davor(e: Span, text: str) -> bool:
    """Does a salutation, title or kinship word (person) or a preposition or postcode
    (place) stand right before the entity?  Only that makes a lone unknown word count."""
    davor = text[max(0, e.start_char - 30):e.start_char]
    muster = _PERSONENKONTEXT if e.label_ == "PER" else _ORTSKONTEXT
    return muster.search(davor) is not None


_RANDZEICHEN = " .,;:-–()"


def _personenkern(text: str) -> str:
    """The name without the salutations, titles and kinship words in front of it."""
    tokens = [t for t in re.split(r"\s+", text.strip(_RANDZEICHEN)) if t]
    while tokens and tokens[0].rstrip(".").lower() in _TITEL:
        tokens.pop(0)
    return " ".join(tokens)


def _nur_eponyme(tokens: list[str]) -> bool:
    """Do all words (parts joined by hyphen or slash counted on their own) name an eponym
    or a generic term such as "Morbus" or "Syndrom"?"""
    woerter = [w.lower() for t in tokens for w in re.split(r"[-/]", t.strip(".,")) if w]
    return bool(woerter) and all(w in EPONYME for w in woerter)


def _grossgeschrieben(tokens: list[str]) -> bool:
    """Does every token that starts with a letter start with a capital, and is there one?"""
    buchstaben = [t for t in tokens if t[:1].isalpha()]
    return bool(buchstaben) and all(t[:1].isupper() for t in buchstaben)


def _plausibler_name(kern: str) -> bool:
    """Capitalised tokens, and not just an eponym: "Erika Hoffmann" is a person,
    "Hoffmann", "Morbus Parkinson" and "Parkinson-Syndrom" are not."""
    tokens = kern.split()
    if not tokens or len(kern) < 3:
        return False
    return not _nur_eponyme(tokens) and _grossgeschrieben(tokens)


def _plausibler_ort(kern: str) -> bool:
    """At least three characters, capitalised and not an eponym."""
    return len(kern) >= 3 and kern.lower() not in EPONYME and kern[:1].isupper()


_TEILNAMEN = (Art.NACHNAME, Art.VORNAME, Art.GEBURTSNAME)


@dataclass(frozen=True)
class _Kandidat:
    """A person or place the model proposed, reduced to its core and already coded."""

    kern: str
    art: Art
    regel: str
    pseudonym: str

    def fundstelle(self, seite: Seitentext, a: int, b: int, quelle: str) -> Fundstelle:
        """The hit over ``seite.text[a:b]`` with its boxes and font size."""
        return Fundstelle(
            seite=seite.nummer, start=a, ende=b, text=self.kern, art=self.art, regel=self.regel,
            pseudonym=self.pseudonym, rects=[tuple(r) for r in seite.rects(a, b)],
            schriftgroesse=seite.schriftgroesse(a, b), quelle=quelle,
        )


def _kandidat_aus(label: str, text: str, vergabe: Pseudonymvergabe) -> _Kandidat | None:
    """The hit an entity would become, or None for anything but a plausible person or place.

    The pseudonym is coded here, before the later steps decide whether the hit survives:
    ``vergabe`` records every name it codes, in order, and that record must not change.
    """
    if label == "PER":
        kern = _personenkern(text)
        if _plausibler_name(kern):
            return _Kandidat(kern, Art.NAME, "ner.person", vergabe.person_unbekannt(kern))
    elif label in ("LOC", "GPE"):
        kern = text.strip(_RANDZEICHEN)
        if _plausibler_ort(kern):
            return _Kandidat(kern, Art.ORT, "ner.ort", vergabe.ort_unbekannt(kern))
    return None


@dataclass(frozen=True)
class _Vokabularschutz:
    """The medical terms of one page, as far as NER has to leave them alone.

    Without a vocabulary nothing is protected.  Every core that was spared goes to
    ``verschont`` when the caller passed a list, so the report can count them.
    """

    text: str
    fachbegriffe: Fachbegriffe | None
    spannen: list[tuple[int, int]]
    verschont: list[str] | None

    @classmethod
    def fuer(cls, text: str, fachbegriffe: Fachbegriffe | None,
             verschont: list[str] | None) -> _Vokabularschutz:
        """Mark the terms of ``text`` once; the spans serve every entity of the page."""
        spannen = fachbegriffe.spannen(text) if fachbegriffe is not None else []
        return cls(text, fachbegriffe, spannen, verschont)

    def schuetzt(self, a: int, b: int, kern: str) -> bool:
        """Does ``text[a:b]`` consist of medical terms?  Records ``kern`` when it does."""
        if self.fachbegriffe is None or not self.fachbegriffe.geschuetzt(
                self.text, a, b, self.spannen, ganz=True):
            return False
        if self.verschont is not None:
            self.verschont.append(kern)
        return True


def _ueberdeckter_teilname(f: Fundstelle, a: int, b: int, vorhandene: list[Fundstelle]) -> bool:
    """Is ``f`` a lone first, last or birth name of the rules that lies inside ``[a, b)``
    and is shorter than that span?  Only such hits give way to the full name."""
    return (f in vorhandene and f.art in _TEILNAMEN and a <= f.start and f.ende <= b
            and (f.ende - f.start) < (b - a))


def _verdraengte_teiltreffer(a: int, b: int, vorhandene: list[Fundstelle],
                             treffer: list[Fundstelle]) -> list[Fundstelle] | None:
    """The partial names a full name over ``[a, b)`` supersedes, or None when the span
    also touches a hit that has to stay (then the entity is dropped).  A hit with an
    empty span is never touched."""
    ueberlappend = [f for f in vorhandene + treffer
                    if f.ende > f.start and a < f.ende and b > f.start]
    ersetzbar = [f for f in ueberlappend if _ueberdeckter_teilname(f, a, b, vorhandene)]
    return ersetzbar if len(ersetzbar) == len(ueberlappend) else None


def finde_ner(
    seite: Seitentext, ner: NER, vergabe: Pseudonymvergabe, vorhandene: list[Fundstelle],
    quelle: str = "text", fachbegriffe: Fachbegriffe | None = None,
    verschont: list[str] | None = None,
) -> list[Fundstelle]:
    """People and places the rules did not know.

    Entities made up of medical terms of ``fachbegriffe`` are left alone (and
    listed in ``verschont`` when given): "Morbus Wilson" or "Babinski" is not
    a patient, "Erika Hoffmann" still is although Hoffmann names a sign.
    A hit that only overlaps existing hits is dropped,
    with one exception: when the entity completely contains hits on a lone
    surname or first name (the rules saw "Flantz" in "Hildegund Flantz"), those
    partial hits are removed from ``vorhandene`` and the full name takes their
    place, because it is a different person than the one the rules assumed.
    """
    treffer: list[Fundstelle] = []
    schutz = _Vokabularschutz.fuer(seite.text, fachbegriffe, verschont)
    for start, ende, label, text in ner.entitaeten(seite.text):
        kandidat = _kandidat_aus(label, text, vergabe)
        if kandidat is None:
            continue
        a = seite.text.find(kandidat.kern, start, ende + 1)
        if a < 0:
            continue
        b = a + len(kandidat.kern)
        if schutz.schuetzt(a, b, kandidat.kern):
            continue
        verdraengte = _verdraengte_teiltreffer(a, b, vorhandene, treffer)
        if verdraengte is None:
            continue
        for f in verdraengte:
            vorhandene.remove(f)
        treffer.append(kandidat.fundstelle(seite, a, b, quelle))
    return treffer
