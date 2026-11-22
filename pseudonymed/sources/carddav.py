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

from pseudonymed import TOOL
from pseudonymed.core.model import Patient, normalise, parse_date
from pseudonymed.detection.variants import without_accents
from pseudonymed.sources.vcf import VCard, parse_vcards, patient_from_vcard

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}
_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_DATE_TOKEN = re.compile(r"^(?:\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{2}-\d{2}|\d{4})$")

_PROPFIND = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>"
)


class CardDAVError(Exception):
    """Connection, authentication or protocol problem, with a readable message."""


@dataclass
class AddressBook:
    """One address book of the CardDAV account: its href and display name."""
    href: str
    name: str

    @property
    def uri(self) -> str:
        """The last path segment of the href, the name the server addresses the book by."""
        return urlsplit(self.href).path.rstrip("/").rsplit("/", 1)[-1]


@dataclass
class Contact:
    """One contact of an address book: href, etag and the parsed vCard."""
    href: str
    etag: str
    vcard: VCard
    patient: Patient


def _query(term: str) -> str:
    filter_xml = ""
    if term:
        b = escape(term)
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


def _token_matches(token: str, patient: Patient) -> bool:
    if _DATE_TOKEN.match(token):
        born = patient.birth_date
        if born is None:
            return False
        if len(token) == 4 and token.isdigit():
            return born.year == int(token)
        date = parse_date(token)
        return date == born
    haystack = " ".join((patient.surname, patient.first_name, patient.birth_name))
    variants = {normalise(haystack), normalise(haystack.translate(_TRANS))}
    needles = {normalise(token), normalise(token.translate(_TRANS)),
               normalise(without_accents(token))}
    return any(n in h for n in needles for h in variants)


def fits(patient: Patient, term: str) -> bool:
    """All whitespace-separated tokens must match name parts or the birth date."""
    tokens = [t for t in term.replace(",", " ").split() if t]
    return all(_token_matches(t, patient) for t in tokens)


def _server_term(term: str) -> str:
    """The first name-like token goes to the server; everything else is filtered locally."""
    for t in term.replace(",", " ").split():
        if not _DATE_TOKEN.match(t):
            return t
    return ""


class CardDAVClient:
    """A small CardDAV client for Nextcloud: lists address books, fetches and searches contacts."""
    def __init__(self, url: str, user: str, password: str, timeout: float = 30.0) -> None:
        self.base = url.strip().rstrip("/")
        parts = urlsplit(self.base)
        if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise CardDAVError("Nur https-Adressen werden akzeptiert (Ausnahme: localhost).")
        if not parts.netloc:
            raise CardDAVError(f"Ungültige Server-Adresse: {url!r}")
        self.user = user
        self.timeout = timeout
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        self._auth = f"Basic {token}"

    @property
    def address_book_home(self) -> str:
        """The URL under which Nextcloud lists the user's address books."""
        return f"{self.base}/remote.php/dav/addressbooks/users/{quote(self.user)}/"

    def _request(
        self, method: str, url: str, body: str | None = None, depth: str | None = None
    ) -> bytes:
        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("User-Agent", TOOL)
        req.add_header("Accept", "application/xml, text/xml")
        if data is not None:
            req.add_header("Content-Type", "application/xml; charset=utf-8")
        if depth is not None:
            req.add_header("Depth", depth)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise CardDAVError(
                    "Anmeldung abgelehnt (401): Benutzername oder App-Passwort prüfen."
                ) from e
            if e.code == 404:
                raise CardDAVError(f"Nicht gefunden (404): {url}") from e
            raise CardDAVError(f"{method} {url}: HTTP {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise CardDAVError(f"Server nicht erreichbar: {e.reason}") from e

    @staticmethod
    def _responses(data: bytes) -> list[ET.Element]:
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            raise CardDAVError(f"Unlesbare Antwort des Servers: {e}") from e
        return root.findall("d:response", NS)

    def address_books(self) -> list[AddressBook]:
        """Every address book of the account, as the server lists them."""
        data = self._request("PROPFIND", self.address_book_home, _PROPFIND, depth="1")
        books: list[AddressBook] = []
        for response in self._responses(data):
            href = (response.findtext("d:href", default="", namespaces=NS) or "").strip()
            if response.find(".//d:resourcetype/c:addressbook", NS) is None:
                continue
            name = (response.findtext(".//d:prop/d:displayname", default="", namespaces=NS) or "")
            full = urljoin(self.base + "/", href)
            display_name = name.strip() or full.rstrip("/").rsplit("/", 1)[-1]
            books.append(AddressBook(href=full, name=display_name))
        return books

    def address_book(self, name_or_uri: str = "") -> AddressBook:
        """The address book named by display name or uri; the only one when the name is empty."""
        books = self.address_books()
        if not books:
            raise CardDAVError("Der Server meldet kein Adressbuch für diesen Benutzer.")
        if not name_or_uri:
            if len(books) == 1:
                return books[0]
            names = ", ".join(f"{b.name} ({b.uri})" for b in books)
            raise CardDAVError(f"Mehrere Adressbücher, bitte eines wählen: {names}")
        wanted = name_or_uri.casefold()
        for b in books:
            if wanted in (b.uri.casefold(), b.name.casefold()):
                return b
        names = ", ".join(f"{b.name} ({b.uri})" for b in books)
        raise CardDAVError(f"Adressbuch {name_or_uri!r} nicht gefunden. Vorhanden: {names}")

    def _contacts(self, data: bytes) -> list[Contact]:
        contacts: list[Contact] = []
        for response in self._responses(data):
            href = (response.findtext("d:href", default="", namespaces=NS) or "").strip()
            etag = response.findtext(".//d:prop/d:getetag", default="", namespaces=NS) or ""
            etag = etag.strip()
            text = response.findtext(".//d:prop/c:address-data", default="", namespaces=NS) or ""
            for card in parse_vcards(text):
                contacts.append(Contact(urljoin(self.base + "/", href), etag, card,
                                        patient_from_vcard(card)))
        return contacts

    def all_items(self, book: AddressBook) -> list[Contact]:
        """Every contact of ``book``, sorted by name."""
        return self._sorted(self._contacts(self._request("REPORT", book.href, _query(""), "1")))

    def search(self, book: AddressBook, term: str) -> list[Contact]:
        """The contacts of ``book`` matching ``term`` on the server and in the local check."""
        data = self._request("REPORT", book.href, _query(_server_term(term)), "1")
        return self._sorted([k for k in self._contacts(data) if fits(k.patient, term)])

    @staticmethod
    def _sorted(contacts: list[Contact]) -> list[Contact]:
        return sorted(contacts, key=lambda k: (normalise(k.patient.surname),
                                               normalise(k.patient.first_name)))
