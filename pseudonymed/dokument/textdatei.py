"""Text-like inputs: transcripts of consultations, notes, subtitles, Whisper JSON,
e-mails.

A text file is one "page" without geometry.  Detection runs on an analysis
copy of exactly the same length in which the timestamps and cue numbers of
subtitle formats (SRT, WebVTT, Whisper's console style) are blanked, so that
every offset maps one to one onto the original and the pseudonyms can be
written back into it without touching anything else.  Whisper JSON and
e-mails are first rendered as readable text (segments with timestamps,
headers plus body); their pseudonymised form is that text, not the JSON or the
MIME message, because token ids and word-level copies in the JSON and the
routing headers of a mail cannot be checked the way text can.
"""

from __future__ import annotations

import email
import email.policy
import json
import re
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, NamedTuple

from pseudonymed.dokument.textebene import Seitentext
from pseudonymed.kern.modell import Fundstelle

TEXTENDUNGEN = {".txt", ".md", ".markdown", ".srt", ".vtt", ".json", ".eml"}
UNTERTITEL = {".srt", ".vtt"}

_ZEIT = r"(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_TIMING = re.compile(rf"^[ \t]*{_ZEIT}[ \t]*-->[ \t]*{_ZEIT}.*$", re.MULTILINE)
_CUE_NUMMER = re.compile(r"^[ \t]*\d+[ \t]*$", re.MULTILINE)
_STEMPEL = re.compile(rf"\[{_ZEIT}[ \t]*-->[ \t]*{_ZEIT}\]")  # "[00:00.000 --> 00:04.000]"


def ist_textdatei(pfad: str | Path) -> bool:
    """Whether the extension is one of the text-like inputs (case-insensitive)."""
    return Path(pfad).suffix.lower() in TEXTENDUNGEN


def lese_textdatei(pfad: str | Path) -> str:
    """Read UTF-8 (with or without BOM), fall back to Windows-1252; normalise line ends."""
    roh = Path(pfad).read_bytes()
    for kodierung in ("utf-8-sig", "cp1252"):
        try:
            text = roh.decode(kodierung)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = roh.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def lese_quelle(pfad: str | Path) -> tuple[str, str]:
    """Text of a text-like file and the extension of its pseudonymised output.

    Plain text, Markdown and subtitles come back as they are and are written in
    their own format; Whisper JSON and e-mails are rendered and written as ``.txt``.
    """
    pfad = Path(pfad)
    endung = pfad.suffix.lower()
    if endung == ".json":
        return rendere_whisper(json.loads(lese_textdatei(pfad))), ".txt"
    if endung == ".eml":
        return rendere_mail(pfad.read_bytes()), ".txt"
    return lese_textdatei(pfad), pfad.suffix


# -- Whisper JSON ----------------------------------------------------------------------------------


def _hms(sekunden: float) -> str:
    """``hh:mm:ss.mmm`` of a number of seconds; negative values are clamped to zero."""
    h, rest = divmod(max(0.0, float(sekunden)), 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _zeitpunkt(segment: dict[str, Any], sekunden: str, cpp: str) -> str:
    """Start or end of a segment: seconds (openai-whisper, WhisperX), ``timestamps``
    strings or ``offsets`` in milliseconds (whisper.cpp)."""
    wert = segment.get(sekunden)
    if isinstance(wert, (int, float)) and not isinstance(wert, bool):
        return _hms(wert)
    stempel = segment.get("timestamps")
    if isinstance(stempel, dict) and isinstance(stempel.get(cpp), str):
        return stempel[cpp].replace(",", ".")
    versatz = segment.get("offsets")
    if isinstance(versatz, dict) and isinstance(versatz.get(cpp), (int, float)):
        return _hms(versatz[cpp] / 1000)
    return ""


def _segmentliste(daten: Any) -> list[Any] | None:
    """The segment list of a Whisper-style JSON: ``segments`` (openai-whisper, WhisperX),
    else ``transcription`` (whisper.cpp), or the data itself when it is a non-empty list
    of dicts; None when there is no such list."""
    if isinstance(daten, dict):
        for schluessel in ("segments", "transcription"):
            if isinstance(daten.get(schluessel), list):
                return daten[schluessel]
        return None
    if isinstance(daten, list) and daten and all(isinstance(s, dict) for s in daten):
        return daten
    return None


def _zeitstempel(segment: dict[str, Any]) -> str:
    """``[from --> to] `` of a segment, empty when either end is unknown."""
    von, bis = _zeitpunkt(segment, "start", "from"), _zeitpunkt(segment, "end", "to")
    return f"[{von} --> {bis}] " if von and bis else ""


def _sprechervorsatz(segment: dict[str, Any]) -> str:
    """``SPEAKER: `` of a diarised segment (WhisperX), empty without a speaker label."""
    sprecher = segment.get("speaker")
    return f"{sprecher}: " if isinstance(sprecher, str) and sprecher.strip() else ""


def _segmentzeile(segment: Any) -> str:
    """One rendered line ``[from --> to] speaker: text``; empty for anything that is
    not a segment with text."""
    if not isinstance(segment, dict):
        return ""
    text = str(segment.get("text") or "").strip()
    if not text:
        return ""
    return f"{_zeitstempel(segment)}{_sprechervorsatz(segment)}{text}"


def rendere_whisper(daten: Any) -> str:
    """Whisper-style JSON as one line per segment: ``[from --> to] speaker: text``.

    Understood: openai-whisper and WhisperX (``segments``), whisper.cpp
    (``transcription``), a bare list of segments, or just ``text``.  Word-level
    entries and token ids are not carried over.
    """
    segmente = _segmentliste(daten)
    if segmente is None:
        if isinstance(daten, dict) and isinstance(daten.get("text"), str):
            return daten["text"].strip() + "\n"
        raise ValueError("JSON ohne Transkript: erwartet 'segments' (Whisper, WhisperX),"
                         " 'transcription' (whisper.cpp) oder 'text'.")
    zeilen = [zeile for zeile in map(_segmentzeile, segmente) if zeile]
    if not zeilen:
        raise ValueError("JSON-Transkript ohne Text.")
    return "\n".join(zeilen) + "\n"


# -- e-mail ----------------------------------------------------------------------------------------


class _HTMLText(HTMLParser):
    """Visible text of an HTML body; block tags become line breaks."""

    _ABSATZ = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table", "ul",
               "ol", "pre"}  # a break before and after
    _ZEILE = {"br", "li", "tr", "hr"}  # a break before only

    def __init__(self) -> None:
        """A parser that resolves character references and starts outside any hidden tag."""
        super().__init__(convert_charrefs=True)
        self.teile: list[str] = []
        self._still = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enter a hidden region (script, style, head) or break the line before a block."""
        if tag in ("script", "style", "head"):
            self._still += 1
        elif tag in self._ABSATZ or tag in self._ZEILE:
            self.teile.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Leave a hidden region or break the line after a paragraph-level block."""
        if tag in ("script", "style", "head"):
            self._still = max(0, self._still - 1)
        elif tag in self._ABSATZ:
            self.teile.append("\n")

    def handle_data(self, data: str) -> None:
        """Collect text unless it lies inside a hidden region."""
        if not self._still:
            self.teile.append(data)

    def text(self) -> str:
        """The collected text with runs of blanks and of blank lines collapsed."""
        roh = "".join(self.teile)
        roh = re.sub(r"[ \t]+", " ", roh)
        roh = re.sub(r" *\n *", "\n", roh)
        return re.sub(r"\n{3,}", "\n\n", roh).strip()


def html_zu_text(html: str) -> str:
    """Visible text of an HTML document; block tags become line breaks."""
    p = _HTMLText()
    p.feed(html)
    p.close()
    return p.text()


def _teilinhalt(teil: EmailMessage) -> str:
    """Decoded text of a leaf part; empty when the content is not a string."""
    try:
        inhalt = teil.get_content()
    except Exception:  # unknown charset or broken encoding: decode leniently
        roh = teil.get_payload(decode=True)
        if not isinstance(roh, bytes):
            roh = b""
        inhalt = roh.decode(teil.get_content_charset() or "utf-8", errors="replace")
    return inhalt if isinstance(inhalt, str) else ""


def _anhangsname(name: str) -> str:
    """Attachment name for the listing: separators become spaces so that a name
    inside it ("Befund_Flantz.pdf") is a word the rules can see."""
    stamm, punkt, endung = name.rpartition(".")
    if not punkt:
        stamm, endung = name, ""
    stamm = re.sub(r"[_\-.]+", " ", stamm).strip()
    return f"{stamm} ({endung})" if endung else stamm


# the headers worth reading, in the order of the rendering; routing headers are left out
_KOPFZEILEN = (("Von", "From"), ("An", "To"), ("Cc", "Cc"), ("Datum", "Date"),
               ("Betreff", "Subject"))


def _kopfzeilen(nachricht: EmailMessage) -> list[str]:
    """``Beschriftung: Wert`` for every present header of :data:`_KOPFZEILEN`, with the
    folding whitespace of long values collapsed to single spaces."""
    werte = ((beschriftung, nachricht.get(kopf)) for beschriftung, kopf in _KOPFZEILEN)
    return [f"{beschriftung}: {' '.join(str(wert).split())}" for beschriftung, wert in werte
            if wert]


class _Mailteile(NamedTuple):
    """The leaf parts of a message, sorted by what the rendering does with them."""

    texte: list[str]  # text/plain bodies, stripped
    htmls: list[str]  # text/html bodies rendered as text
    anhaenge: list[str]  # attachment names, or the content type without a file name


def _ist_anhang(teil: EmailMessage) -> bool:
    """Whether a leaf part is only listed by name: declared as an attachment, or neither
    plain text nor HTML (images, PDFs, calendar entries, …)."""
    return (teil.get_content_disposition() == "attachment"
            or teil.get_content_type() not in ("text/plain", "text/html"))


def _sortiere_teile(nachricht: EmailMessage) -> _Mailteile:
    """Walk the message and decode its leaf parts into plain texts, HTML texts and
    attachment names, in document order."""
    texte: list[str] = []
    htmls: list[str] = []
    anhaenge: list[str] = []
    for teil in nachricht.walk():
        if teil.is_multipart():
            continue
        if _ist_anhang(teil):
            name = teil.get_filename()
            anhaenge.append(_anhangsname(name) if name else teil.get_content_type())
        elif teil.get_content_type() == "text/plain":
            texte.append(_teilinhalt(teil).strip())
        else:
            htmls.append(html_zu_text(_teilinhalt(teil)))
    return _Mailteile(texte, htmls, anhaenge)


def rendere_mail(roh: bytes) -> str:
    """An e-mail as readable text: From, To, Cc, Date, Subject, the plain-text body
    (or the HTML body as text when there is no plain part) and the names of the
    attachments.  Routing headers (Received, Message-ID, …) are left out, attachments
    are not included; pseudonymise them as files of their own."""
    nachricht = email.message_from_bytes(roh, policy=email.policy.default)
    zeilen = _kopfzeilen(nachricht)
    teile = _sortiere_teile(nachricht)
    koerper = [t for t in (teile.texte or teile.htmls) if t]
    if koerper:
        zeilen += [""] + koerper
    if teile.anhaenge:
        zeilen += ["", "[Anhänge, nicht enthalten: " + ", ".join(teile.anhaenge) + "]"]
    return "\n".join(zeilen).strip() + "\n"


def analysetext(text: str, endung: str) -> str:
    """Copy of ``text`` with the same length in which subtitle timing lines, cue numbers,
    the WebVTT header and Whisper-style stamps are blanked out."""
    zeichen = list(text)

    def blende(a: int, b: int) -> None:
        """Blank the characters of ``[a, b)``, keeping line breaks so lines stay aligned."""
        for i in range(a, b):
            if zeichen[i] != "\n":
                zeichen[i] = " "

    muster = [_STEMPEL]
    if endung.lower() in UNTERTITEL:
        muster += [_TIMING, _CUE_NUMMER]
        if text.startswith("WEBVTT"):
            ende = text.find("\n")
            blende(0, len(text) if ende < 0 else ende)
    for m in muster:
        for t in m.finditer(text):
            blende(t.start(), t.end())
    return "".join(zeichen)


def textseite(text: str, nummer: int = 0) -> Seitentext:
    """A :class:`Seitentext` without boxes: the rules, the fuzzy search, NER and the
    vocabulary only need the string and the line of every character."""
    seite = Seitentext(nummer=nummer, breite=0.0, hoehe=0.0, text=text, quelle="text")
    zeilen: list[int] = []
    zeile = 0
    for c in text:
        zeilen.append(zeile)
        if c == "\n":
            zeile += 1
    n = len(text)
    seite.boxen = [None] * n
    seite.zeilen = zeilen
    seite.groessen = [0.0] * n
    seite.basislinien = [0.0] * n
    return seite


def ersetze(text: str, fundstellen: list[Fundstelle]) -> str:
    """Replace every hit by its pseudonym, from the end so that offsets stay valid."""
    for f in sorted((f for f in fundstellen if f.ende > f.start), key=lambda f: -f.start):
        text = text[: f.start] + f.pseudonym + text[f.ende :]
    return text
