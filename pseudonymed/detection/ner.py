"""Named-entity recognition (optional extra ``ner``): unknown people and places.

The deterministic rules only know the identifiers in the patient record.  A
relative, a colleague or a town that nobody entered slips through them.  A
spaCy model trained on German news tags person and place names in free
text; those hits get their own reproducible pseudonyms ("Person K7Q4MX",
"[Ort 2F7QV4]").  NER guesses, so it is off by default, never runs in the
re-check, and medical eponyms (Alzheimer, Parkinson, Babinski ...) are
excluded; everything it finds is shown in the review step for a decision.

Install: ``uv sync --extra ner`` and a model, for example
``uv pip install
https://github.com/explosion/spacy-models/releases/download/de_core_news_md-3.8.0/de_core_news_md-3.8.0-py3-none-any.whl``.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from optdoc.pagetext import PageText

from pseudonymed.core.model import Hit, Kind
from pseudonymed.core.text import say
from pseudonymed.pseudonyms.assignment import PseudonymAssignment

if TYPE_CHECKING:
    from spacy.tokens import Span, Token

    from pseudonymed.detection.medical_terms import MedicalTerms

MODELS = ("de_core_news_lg", "de_core_news_md", "de_core_news_sm")
_TITLES = {"dr", "prof", "med", "dent", "phil", "pd", "dipl", "univ", "herr", "herrn", "frau",
           "fr", "hr", "mr", "mrs", "ms", "kollege", "kollegin", "ehefrau", "ehemann", "gattin",
           "gatte", "tochter", "sohn", "mutter", "vater", "schwester", "bruder", "partner",
           "partnerin", "betreuer", "betreuerin", "patient", "patientin", "pat"}
# Diseases, signs, scores and syndromes named after people: not patients.
EPONYMS = {
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


def available_model() -> str | None:
    """Name of the first installed German model, largest first, or None without one."""
    for m in MODELS:
        if importlib.util.find_spec(m) is not None:
            return m
    return None


def available() -> bool:
    """Are spaCy and a German model installed, so that :class:`NER` can be built?"""
    return importlib.util.find_spec("spacy") is not None and available_model() is not None


class NER:
    """A loaded spaCy pipeline reduced to tokenisation and entity recognition."""

    def __init__(self, model: str | None = None) -> None:
        """Load ``model`` or the best installed one; RuntimeError without spaCy or a model."""
        try:
            import spacy
        except ImportError as e:
            raise RuntimeError(say("NER braucht das Extra 'ner' (spaCy).",
                                   "NER needs the extra 'ner' (spaCy).")) from e
        name = model or available_model()
        if not name:
            raise RuntimeError(say("Kein deutsches spaCy-Modell installiert (de_core_news_md).",
                                   "No German spaCy model installed (de_core_news_md)."))
        self.name = name
        # the morphologizer stays: its part of speech tells a sentence-initial verb
        # ("Überwiesen hat Sie …") from a place name
        self.nlp = spacy.load(name, exclude=["parser", "lemmatizer", "attribute_ruler", "tagger"])
        self.nlp.max_length = 2_000_000

    def entities(self, text: str) -> list[tuple[int, int, str, str]]:
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
                for e in self.nlp(text).ents if not self._is_noise(e, text)]

    def _is_noise(self, e: Span, text: str) -> bool:
        """Is the entity one of the artefacts :meth:`entities` lists?"""
        header = e.root
        if header.pos_ in _NON_NAME_POS or self._capitalised_by_sentence_start(header):
            return True
        if len(e) != 1 or e.label_ not in _NAME_LABELS:
            return False
        return header.pos_ == "NOUN" or (self._is_unknown(header) and not _context_before(e, text))

    def _capitalised_by_sentence_start(self, token: Token) -> bool:
        """A capitalised word the vectors know only in lower case: an ordinary word that
        happens to open the sentence, not a name."""
        vocabulary = self.nlp.vocab
        return (token.text[:1].isupper() and not vocabulary.has_vector(token.text)
                and vocabulary.has_vector(token.text.lower()))

    def _is_unknown(self, token: Token) -> bool:
        """Has the model no vector for the word in either spelling (a compound noun, say)?"""
        vocabulary = self.nlp.vocab
        return not (vocabulary.has_vector(token.text)
                    or vocabulary.has_vector(token.text.lower()))


_NON_NAME_POS = {"VERB", "AUX", "ADJ", "ADV", "ADP", "DET", "PRON", "SCONJ", "CCONJ", "PART"}
_NAME_LABELS = ("PER", "LOC", "GPE")
# what must stand right before a lone unknown word for it to count as a person or a place
_PERSON_CONTEXT = re.compile(
    r"(?:\b(?:Herrn?|Frau|Hr\.|Fr\.|Dr\.|Prof\.|PD|med\.|Dipl\.|Patient(?:in)?|Pat\.|Kolleg(?:e|in)"
    r"|Schwester|Pfleger(?:in)?|Ehemann|Ehefrau|Mutter|Vater|Tochter|Sohn|Bruder|Schwester"
    r"|Betreuer(?:in)?|Familie|Fam\.)\s*$)|(?:[A-ZÄÖÜ][a-zäöüß]+\s+$)", re.IGNORECASE)
_PLACE_CONTEXT = re.compile(r"\b(?:in|aus|nach|bei|von|wohnhaft in|Ort|Wohnort|Stadt|Gemeinde|PLZ"
                            r"|\d{5})\s*$", re.IGNORECASE)


def _context_before(e: Span, text: str) -> bool:
    """Does a salutation, title or kinship word (person) or a preposition or postcode
    (place) stand right before the entity?  Only that makes a lone unknown word count."""
    preceding = text[max(0, e.start_char - 30):e.start_char]
    muster = _PERSON_CONTEXT if e.label_ == "PER" else _PLACE_CONTEXT
    return muster.search(preceding) is not None


_EDGE_CHARS = " .,;:-–()"


def _person_core(text: str) -> str:
    """The name without the salutations, titles and kinship words in front of it."""
    tokens = [t for t in re.split(r"\s+", text.strip(_EDGE_CHARS)) if t]
    while tokens and tokens[0].rstrip(".").lower() in _TITLES:
        tokens.pop(0)
    return " ".join(tokens)


def _only_eponyms(tokens: list[str]) -> bool:
    """Do all words (parts joined by hyphen or slash counted on their own) name an eponym
    or a generic term such as "Morbus" or "Syndrom"?"""
    words = [w.lower() for t in tokens for w in re.split(r"[-/]", t.strip(".,")) if w]
    return bool(words) and all(w in EPONYMS for w in words)


def _capitalised(tokens: list[str]) -> bool:
    """Does every token that starts with a letter start with a capital, and is there one?"""
    letters = [t for t in tokens if t[:1].isalpha()]
    return bool(letters) and all(t[:1].isupper() for t in letters)


def _plausible_name(core: str) -> bool:
    """Capitalised tokens, and not just an eponym: "Erika Hoffmann" is a person,
    "Hoffmann", "Morbus Parkinson" and "Parkinson-Syndrom" are not."""
    tokens = core.split()
    if not tokens or len(core) < 3:
        return False
    return not _only_eponyms(tokens) and _capitalised(tokens)


def _plausible_place(core: str) -> bool:
    """At least three characters, capitalised and not an eponym."""
    return len(core) >= 3 and core.lower() not in EPONYMS and core[:1].isupper()


_PARTIAL_NAMES = (Kind.SURNAME, Kind.FIRST_NAME, Kind.BIRTH_NAME)


@dataclass(frozen=True)
class _Candidate:
    """A person or place the model proposed, reduced to its core and already coded."""

    core: str
    kind: Kind
    rule: str
    pseudonym: str

    def hit(self, page: PageText, a: int, b: int, source: str) -> Hit:
        """The hit over ``seite.text[a:b]`` with its boxes and font size."""
        return Hit(
            page=page.number, start=a, end=b, text=self.core, kind=self.kind, rule=self.rule,
            pseudonym=self.pseudonym, rects=[tuple(r) for r in page.rects(a, b)],
            font_size=page.font_size(a, b), source=source,
        )


def _candidate_from(label: str, text: str, assignment: PseudonymAssignment) -> _Candidate | None:
    """The hit an entity would become, or None for anything but a plausible person or place.

    The pseudonym is coded here, before the later steps decide whether the hit survives:
    ``assignment`` records every name it codes, in order, and that record must not change.
    """
    if label == "PER":
        core = _person_core(text)
        if _plausible_name(core):
            return _Candidate(core, Kind.NAME, "ner.person", assignment.unknown_person(core))
    elif label in ("LOC", "GPE"):
        core = text.strip(_EDGE_CHARS)
        if _plausible_place(core):
            return _Candidate(core, Kind.PLACE, "ner.ort", assignment.unknown_place(core))
    return None


@dataclass(frozen=True)
class _VocabularyGuard:
    """The medical terms of one page, as far as NER has to leave them alone.

    Without a vocabulary nothing is protected.  Every core that was spared goes to
    ``spared`` when the caller passed a list, so the report can count them.
    """

    text: str
    medical_terms: MedicalTerms | None
    spans: list[tuple[int, int]]
    spared: list[str] | None

    @classmethod
    def of(cls, text: str, medical_terms: MedicalTerms | None,
           spared: list[str] | None) -> _VocabularyGuard:
        """Mark the terms of ``text`` once; the spans serve every entity of the page."""
        spans = medical_terms.spans(text) if medical_terms is not None else []
        return cls(text, medical_terms, spans, spared)

    def protects(self, a: int, b: int, core: str) -> bool:
        """Does ``text[a:b]`` consist of medical terms?  Records ``core`` when it does."""
        if self.medical_terms is None or not self.medical_terms.is_protected(
                self.text, a, b, self.spans, whole=True):
            return False
        if self.spared is not None:
            self.spared.append(core)
        return True


def _covered_partial_name(f: Hit, a: int, b: int, existing: list[Hit]) -> bool:
    """Is ``f`` a lone first, last or birth name of the rules that lies inside ``[a, b)``
    and is shorter than that span?  Only such hits give way to the full name."""
    return (f in existing and f.kind in _PARTIAL_NAMES and a <= f.start and f.end <= b
            and (f.end - f.start) < (b - a))


def _displaced_partial_matches(a: int, b: int, existing: list[Hit],
                               matches: list[Hit]) -> list[Hit] | None:
    """The partial names a full name over ``[a, b)`` supersedes, or None when the span
    also touches a hit that has to stay (then the entity is dropped).  A hit with an
    empty span is never touched."""
    overlapping = [f for f in existing + matches
                   if f.end > f.start and a < f.end and b > f.start]
    replaceable = [f for f in overlapping if _covered_partial_name(f, a, b, existing)]
    return replaceable if len(replaceable) == len(overlapping) else None


def find_ner(
    page: PageText, ner: NER, assignment: PseudonymAssignment, existing: list[Hit],
    source: str = "text", medical_terms: MedicalTerms | None = None,
    spared: list[str] | None = None,
) -> list[Hit]:
    """People and places the rules did not know.

    Entities made up of medical terms of ``medical_terms`` are left alone (and
    listed in ``spared`` when given): "Morbus Wilson" or "Babinski" is not
    a patient, "Erika Hoffmann" still is although Hoffmann names a sign.
    A hit that only overlaps existing hits is dropped,
    with one exception: when the entity completely contains hits on a lone
    surname or first name (the rules saw "Flantz" in "Hildegund Flantz"), those
    partial hits are removed from ``existing`` and the full name takes their
    place, because it is a different person than the one the rules assumed.
    """
    matches: list[Hit] = []
    protection = _VocabularyGuard.of(page.text, medical_terms, spared)
    for start, end, label, text in ner.entities(page.text):
        candidate = _candidate_from(label, text, assignment)
        if candidate is None:
            continue
        a = page.text.find(candidate.core, start, end + 1)
        if a < 0:
            continue
        b = a + len(candidate.core)
        if protection.protects(a, b, candidate.core):
            continue
        displaced = _displaced_partial_matches(a, b, existing, matches)
        if displaced is None:
            continue
        for f in displaced:
            existing.remove(f)
        matches.append(candidate.hit(page, a, b, source))
    return matches
