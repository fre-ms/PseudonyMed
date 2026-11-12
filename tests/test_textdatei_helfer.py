"""Helpers of the text renderers: segment lists and lines, mail headers and leaf parts."""

from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage

import pytest

from pseudonymed.dokument.textdatei import (
    _ist_anhang,
    _kopfzeilen,
    _segmentliste,
    _segmentzeile,
    _sortiere_teile,
    _teilinhalt,
    rendere_mail,
)
from pseudonymed.kern.modell import Patient


def test_segmentliste_bevorzugt_segments_vor_transcription_vor_text() -> None:
    assert _segmentliste({"segments": [{"text": "s"}], "transcription": [{"text": "t"}]}) == [
        {"text": "s"}]
    assert _segmentliste({"segments": None, "transcription": [{"text": "t"}]}) == [{"text": "t"}]
    assert _segmentliste({"segments": "keine Liste", "text": "nur Text"}) is None
    assert _segmentliste([{"text": "a"}, {"text": "b"}]) == [{"text": "a"}, {"text": "b"}]
    assert _segmentliste([]) is None and _segmentliste([{"text": "a"}, 5]) is None
    assert _segmentliste("string") is None and _segmentliste(None) is None


def test_segmentzeile_stempel_und_sprecher() -> None:
    assert _segmentzeile({"text": " Guten Tag ", "start": 0, "end": 2.25}) == (
        "[00:00:00.000 --> 00:00:02.250] Guten Tag")
    assert _segmentzeile({"text": "x", "start": 1.5}) == "x"  # one end unknown: no stamp
    assert _segmentzeile({"text": "x", "start": True, "end": 1.0}) == "x"  # a bool is no time
    assert _segmentzeile({"text": "x", "speaker": "ARZT",
                          "offsets": {"from": 1500, "to": 2750}}) == (
        "[00:00:01.500 --> 00:00:02.750] ARZT: x")
    assert _segmentzeile({"text": "x", "timestamps": {"from": "00:00:01,500",
                                                      "to": "00:00:02,750"}}) == (
        "[00:00:01.500 --> 00:00:02.750] x")
    assert _segmentzeile({"text": "x", "speaker": "  "}) == "x"  # blank label: no prefix
    assert _segmentzeile({"text": "   "}) == "" and _segmentzeile("kein dict") == ""
    assert _segmentzeile({"text": 123}) == "123" and _segmentzeile({"text": 0}) == ""


def _gelesen(nachricht: EmailMessage) -> EmailMessage:
    """The message as it comes back from the wire (folded headers, encoded words)."""
    return email.message_from_bytes(bytes(nachricht), policy=email.policy.default)


def test_kopfzeilen_reihenfolge_faltung_und_fehlende_koepfe() -> None:
    betreff = " ".join(["Rückfrage zu einem Termin, der verschoben werden soll"] * 3)
    m = EmailMessage()
    m["Subject"] = betreff
    m["From"] = "absender@example.org"
    m["Date"] = "Tue, 21 Jul 2026 10:00:00 +0200"
    m["Message-ID"] = "<geheim@example.org>"
    m["Received"] = "from mx.example.org by mail.example.org"
    assert len(bytes(m).splitlines()) > 5  # the long subject is folded on the wire
    assert _kopfzeilen(_gelesen(m)) == ["Von: absender@example.org",
                                        "Datum: Tue, 21 Jul 2026 10:00:00 +0200",
                                        f"Betreff: {betreff}"]
    assert _kopfzeilen(EmailMessage()) == []


def test_ist_anhang_nach_disposition_oder_typ() -> None:
    text = EmailMessage()
    text.set_content("x\n")
    html = EmailMessage()
    html.set_content("<p>x</p>", subtype="html")
    erklaert = EmailMessage()
    erklaert.set_content("x\n", disposition="attachment", filename="notiz.txt")
    kalender = EmailMessage()
    kalender.set_content(b"BEGIN:VCALENDAR", maintype="text", subtype="calendar")
    assert not _ist_anhang(text) and not _ist_anhang(html)
    assert _ist_anhang(erklaert) and _ist_anhang(kalender)


def test_sortiere_teile_in_dokumentreihenfolge(patient: Patient) -> None:
    m = EmailMessage()
    m["From"] = "absender@example.org"
    m.set_content("Text\n", charset="utf-8", cte="base64")
    m.add_alternative("<p>HTML</p>", subtype="html", charset="utf-8")
    m.make_mixed()
    m.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="bild.png",
                     disposition="inline")
    m.add_attachment(b"\x00", maintype="application", subtype="octet-stream")
    m.add_attachment("Notiz\n", subtype="plain", filename=f"Befund_{patient.nachname}.txt",
                     disposition="attachment")
    teile = _sortiere_teile(_gelesen(m))
    assert teile.texte == ["Text"] and teile.htmls == ["HTML"]
    assert teile.anhaenge == ["bild (png)", "application/octet-stream",
                              f"Befund {patient.nachname} (txt)"]
    text = rendere_mail(bytes(m))
    assert "\n\nText\n\n[Anhänge, nicht enthalten: bild (png), application/octet-stream, " in text
    assert "HTML" not in text  # the plain part wins over the HTML alternative


def test_teilinhalt_dekodiert_nachsichtig(monkeypatch: pytest.MonkeyPatch) -> None:
    teil = EmailMessage()
    teil.set_content("Grüße\n", charset="utf-8")
    assert _teilinhalt(teil) == "Grüße\n"
    mehrteilig = email.message_from_bytes(
        b"Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\n\r\nx\r\n--b--\r\n",
        policy=email.policy.default)
    assert mehrteilig.is_multipart() and _teilinhalt(mehrteilig) == ""  # no decodable payload

    def scheitert(self: EmailMessage) -> str:
        raise ValueError("kaputt")

    monkeypatch.setattr(EmailMessage, "get_content", scheitert)
    assert _teilinhalt(teil) == "Grüße\n"  # the raw payload is decoded with the part's charset
