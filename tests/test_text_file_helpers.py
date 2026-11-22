"""Helpers of the text renderers: segment lists and lines, mail headers and leaf parts."""

from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage

import pytest

from pseudonymed.core.model import Patient
from pseudonymed.document.text_file import (
    _header_lines,
    _is_attachment,
    _part_content,
    _segment_line,
    _segment_list,
    _sort_parts,
    render_mail,
)


def test_segment_list_prefers_segments_over_transcription_over_text() -> None:
    assert _segment_list({"segments": [{"text": "s"}], "transcription": [{"text": "t"}]}) == [
        {"text": "s"}]
    assert _segment_list({"segments": None, "transcription": [{"text": "t"}]}) == [{"text": "t"}]
    assert _segment_list({"segments": "keine Liste", "text": "nur Text"}) is None
    assert _segment_list([{"text": "a"}, {"text": "b"}]) == [{"text": "a"}, {"text": "b"}]
    assert _segment_list([]) is None and _segment_list([{"text": "a"}, 5]) is None
    assert _segment_list("string") is None and _segment_list(None) is None


def test_segment_line_stamp_and_speaker() -> None:
    assert _segment_line({"text": " Guten Tag ", "start": 0, "end": 2.25}) == (
        "[00:00:00.000 --> 00:00:02.250] Guten Tag")
    assert _segment_line({"text": "x", "start": 1.5}) == "x"  # one end unknown: no stamp
    assert _segment_line({"text": "x", "start": True, "end": 1.0}) == "x"  # a bool is no time
    assert _segment_line({"text": "x", "speaker": "ARZT",
                          "offsets": {"from": 1500, "to": 2750}}) == (
        "[00:00:01.500 --> 00:00:02.750] ARZT: x")
    assert _segment_line({"text": "x", "timestamps": {"from": "00:00:01,500",
                                                      "to": "00:00:02,750"}}) == (
        "[00:00:01.500 --> 00:00:02.750] x")
    assert _segment_line({"text": "x", "speaker": "  "}) == "x"  # blank label: no prefix
    assert _segment_line({"text": "   "}) == "" and _segment_line("kein dict") == ""
    assert _segment_line({"text": 123}) == "123" and _segment_line({"text": 0}) == ""


def _read(message: EmailMessage) -> EmailMessage:
    """The message as it comes back from the wire (folded headers, encoded words)."""
    return email.message_from_bytes(bytes(message), policy=email.policy.default)


def test_header_lines_order_folding_and_missing_headers() -> None:
    subject = " ".join(["Rückfrage zu einem Termin, der verschoben werden soll"] * 3)
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = "absender@example.org"
    m["Date"] = "Tue, 21 Jul 2026 10:00:00 +0200"
    m["Message-ID"] = "<geheim@example.org>"
    m["Received"] = "from mx.example.org by mail.example.org"
    assert len(bytes(m).splitlines()) > 5  # the long subject is folded on the wire
    assert _header_lines(_read(m)) == ["Von: absender@example.org",
                                       "Datum: Tue, 21 Jul 2026 10:00:00 +0200",
                                       f"Betreff: {subject}"]
    assert _header_lines(EmailMessage()) == []


def test_is_attachment_by_disposition_or_type() -> None:
    text = EmailMessage()
    text.set_content("x\n")
    html = EmailMessage()
    html.set_content("<p>x</p>", subtype="html")
    declared = EmailMessage()
    declared.set_content("x\n", disposition="attachment", filename="notiz.txt")
    calendar = EmailMessage()
    calendar.set_content(b"BEGIN:VCALENDAR", maintype="text", subtype="calendar")
    assert not _is_attachment(text) and not _is_attachment(html)
    assert _is_attachment(declared) and _is_attachment(calendar)


def test_sort_parts_in_document_order(patient: Patient) -> None:
    m = EmailMessage()
    m["From"] = "absender@example.org"
    m.set_content("Text\n", charset="utf-8", cte="base64")
    m.add_alternative("<p>HTML</p>", subtype="html", charset="utf-8")
    m.make_mixed()
    m.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="bild.png",
                     disposition="inline")
    m.add_attachment(b"\x00", maintype="application", subtype="octet-stream")
    m.add_attachment("Notiz\n", subtype="plain", filename=f"Befund_{patient.surname}.txt",
                     disposition="attachment")
    parts = _sort_parts(_read(m))
    assert parts.texts == ["Text"] and parts.htmls == ["HTML"]
    assert parts.attachments == ["bild (png)", "application/octet-stream",
                                 f"Befund {patient.surname} (txt)"]
    text = render_mail(bytes(m))
    assert "\n\nText\n\n[Anhänge, nicht enthalten: bild (png), application/octet-stream, " in text
    assert "HTML" not in text  # the plain part wins over the HTML alternative


def test_part_content_decodes_leniently(monkeypatch: pytest.MonkeyPatch) -> None:
    part = EmailMessage()
    part.set_content("Grüße\n", charset="utf-8")
    assert _part_content(part) == "Grüße\n"
    multipart = email.message_from_bytes(
        b"Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\n\r\nx\r\n--b--\r\n",
        policy=email.policy.default)
    assert multipart.is_multipart() and _part_content(multipart) == ""  # no decodable payload

    def fails(self: EmailMessage) -> str:
        raise ValueError("kaputt")

    monkeypatch.setattr(EmailMessage, "get_content", fails)
    assert _part_content(part) == "Grüße\n"  # the raw payload is decoded with the part's charset
