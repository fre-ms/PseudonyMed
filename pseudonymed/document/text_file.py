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

from ocrdoc.pagetext import PageText

from pseudonymed.core.model import Hit
from pseudonymed.core.text import say

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".srt", ".vtt", ".json", ".eml"}
SUBTITLE_EXTENSIONS = {".srt", ".vtt"}

_TIME = r"(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_TIMING = re.compile(rf"^[ \t]*{_TIME}[ \t]*-->[ \t]*{_TIME}.*$", re.MULTILINE)
_CUE_NUMBER = re.compile(r"^[ \t]*\d+[ \t]*$", re.MULTILINE)
_STAMP = re.compile(rf"\[{_TIME}[ \t]*-->[ \t]*{_TIME}\]")  # "[00:00.000 --> 00:04.000]"


def is_text_file(path: str | Path) -> bool:
    """Whether the extension is one of the text-like inputs (case-insensitive)."""
    return Path(path).suffix.lower() in TEXT_EXTENSIONS


def read_text_file(path: str | Path) -> str:
    """Read UTF-8 (with or without BOM), fall back to Windows-1252; normalise line ends."""
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_source(path: str | Path) -> tuple[str, str]:
    """Text of a text-like file and the extension of its pseudonymised output.

    Plain text, Markdown and subtitles come back as they are and are written in
    their own format; Whisper JSON and e-mails are rendered and written as ``.txt``.
    """
    path = Path(path)
    extension = path.suffix.lower()
    if extension == ".json":
        return render_whisper(json.loads(read_text_file(path))), ".txt"
    if extension == ".eml":
        return render_mail(path.read_bytes()), ".txt"
    return read_text_file(path), path.suffix


# -- Whisper JSON ----------------------------------------------------------------------------------


def _hms(seconds: float) -> str:
    """``hh:mm:ss.mmm`` of a number of seconds; negative values are clamped to zero."""
    h, rest = divmod(max(0.0, float(seconds)), 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _time_point(segment: dict[str, Any], seconds: str, cpp: str) -> str:
    """Start or end of a segment: seconds (openai-whisper, WhisperX), ``timestamps``
    strings or ``offsets`` in milliseconds (whisper.cpp)."""
    value = segment.get(seconds)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _hms(value)
    stamp = segment.get("timestamps")
    if isinstance(stamp, dict) and isinstance(stamp.get(cpp), str):
        return stamp[cpp].replace(",", ".")
    offsets = segment.get("offsets")
    if isinstance(offsets, dict) and isinstance(offsets.get(cpp), (int, float)):
        return _hms(offsets[cpp] / 1000)
    return ""


def _segment_list(data: Any) -> list[Any] | None:
    """The segment list of a Whisper-style JSON: ``segments`` (openai-whisper, WhisperX),
    else ``transcription`` (whisper.cpp), or the data itself when it is a non-empty list
    of dicts; None when there is no such list."""
    if isinstance(data, dict):
        for key in ("segments", "transcription"):
            if isinstance(data.get(key), list):
                return data[key]
        return None
    if isinstance(data, list) and data and all(isinstance(s, dict) for s in data):
        return data
    return None


def _timestamp(segment: dict[str, Any]) -> str:
    """``[from --> to] `` of a segment, empty when either end is unknown."""
    begin, end = _time_point(segment, "start", "from"), _time_point(segment, "end", "to")
    return f"[{begin} --> {end}] " if begin and end else ""


def _speaker_prefix(segment: dict[str, Any]) -> str:
    """``SPEAKER: `` of a diarised segment (WhisperX), empty without a speaker label."""
    speaker = segment.get("speaker")
    return f"{speaker}: " if isinstance(speaker, str) and speaker.strip() else ""


def _segment_line(segment: Any) -> str:
    """One rendered line ``[from --> to] speaker: text``; empty for anything that is
    not a segment with text."""
    if not isinstance(segment, dict):
        return ""
    text = str(segment.get("text") or "").strip()
    if not text:
        return ""
    return f"{_timestamp(segment)}{_speaker_prefix(segment)}{text}"


def render_whisper(data: Any) -> str:
    """Whisper-style JSON as one line per segment: ``[from --> to] speaker: text``.

    Understood: openai-whisper and WhisperX (``segments``), whisper.cpp
    (``transcription``), a bare list of segments, or just ``text``.  Word-level
    entries and token ids are not carried over.
    """
    segments = _segment_list(data)
    if segments is None:
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            return data["text"].strip() + "\n"
        raise ValueError(say("JSON ohne Transkript: erwartet 'segments' (Whisper, WhisperX),"
                             " 'transcription' (whisper.cpp) oder 'text'.",
                             "JSON without a transcript: expected 'segments' (Whisper, WhisperX),"
                             " 'transcription' (whisper.cpp) or 'text'."))
    lines = [line for line in map(_segment_line, segments) if line]
    if not lines:
        raise ValueError(say("JSON-Transkript ohne Text.", "JSON transcript without text."))
    return "\n".join(lines) + "\n"


# -- e-mail ----------------------------------------------------------------------------------------


class _HTMLText(HTMLParser):
    """Visible text of an HTML body; block tags become line breaks."""

    _PARAGRAPH_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table", "ul",
                       "ol", "pre"}  # a break before and after
    _LINE_TAGS = {"br", "li", "tr", "hr"}  # a break before only

    def __init__(self) -> None:
        """A parser that resolves character references and starts outside any hidden tag."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Enter a hidden region (script, style, head) or break the line before a block."""
        if tag in ("script", "style", "head"):
            self._suppressed += 1
        elif tag in self._PARAGRAPH_TAGS or tag in self._LINE_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Leave a hidden region or break the line after a paragraph-level block."""
        if tag in ("script", "style", "head"):
            self._suppressed = max(0, self._suppressed - 1)
        elif tag in self._PARAGRAPH_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        """Collect text unless it lies inside a hidden region."""
        if not self._suppressed:
            self.parts.append(data)

    def text(self) -> str:
        """The collected text with runs of blanks and of blank lines collapsed."""
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def html_to_text(html: str) -> str:
    """Visible text of an HTML document; block tags become line breaks."""
    p = _HTMLText()
    p.feed(html)
    p.close()
    return p.text()


def _part_content(part: EmailMessage) -> str:
    """Decoded text of a leaf part; empty when the content is not a string."""
    try:
        content = part.get_content()
    except Exception:  # unknown charset or broken encoding: decode leniently
        raw = part.get_payload(decode=True)
        if not isinstance(raw, bytes):
            raw = b""
        content = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    return content if isinstance(content, str) else ""


def _attachment_name(name: str) -> str:
    """Attachment name for the listing: separators become spaces so that a name
    inside it ("Befund_Flantz.pdf") is a word the rules can see."""
    stem, point, extension = name.rpartition(".")
    if not point:
        stem, extension = name, ""
    stem = re.sub(r"[_\-.]+", " ", stem).strip()
    return f"{stem} ({extension})" if extension else stem


# the headers worth reading, in the order of the rendering; routing headers are left out
_HEADERS = (("Von", "From"), ("An", "To"), ("Cc", "Cc"), ("Datum", "Date"),
            ("Betreff", "Subject"))


def _header_lines(message: EmailMessage) -> list[str]:
    """``Beschriftung: Wert`` for every present header of :data:`_HEADERS`, with the
    folding whitespace of long values collapsed to single spaces."""
    values = ((label, message.get(header)) for label, header in _HEADERS)
    return [f"{label}: {' '.join(str(value).split())}" for label, value in values
            if value]


class _MailParts(NamedTuple):
    """The leaf parts of a message, sorted by what the rendering does with them."""

    texts: list[str]  # text/plain bodies, stripped
    htmls: list[str]  # text/html bodies rendered as text
    attachments: list[str]  # attachment names, or the content type without a file name


def _is_attachment(part: EmailMessage) -> bool:
    """Whether a leaf part is only listed by name: declared as an attachment, or neither
    plain text nor HTML (images, PDFs, calendar entries, …)."""
    return (part.get_content_disposition() == "attachment"
            or part.get_content_type() not in ("text/plain", "text/html"))


def _sort_parts(message: EmailMessage) -> _MailParts:
    """Walk the message and decode its leaf parts into plain texts, HTML texts and
    attachment names, in document order."""
    texts: list[str] = []
    htmls: list[str] = []
    attachments: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        if _is_attachment(part):
            name = part.get_filename()
            attachments.append(_attachment_name(name) if name else part.get_content_type())
        elif part.get_content_type() == "text/plain":
            texts.append(_part_content(part).strip())
        else:
            htmls.append(html_to_text(_part_content(part)))
    return _MailParts(texts, htmls, attachments)


def render_mail(raw: bytes) -> str:
    """An e-mail as readable text: From, To, Cc, Date, Subject, the plain-text body
    (or the HTML body as text when there is no plain part) and the names of the
    attachments.  Routing headers (Received, Message-ID, …) are left out, attachments
    are not included; pseudonymise them as files of their own."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    lines = _header_lines(message)
    parts = _sort_parts(message)
    body = [t for t in (parts.texts or parts.htmls) if t]
    if body:
        lines += [""] + body
    if parts.attachments:
        lines += ["", "[Anhänge, nicht enthalten: " + ", ".join(parts.attachments) + "]"]
    return "\n".join(lines).strip() + "\n"


def analysis_text(text: str, extension: str) -> str:
    """Copy of ``text`` with the same length in which subtitle timing lines, cue numbers,
    the WebVTT header and Whisper-style stamps are blanked out."""
    chars = list(text)

    def mask_out(a: int, b: int) -> None:
        """Blank the characters of ``[a, b)``, keeping line breaks so lines stay aligned."""
        for i in range(a, b):
            if chars[i] != "\n":
                chars[i] = " "

    muster = [_STAMP]
    if extension.lower() in SUBTITLE_EXTENSIONS:
        muster += [_TIMING, _CUE_NUMBER]
        if text.startswith("WEBVTT"):
            end = text.find("\n")
            mask_out(0, len(text) if end < 0 else end)
    for m in muster:
        for t in m.finditer(text):
            mask_out(t.start(), t.end())
    return "".join(chars)


def text_page(text: str, number: int = 0) -> PageText:
    """A :class:`PageText` without boxes: the rules, the fuzzy search, NER and the
    vocabulary only need the string and the line of every character."""
    page = PageText(number=number, width=0.0, height=0.0, text=text, source="text")
    lines: list[int] = []
    line = 0
    for c in text:
        lines.append(line)
        if c == "\n":
            line += 1
    n = len(text)
    page.boxes = [None] * n
    page.lines = lines
    page.sizes = [0.0] * n
    page.baselines = [0.0] * n
    return page


def replace(text: str, hits: list[Hit]) -> str:
    """Replace every hit by its pseudonym, from the end so that offsets stay valid."""
    for f in sorted((f for f in hits if f.end > f.start), key=lambda f: -f.start):
        text = text[: f.start] + f.pseudonym + text[f.end :]
    return text
