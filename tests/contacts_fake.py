"""A stand-in for Apple's Contacts framework as pyobjc presents it, with synthetic cards.

Only what PseudonyMed calls is there: the state of access and the request for it, the
containers (the Contacts app's address books) and the unified contacts of one container,
fetched with the keys asked for.  Nothing here is real.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pseudonymed.core.model import Patient

HOME, WORK, MOBILE = "_$!<Home>!$_", "_$!<Work>!$_", "_$!<Mobile>!$_"
UNDEFINED = sys.maxsize
NOT_DETERMINED, DENIED, AUTHORIZED = 0, 2, 3


@dataclass
class Labelled:
    """CNLabeledValue: a label and a value."""

    label_: str
    value_: Any

    def label(self) -> str:
        return self.label_

    def value(self) -> Any:
        return self.value_


@dataclass
class Phone:
    """CNPhoneNumber."""

    number: str

    def stringValue(self) -> str:  # noqa: N802 - the framework's name
        return self.number


@dataclass
class Address:
    """CNPostalAddress."""

    street_: str = ""
    city_: str = ""
    postcode_: str = ""
    country_: str = ""

    def street(self) -> str:
        return self.street_

    def city(self) -> str:
        return self.city_

    def state(self) -> str:
        return ""

    def postalCode(self) -> str:  # noqa: N802
        return self.postcode_

    def country(self) -> str:
        return self.country_


@dataclass
class Day:
    """NSDateComponents of a birthday."""

    day_: int
    month_: int
    year_: int = UNDEFINED

    def day(self) -> int:
        return self.day_

    def month(self) -> int:
        return self.month_

    def year(self) -> int:
        return self.year_


@dataclass
class Contact:
    """CNContact with the properties PseudonyMed fetches."""

    identifier_: str
    family: str = ""
    given: str = ""
    middle: str = ""
    prefix: str = ""
    suffix: str = ""
    previous_family: str = ""
    job: str = ""
    born: Day | None = None
    addresses: list[Labelled] = field(default_factory=list)
    phones: list[Labelled] = field(default_factory=list)
    emails: list[Labelled] = field(default_factory=list)

    def identifier(self) -> str:
        return self.identifier_

    def familyName(self) -> str:  # noqa: N802
        return self.family

    def givenName(self) -> str:  # noqa: N802
        return self.given

    def middleName(self) -> str:  # noqa: N802
        return self.middle

    def namePrefix(self) -> str:  # noqa: N802
        return self.prefix

    def nameSuffix(self) -> str:  # noqa: N802
        return self.suffix

    def previousFamilyName(self) -> str:  # noqa: N802
        return self.previous_family

    def jobTitle(self) -> str:  # noqa: N802
        return self.job

    def birthday(self) -> Day | None:
        return self.born

    def postalAddresses(self) -> list[Labelled]:  # noqa: N802
        return self.addresses

    def phoneNumbers(self) -> list[Labelled]:  # noqa: N802
        return self.phones

    def emailAddresses(self) -> list[Labelled]:  # noqa: N802
        return self.emails


def contact_from_patient(p: Patient, identifier: str, title: str = "") -> Contact:
    """A synthetic patient as the Contacts app would hold the card: salutation and title in
    the name prefix, the maiden name, a home address, the phones and e-mail addresses."""
    salutation = "Herr" if p.male else "Frau" if p.female else ""
    born = Day(p.birth_date.day, p.birth_date.month, p.birth_date.year) if p.birth_date else None
    return Contact(
        identifier, family=p.surname, given=p.first_name,
        prefix=" ".join(t for t in (salutation, title) if t), previous_family=p.birth_name,
        born=born, addresses=[Labelled(HOME, Address(p.street, p.place, p.postcode))],
        phones=[Labelled(MOBILE, Phone(t)) for t in p.phones],
        emails=[Labelled(HOME, e) for e in p.emails])


@dataclass
class Container:
    """CNContainer: one address book of the Contacts app."""

    identifier_: str
    name_: str
    type_: int = 3  # CNContainerTypeCardDAV
    contacts: list[Contact] = field(default_factory=list)

    def identifier(self) -> str:
        return self.identifier_

    def name(self) -> str:
        return self.name_

    def type(self) -> int:
        return self.type_


@dataclass
class Error:
    """NSError."""

    text: str

    def localizedDescription(self) -> str:  # noqa: N802
        return self.text


class FakeContacts:
    """The module ``Contacts``: give it the address books, the state of access, and what
    the person answers when macOS asks."""

    CNEntityTypeContacts = 0
    CNLabelHome = HOME
    CNLabelWork = WORK

    def __init__(self, containers: list[Container], status: int = AUTHORIZED,
                 answer: int = AUTHORIZED) -> None:
        self.containers = containers
        self.status = status
        self.answer = answer
        self.asked = 0  # how often macOS showed its prompt
        self.keys: list[list[str]] = []  # the keys of every fetch
        fake = self
        for key in ("NamePrefix", "GivenName", "MiddleName", "FamilyName", "NameSuffix",
                    "PreviousFamilyName", "Birthday", "PostalAddresses", "PhoneNumbers",
                    "EmailAddresses", "JobTitle"):
            setattr(self, f"CNContact{key}Key", key[0].lower() + key[1:])

        class CNContactStore:
            @staticmethod
            def authorizationStatusForEntityType_(_entity: int) -> int:  # noqa: N802
                return fake.status

            @staticmethod
            def alloc() -> CNContactStore:
                return CNContactStore()

            def init(self) -> CNContactStore:
                return self

            def requestAccessForEntityType_completionHandler_(  # noqa: N802
                    self, _entity: int, handler: Callable[[bool, Any], None]) -> None:
                if fake.status == NOT_DETERMINED:
                    fake.asked += 1
                    fake.status = fake.answer
                handler(fake.status == AUTHORIZED, None)

            def containersMatchingPredicate_error_(self, _predicate: Any,  # noqa: N802
                                                   _error: Any) -> tuple[Any, Any]:
                if fake.status != AUTHORIZED:
                    return None, Error("Zugriff verweigert")
                return list(fake.containers), None

            def unifiedContactsMatchingPredicate_keysToFetch_error_(  # noqa: N802
                    self, predicate: str, keys: list[str], _error: Any) -> tuple[Any, Any]:
                if fake.status != AUTHORIZED:
                    return None, Error("Zugriff verweigert")
                fake.keys.append(list(keys))
                return next(c.contacts for c in fake.containers
                            if c.identifier_ == predicate), None

        class CNContact:
            @staticmethod
            def predicateForContactsInContainerWithIdentifier_(identifier: str) -> str:  # noqa: N802
                return identifier

        self.CNContactStore = CNContactStore
        self.CNContact = CNContact
