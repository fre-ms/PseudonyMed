"""CardDAV client for Nextcloud using only the standard library.

PROPFIND lists the address books of the user, REPORT ``addressbook-query``
searches by name on the server; a second, local pass applies all search
tokens (name parts and dates of birth).  Only https is accepted, except for
loopback addresses used in tests.
"""

from __future__ import annotations

import base64
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit
from xml.sax.saxutils import escape

from pseudonymed import WERKZEUG
from pseudonymed.erkennung.varianten import ohne_akzente
from pseudonymed.kern.modell import Patient, normalisiere, parse_datum
from pseudonymed.quellen.vcf import VCard, parse_vcards, patient_aus_vcard

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_DATUM_TOKEN = re.compile(r"^(?:\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{2}-\d{2}|\d{4})$")

_PROPFIND = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>"
)


class CardDAVFehler(Exception):
    """Connection, authentication or protocol problem, with a readable message."""


@dataclass
class Adressbuch:
    """One address book of the CardDAV account: its href and display name."""
    href: str
    name: str

    @property
    def uri(self) -> str:
        """The last path segment of the href, the name the server addresses the book by."""
        return urlsplit(self.href).path.rstrip("/").rsplit("/", 1)[-1]


@dataclass
class Kontakt:
    """One contact of an address book: href, etag and the parsed vCard."""
    href: str
    etag: str
    vcard: VCard
    patient: Patient


def _query(begriff: str) -> str:
    filter_xml = ""
    if begriff:
        b = escape(begriff)
        filter_xml = (
            '<c:filter test="anyof">'
            '<c:prop-filter name="FN"><c:text-match collation="i;unicode-casemap"'
            f' match-type="contains">{b}</c:text-match></c:prop-filter>'
            '<c:prop-filter name="N"><c:text-match collation="i;unicode-casemap"'
            f' match-type="contains">{b}</c:text-match></c:prop-filter>'
            "</c:filter>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:addressbook-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><d:getetag/><c:address-data/></d:prop>"
        f"{filter_xml}</c:addressbook-query>"
    )


def _token_passt(token: str, patient: Patient) -> bool:
    if _DATUM_TOKEN.match(token):
        geb = patient.geburtsdatum
        if geb is None:
            return False
        if len(token) == 4 and token.isdigit():
            return geb.year == int(token)
        datum = parse_datum(token)
        return datum == geb
    heuhaufen = " ".join((patient.nachname, patient.vorname, patient.geburtsname))
    varianten = {normalisiere(heuhaufen), normalisiere(heuhaufen.translate(_TRANS))}
    nadel = {normalisiere(token), normalisiere(token.translate(_TRANS)),
             normalisiere(ohne_akzente(token))}
    return any(n in h for n in nadel for h in varianten)


def passt(patient: Patient, begriff: str) -> bool:
    """All whitespace-separated tokens must match name parts or the birth date."""
    tokens = [t for t in begriff.replace(",", " ").split() if t]
    return all(_token_passt(t, patient) for t in tokens)


def _server_begriff(begriff: str) -> str:
    """The first name-like token goes to the server; everything else is filtered locally."""
    for t in begriff.replace(",", " ").split():
        if not _DATUM_TOKEN.match(t):
            return t
    return ""


class CardDAVClient:
    """A small CardDAV client for Nextcloud: lists address books, fetches and searches contacts."""
    def __init__(self, url: str, benutzer: str, passwort: str, zeitlimit: float = 30.0) -> None:
        self.basis = url.strip().rstrip("/")
        teile = urlsplit(self.basis)
        if teile.scheme != "https" and teile.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise CardDAVFehler("Nur https-Adressen werden akzeptiert (Ausnahme: localhost).")
        if not teile.netloc:
            raise CardDAVFehler(f"Ungültige Server-Adresse: {url!r}")
        self.benutzer = benutzer
        self.zeitlimit = zeitlimit
        token = base64.b64encode(f"{benutzer}:{passwort}".encode()).decode("ascii")
        self._auth = f"Basic {token}"

    @property
    def adressbuch_home(self) -> str:
        """The URL under which Nextcloud lists the user's address books."""
        return f"{self.basis}/remote.php/dav/addressbooks/users/{quote(self.benutzer)}/"

    def _anfrage(
        self, methode: str, url: str, body: str | None = None, tiefe: str | None = None
    ) -> bytes:
        daten = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=daten, method=methode)
        req.add_header("Authorization", self._auth)
        req.add_header("User-Agent", WERKZEUG)
        req.add_header("Accept", "application/xml, text/xml")
        if daten is not None:
            req.add_header("Content-Type", "application/xml; charset=utf-8")
        if tiefe is not None:
            req.add_header("Depth", tiefe)
        try:
            with urllib.request.urlopen(req, timeout=self.zeitlimit) as antwort:
                return antwort.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise CardDAVFehler(
                    "Anmeldung abgelehnt (401): Benutzername oder App-Passwort prüfen."
                ) from e
            if e.code == 404:
                raise CardDAVFehler(f"Nicht gefunden (404): {url}") from e
            raise CardDAVFehler(f"{methode} {url}: HTTP {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise CardDAVFehler(f"Server nicht erreichbar: {e.reason}") from e

    @staticmethod
    def _antworten(daten: bytes) -> list[ET.Element]:
        try:
            wurzel = ET.fromstring(daten)
        except ET.ParseError as e:
            raise CardDAVFehler(f"Unlesbare Antwort des Servers: {e}") from e
        return wurzel.findall("d:response", NS)

    def adressbuecher(self) -> list[Adressbuch]:
        """Every address book of the account, as the server lists them."""
        daten = self._anfrage("PROPFIND", self.adressbuch_home, _PROPFIND, tiefe="1")
        buecher: list[Adressbuch] = []
        for antwort in self._antworten(daten):
            href = (antwort.findtext("d:href", default="", namespaces=NS) or "").strip()
            if antwort.find(".//d:resourcetype/c:addressbook", NS) is None:
                continue
            name = (antwort.findtext(".//d:prop/d:displayname", default="", namespaces=NS) or "")
            voll = urljoin(self.basis + "/", href)
            anzeigename = name.strip() or voll.rstrip("/").rsplit("/", 1)[-1]
            buecher.append(Adressbuch(href=voll, name=anzeigename))
        return buecher

    def adressbuch(self, name_oder_uri: str = "") -> Adressbuch:
        """The address book named by display name or uri; the only one when the name is empty."""
        buecher = self.adressbuecher()
        if not buecher:
            raise CardDAVFehler("Der Server meldet kein Adressbuch für diesen Benutzer.")
        if not name_oder_uri:
            if len(buecher) == 1:
                return buecher[0]
            namen = ", ".join(f"{b.name} ({b.uri})" for b in buecher)
            raise CardDAVFehler(f"Mehrere Adressbücher, bitte eines wählen: {namen}")
        gesucht = name_oder_uri.casefold()
        for b in buecher:
            if gesucht in (b.uri.casefold(), b.name.casefold()):
                return b
        namen = ", ".join(f"{b.name} ({b.uri})" for b in buecher)
        raise CardDAVFehler(f"Adressbuch {name_oder_uri!r} nicht gefunden. Vorhanden: {namen}")

    def _kontakte(self, daten: bytes) -> list[Kontakt]:
        kontakte: list[Kontakt] = []
        for antwort in self._antworten(daten):
            href = (antwort.findtext("d:href", default="", namespaces=NS) or "").strip()
            etag = antwort.findtext(".//d:prop/d:getetag", default="", namespaces=NS) or ""
            etag = etag.strip()
            text = antwort.findtext(".//d:prop/c:address-data", default="", namespaces=NS) or ""
            for karte in parse_vcards(text):
                kontakte.append(Kontakt(urljoin(self.basis + "/", href), etag, karte,
                                        patient_aus_vcard(karte)))
        return kontakte

    def alle(self, buch: Adressbuch) -> list[Kontakt]:
        """Every contact of ``buch``, sorted by name."""
        return self._sortiert(self._kontakte(self._anfrage("REPORT", buch.href, _query(""), "1")))

    def suche(self, buch: Adressbuch, begriff: str) -> list[Kontakt]:
        """The contacts of ``buch`` matching ``begriff`` on the server and in the local check."""
        daten = self._anfrage("REPORT", buch.href, _query(_server_begriff(begriff)), "1")
        return self._sortiert([k for k in self._kontakte(daten) if passt(k.patient, begriff)])

    @staticmethod
    def _sortiert(kontakte: list[Kontakt]) -> list[Kontakt]:
        return sorted(kontakte, key=lambda k: (normalisiere(k.patient.nachname),
                                               normalisiere(k.patient.vorname)))
