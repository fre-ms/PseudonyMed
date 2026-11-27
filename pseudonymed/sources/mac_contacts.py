"""The address books of the Mac's Contacts app, read through Apple's Contacts framework.

macOS asks once whether PseudonyMed may read the contacts (the privacy prompt shows the
text from the app's Info.plist) and keeps the answer under System Settings → Privacy &
Security → Contacts.  Every address book of the Contacts app can be searched: the cards
on this Mac, iCloud, and the CardDAV and Exchange accounts set up there.

Each contact is turned into the vCard properties PseudonyMed reads from a vCard file, so
a card read here gives the same patient as the same card exported as a file.  Two things
do not come through: notes (macOS gives them only to apps holding an entitlement that
Apple grants on request) and fields of a vCard's own (``X-DOCTOLIB-PATIENT-ID``,
``X-VERSICHERTENNUMMER``), which Apple's framework does not hand out.  Nothing is written.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from pseudonymed.core.model import Patient
from pseudonymed.core.text import Text, say
from pseudonymed.sources.carddav import fits
from pseudonymed.sources.vcf import Property, VCard, patient_from_vcard

# what macOS answers when asked whether the contacts may be read
GRANTED, DENIED, RESTRICTED, UNDECIDED, UNAVAILABLE = (
    "granted", "denied", "restricted", "undecided", "unavailable")
# CNAuthorizationStatus: NotDetermined, Restricted, Denied, Authorized, Limited
_STATUS = {0: UNDECIDED, 1: RESTRICTED, 2: DENIED, 3: GRANTED, 4: GRANTED}
_UNDEFINED = sys.maxsize  # NSDateComponentUndefined: a birthday without a year
_KIND = {1: "local", 2: "exchange", 3: "carddav"}  # CNContainerType
_KEYS = ("CNContactNamePrefixKey", "CNContactGivenNameKey", "CNContactMiddleNameKey",
         "CNContactFamilyNameKey", "CNContactNameSuffixKey", "CNContactPreviousFamilyNameKey",
         "CNContactBirthdayKey", "CNContactPostalAddressesKey", "CNContactPhoneNumbersKey",
         "CNContactEmailAddressesKey", "CNContactJobTitleKey")


class MacContactsError(Exception):
    """The Contacts app could not be read: no access, or an address book is gone."""


@dataclass(frozen=True)
class MacAddressBook:
    """One address book of the Contacts app (an account there): its stable identifier, its
    name and its kind (``local``, ``carddav`` for iCloud and CardDAV servers, ``exchange``)."""

    identifier: str
    name: str
    kind: str

    @property
    def shown(self) -> Text:
        """The name as the Contacts app's sidebar shows it, with its kind."""
        kind = {"local": say("auf diesem Mac", "on this Mac"), "carddav": say("CardDAV", "CardDAV"),
                "exchange": say("Exchange", "Exchange")}.get(self.kind, say(self.kind, self.kind))
        if not self.name:
            return kind
        return say(f"{self.name} ({kind})", f"{self.name} ({kind.en})")

    def answers_to(self, wish: str) -> bool:
        """Whether ``wish`` names this book: its identifier, or its name in any case."""
        return wish == self.identifier or (bool(self.name)
                                           and wish.casefold() == self.name.casefold())


def _framework() -> Any:
    """Apple's Contacts framework through pyobjc; None off macOS or without pyobjc."""
    if sys.platform != "darwin":
        return None
    try:
        import Contacts
    except ImportError:
        return None
    return Contacts


def supported() -> bool:
    """True on a Mac whose Contacts framework PseudonyMed can reach."""
    return _framework() is not None


def access() -> str:
    """Whether the contacts may be read: granted, denied, restricted (by a profile),
    undecided (never asked) or unavailable (not a Mac).  Asks nothing and reads nothing."""
    cn = _framework()
    if cn is None:
        return UNAVAILABLE
    status = cn.CNContactStore.authorizationStatusForEntityType_(cn.CNEntityTypeContacts)
    return _STATUS.get(int(status), DENIED)


def access_text(state: str) -> Text:
    """The state of access in words, with the way to change it where that is needed."""
    return {
        GRANTED: say("erlaubt", "allowed"),
        UNDECIDED: say("noch nicht gefragt", "not asked yet"),
        DENIED: say("verweigert (Systemeinstellungen → Datenschutz & Sicherheit → Kontakte)",
                    "refused (System Settings → Privacy & Security → Contacts)"),
        RESTRICTED: say("durch eine Verwaltung des Mac gesperrt",
                        "blocked by the Mac's management"),
        UNAVAILABLE: say("nicht verfügbar (nur auf dem Mac)", "not available (on a Mac only)"),
    }.get(state, say(state, state))


def _wait(done: threading.Event, timeout: float) -> None:
    """Wait for macOS's answer.  On the main thread the run loop keeps turning meanwhile,
    in case the answer is delivered there."""
    if threading.current_thread() is not threading.main_thread():
        done.wait(timeout)
        return
    from Foundation import NSDate, NSRunLoop

    end = time.monotonic() + timeout
    while not done.is_set() and time.monotonic() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))


def ask(timeout: float = 300.0) -> str:
    """Ask macOS for access when it has never been asked (it shows its privacy prompt
    once); the state afterwards.  An answer given before is not asked again."""
    state = access()
    if state != UNDECIDED:
        return state
    cn = _framework()
    done = threading.Event()
    store = cn.CNContactStore.alloc().init()
    store.requestAccessForEntityType_completionHandler_(
        cn.CNEntityTypeContacts, lambda _granted, _error: done.set())
    if not done.is_set():
        _wait(done, timeout)
    return access()


def _store() -> Any:
    """A contact store, once macOS has allowed reading; MacContactsError otherwise."""
    state = access()
    if state != GRANTED:
        raise MacContactsError(say(
            f"Kein Zugriff auf die Kontakte dieses Mac ({access_text(state)}).",
            f"No access to the contacts on this Mac ({access_text(state).en})."))
    return _framework().CNContactStore.alloc().init()


def _failure(error: Any) -> MacContactsError:
    """An error of the framework as PseudonyMed's own."""
    reason = str(error.localizedDescription()) if error is not None else ""
    return MacContactsError(say(f"Kontakte dieses Mac: {reason}",
                                f"Contacts on this Mac: {reason}"))


def address_books() -> list[MacAddressBook]:
    """The address books of the Contacts app, in the order macOS gives them."""
    store = _store()
    containers, error = store.containersMatchingPredicate_error_(None, None)
    if containers is None:
        raise _failure(error)
    return [MacAddressBook(str(c.identifier()), str(c.name() or ""),
                           _KIND.get(int(c.type()), "other")) for c in containers]


def choose(books: list[MacAddressBook], wanted: list[str]) -> list[MacAddressBook]:
    """The books ``wanted`` names, by identifier or by name (case does not matter);
    MacContactsError names the ones there are when a wish matches none."""
    chosen: list[MacAddressBook] = []
    for wish in wanted:
        hits = [b for b in books if b.answers_to(wish)]
        if not hits:
            raise _no_such_book(wish, books)
        chosen += [b for b in hits if b not in chosen]
    return chosen


def _no_such_book(wish: str, books: list[MacAddressBook]) -> MacContactsError:
    """The error for a wish that names no address book, with the ones there are."""
    there = ", ".join(b.shown for b in books) or "–"
    there_en = ", ".join(b.shown.en for b in books) or "–"
    return MacContactsError(say(
        f"Kein Adressbuch {wish!r} in der Kontakte-App (vorhanden: {there}).",
        f"No address book {wish!r} in the Contacts app (there are: {there_en})."))


def _escaped(text: Any) -> str:
    """A value of a structured vCard property with its separators escaped."""
    value = str(text or "")
    for raw, escaped in (("\\", "\\\\"), (";", "\\;"), (",", "\\,"), ("\n", "\\n")):
        value = value.replace(raw, escaped)
    return value


def _labelled(values: Any) -> list[tuple[str, Any]]:
    """Labelled values of a contact (phones, e-mail addresses, addresses) as pairs."""
    return [(str(v.label() or ""), v.value()) for v in (values or [])]


def _types(label: str, cn: Any) -> dict[str, list[str]]:
    """The vCard TYPE of an address label: home or work, else none."""
    kind = {str(cn.CNLabelHome): "home", str(cn.CNLabelWork): "work"}.get(label)
    return {"TYPE": [kind]} if kind else {}


def _birthday(contact: Any) -> str:
    """The birthday as BDAY writes it, ``--MMDD`` when the year is not known."""
    born = contact.birthday()
    if born is None:
        return ""
    day, month, year = int(born.day()), int(born.month()), int(born.year())
    if _UNDEFINED in (day, month):
        return ""
    return f"--{month:02d}{day:02d}" if year == _UNDEFINED else f"{year:04d}{month:02d}{day:02d}"


def vcard(contact: Any, cn: Any) -> VCard:
    """One contact of the framework as the vCard properties PseudonyMed reads."""
    name = ";".join(_escaped(part) for part in (
        contact.familyName(), contact.givenName(), contact.middleName(), contact.namePrefix(),
        contact.nameSuffix()))
    card = VCard([Property("UID", value=_escaped(contact.identifier())),
                  Property("N", value=name)])
    simple = (("X-MAIDENNAME", contact.previousFamilyName()), ("BDAY", _birthday(contact)),
              ("ROLE", contact.jobTitle()))
    card.properties += [Property(n, value=_escaped(v)) for n, v in simple if v]
    for label, address in _labelled(contact.postalAddresses()):
        parts = ("", "", address.street(), address.city(), address.state(),
                 address.postalCode(), address.country())
        card.properties.append(Property("ADR", _types(label, cn),
                                        ";".join(_escaped(p) for p in parts)))
    card.properties += [Property("TEL", value=_escaped(number.stringValue()))
                        for _label, number in _labelled(contact.phoneNumbers())]
    card.properties += [Property("EMAIL", value=_escaped(address))
                        for _label, address in _labelled(contact.emailAddresses())]
    return card


def cards(identifiers: list[str] | None = None) -> list[VCard]:
    """The contacts of the chosen address books (all of them when none is chosen) as
    vCards; a contact linked across books comes once."""
    cn = _framework()
    store = _store()
    books = address_books()
    if identifiers:
        books = [b for b in books if b.identifier in identifiers]
        if not books:
            raise MacContactsError(say(
                "Die gewählten Adressbücher gibt es in der Kontakte-App nicht mehr;"
                " bitte unter „Adressbücher einrichten …“ neu wählen.",
                "The chosen address books are no longer in the Contacts app; please choose"
                " again under “Set up address books …”."))
    keys = [getattr(cn, key) for key in _KEYS]
    found: dict[str, VCard] = {}
    for book in books:
        predicate = cn.CNContact.predicateForContactsInContainerWithIdentifier_(book.identifier)
        contacts, error = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
            predicate, keys, None)
        if contacts is None:
            raise _failure(error)
        for contact in contacts:
            found.setdefault(str(contact.identifier()), vcard(contact, cn))
    return list(found.values())


def search(term: str, identifiers: list[str] | None = None) -> list[Patient]:
    """Patients of the chosen address books whose name or birth date fit ``term``
    (empty: every contact with a name)."""
    matches = []
    for card in cards(identifiers):
        p = patient_from_vcard(card)
        if (p.surname or p.first_name) and (not term or fits(p, term)):
            matches.append(p)
    return matches
