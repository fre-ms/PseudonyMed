"""Step 1: who is the patient.  Address book search, vCard or patient file, editable fields."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.core.model import Patient, Person, parse_date
from pseudonymed.detection.variants import street_and_number
from pseudonymed.document.text_file import read_text_file
from pseudonymed.gui.address_book_dialog import AddressBookDialog
from pseudonymed.gui.session import Session
from pseudonymed.gui.translation import shown, shown_error
from pseudonymed.sources import address_book
from pseudonymed.sources.manual import load_patient, template
from pseudonymed.sources.vcf import patients_from_vcf


class ListField(QPlainTextEdit):
    """One entry per line; a text file dropped on it adds its lines (one term per line,
    blank lines and # comments skipped)."""

    def canInsertFromMimeData(self, source) -> bool:  # noqa: N802
        return source.hasUrls() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        if source.hasUrls():
            lines: list[str] = []
            for url in source.urls():
                if url.isLocalFile():
                    lines += lines_from_file(Path(url.toLocalFile()))
            self.append_lines(lines)
            return
        super().insertFromMimeData(source)

    def append_lines(self, lines: list[str]) -> int:
        """Append the lines that are not in the field yet and return how many were new."""
        existing = [z.strip() for z in self.toPlainText().splitlines() if z.strip()]
        added = [z for z in lines if z not in existing]
        if added:
            self.setPlainText("\n".join(existing + added))
        return len(added)


def lines_from_file(path: Path) -> list[str]:
    """The distinct non-empty lines of a text file, # comments skipped; an unreadable
    file yields nothing."""
    try:
        text = read_text_file(path)
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in out:
            out.append(line)
    return out


def _parse_persons(text: str) -> list[Person]:
    """One person per non-empty line: leading titles (Dr., Prof., med., PD, Dipl.) are
    split off, "Nachname, Vorname" is read as such, otherwise the last word is the
    surname."""
    persons = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        title = ""
        parts = line.split()
        while parts and parts[0].rstrip(".").lower() in ("dr", "prof", "med", "pd", "dipl"):
            title = (title + " " + parts.pop(0)).strip()
        if not parts:
            continue
        if "," in line:
            last, _, first = line.replace(title, "", 1).partition(",")
            persons.append(Person(surname=last.strip(), first_name=first.strip(), title=title))
        else:
            persons.append(Person(surname=parts[-1], first_name=" ".join(parts[:-1]), title=title))
    return persons


class PatientPage(QWidget):
    """Step "1 Patientendaten": the patient from the address book, a vCard or a patient
    file, with the derived identifiers in editable fields and three list fields for
    relatives, referrers and further terms."""

    adopted = Signal(object)  # Patient

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        """Build the page region by region, top to bottom."""
        super().__init__(parent)
        self.session = session
        self.synchronous = False
        self._matches: list[Patient] = []
        outer = QVBoxLayout(self)
        self._build_source(outer)
        self._build_matches(outer)
        self._build_fields(outer)
        self._build_extra_lists(outer)
        self._build_footer(outer)

    # -- build -------------------------------------------------------------------------------------
    def _build_source(self, outer: QVBoxLayout) -> None:
        """The "Quelle" group: the search field and the buttons for address book, patient
        file, template and vCard."""
        source = QGroupBox(self.tr("Quelle"))
        q = QHBoxLayout(source)
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("Name, Geburtsjahr oder Geburtsdatum …"))
        self.search.returnPressed.connect(self.run_search)
        search_button = QPushButton(self.tr("Im Adressbuch suchen"))
        search_button.clicked.connect(self.run_search)
        address_book_button = QPushButton(self.tr("Adressbücher einrichten …"))
        address_book_button.setToolTip(self.tr(
            "Lokale vCard-Datei oder -Ordner und Nextcloud (CardDAV)"))
        address_book_button.clicked.connect(self.set_up_address_books)
        file_button = QPushButton(self.tr("Patientendatei …"))
        file_button.setToolTip(self.tr(
            "JSON- oder TOML-Datei mit den Identifikatoren, wie 'pseudonymed kontakte export' sie"
            " schreibt; eine leere Vorlage gibt „Vorlage speichern …“."))
        file_button.clicked.connect(self.load_file)
        template_button = QPushButton(self.tr("Vorlage speichern …"))
        template_button.setToolTip(self.tr("Leere Patientendatei (JSON) zum Ausfüllen"))
        template_button.clicked.connect(self.save_template)
        vcf_button = QPushButton(self.tr("vCard …"))
        vcf_button.setToolTip(self.tr("Eine einzelne vCard-Datei (.vcf) laden"))
        vcf_button.clicked.connect(self.import_vcf)
        q.addWidget(self.search, 1)
        q.addWidget(search_button)
        q.addWidget(address_book_button)
        q.addWidget(file_button)
        q.addWidget(template_button)
        q.addWidget(vcf_button)
        outer.addWidget(source)

    def _build_matches(self, outer: QVBoxLayout) -> None:
        """The hit list, hidden until a search or a file delivered patients."""
        self.match_group = QGroupBox(self.tr("Treffer (anklicken übernimmt die Daten)"))
        matches_layout = QVBoxLayout(self.match_group)
        self.listing = QListWidget()
        self.listing.setMaximumHeight(120)
        self.listing.itemSelectionChanged.connect(self._match_selected)
        matches_layout.addWidget(self.listing)
        self.match_group.setVisible(False)  # shown once a search or a file delivered hits
        outer.addWidget(self.match_group)

    def _build_fields(self, outer: QVBoxLayout) -> None:
        """The form of identifiers: the salutation box and one line field per identifier,
        reachable by name through ``self.f``."""
        fields = QGroupBox(self.tr("Identifikatoren (werden geschwärzt)"))
        form = QFormLayout(fields)
        self.sex = QComboBox()
        self.sex.addItems([self.tr("unbekannt"), self.tr("Herr"),
                           self.tr("Frau")])
        form.addRow(self.tr("Anrede"), self.sex)
        self.f: dict[str, QLineEdit] = {}
        for name, title in (("titel", self.tr("Titel")), ("vorname", self.tr("Vorname(n)")),
                            ("nachname", self.tr("Nachname")),
                            ("geburtsname", self.tr("Geburtsname")),
                            ("geburtsdatum", self.tr("Geburtsdatum (TT.MM.JJJJ)")),
                            ("strasse", self.tr("Straße")), ("hausnummer", self.tr("Hausnummer")),
                            ("plz", self.tr("PLZ")), ("ort", self.tr("Ort")),
                            ("emails", self.tr("E-Mail (mehrere mit ;)")),
                            ("telefone", self.tr("Telefon (mehrere mit ;)")),
                            ("versichertennummer", self.tr("Versichertennummer")),
                            ("patienten_id", self.tr("Patienten-ID")),
                            ("aktenzeichen", self.tr("Aktenzeichen (mehrere mit ;)"))):
            self.f[name] = QLineEdit()
            form.addRow(title, self.f[name])
        self.f["hausnummer"].setMaximumWidth(120)
        outer.addWidget(fields)

    def _build_extra_lists(self, outer: QVBoxLayout) -> None:
        """The three list fields for relatives, referrers and further terms, side by side."""
        extra = QGroupBox(self.tr(
            "Weitere Personen und Begriffe (eine je Zeile; Textdatei hierher ziehen)"))
        z = QHBoxLayout(extra)
        self.relatives = ListField()
        self.relatives.setPlaceholderText(self.tr(
            "Angehörige: Vorname Nachname\n(Textdatei mit einem Namen je Zeile hierher ziehen)"))
        self.referrers = ListField()
        self.referrers.setPlaceholderText(self.tr(
            "Zuweiser: Dr. med. Vorname Nachname\n(Textdatei hierher ziehen)"))
        self.terms = ListField()
        self.terms.setPlaceholderText(self.tr(
            "Begriffe: weitere Namen, Praxen, Nummern\n(Textdatei hierher ziehen)"))
        for w in (self.relatives, self.referrers, self.terms):
            w.setMaximumHeight(90)
            z.addWidget(w)
        outer.addWidget(extra)

    def _build_footer(self, outer: QVBoxLayout) -> None:
        """The hint line and the default button that hands the patient to the session."""
        bottom = QHBoxLayout()
        self.note = QLabel("")
        self.note.setWordWrap(True)
        button = QPushButton(self.tr("Patient übernehmen"))
        button.setDefault(True)
        button.clicked.connect(self.adopt)
        bottom.addWidget(self.note, 1)
        bottom.addWidget(button)
        outer.addLayout(bottom)

    # -- sources -----------------------------------------------------------------------------------
    def _notify(self, title: str, text: str) -> None:
        """Show an information box, or raise in the synchronous test mode (no modal dialogue
        can be answered offscreen)."""
        if self.synchronous:
            raise RuntimeError(text)
        QMessageBox.information(self, title, text)

    def set_up_address_books(self) -> None:
        """Open the address-book dialogue and confirm in the hint line when it was saved."""
        dialog = AddressBookDialog(self)
        if dialog.exec():
            self.note.setText(self.tr("Adressbücher gespeichert."))

    def run_search(self) -> address_book.SearchResult | None:
        """Search the configured address books for the field's term and show the hits;
        None without a term or without any address book (then the dialogue opens)."""
        term = self.search.text().strip()
        if not term:
            return None
        if not address_book.is_set_up():
            self.note.setText(self.tr(
                "Kein Adressbuch eingerichtet: lokale vCard-Datei oder Nextcloud unter"
                " „Adressbücher einrichten …“."))
            if not self.synchronous:
                self.set_up_address_books()
            return None
        result = address_book.search(term)
        if result.error:
            self.note.setText(" | ".join(map(shown, result.error)))
            if len(result.error) == len(result.sources) and not self.synchronous:
                QMessageBox.warning(self, self.tr("Adressbuch"),
                                    "\n".join(map(shown, result.error)))
        self.show_matches(result.patients)
        return result

    def load_file(self) -> None:
        """Pick a JSON or TOML patient file and show its patient as the single hit."""
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Patientendatei"), "",
            self.tr("Patientendatei JSON/TOML (*.json *.toml);;Alle Dateien (*)"))
        if path:
            try:
                self.show_matches([load_patient(path)])
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, self.tr("Patientendatei"), shown_error(e))

    def save_template(self) -> None:
        """Write an empty patient file (JSON) to a place of the user's choice."""
        path, _ = QFileDialog.getSaveFileName(self, self.tr("Vorlage speichern"),
                                              "patient.json", self.tr("JSON (*.json)"))
        if path:
            Path(path).write_text(template(), encoding="utf-8")
            self.note.setText(self.tr("Vorlage geschrieben: {pfad}").format(pfad=path))

    def import_vcf(self) -> None:
        """Pick a vCard file and show its named contacts as hits."""
        path, _ = QFileDialog.getOpenFileName(self, self.tr("vCard"), "", self.tr("vCard (*.vcf)"))
        if path:
            self.show_matches([p for p in patients_from_vcf(path) if p.surname or p.first_name])

    def show_matches(self, patients: list[Patient]) -> None:
        """List the patients found; a single hit fills the form at once, none reports
        "Kein Treffer"."""
        self._matches = patients
        self.match_group.setVisible(bool(patients))
        self.listing.clear()
        for p in patients:
            born = p.birth_date.strftime("%d.%m.%Y") if p.birth_date else "–"
            place = " ".join(t for t in (p.postcode, p.place) if t)
            QListWidgetItem(self.tr("{nachname}, {vorname}  geb. {geb}  {ort}").format(
                nachname=p.surname, vorname=p.first_name, geb=born, ort=place), self.listing)
        if len(patients) == 1:
            self.listing.setCurrentRow(0)
        elif not patients:
            self.note.setText(self.tr("Kein Treffer."))

    def _match_selected(self) -> None:
        """Fill the form with the hit selected in the list."""
        line = self.listing.currentRow()
        if 0 <= line < len(self._matches):
            self.fill_in(self._matches[line])

    # -- form ------------------------------------------------------------------------------------
    def fill_in(self, p: Patient) -> None:
        """Put a patient's identifiers into the fields; the card's notes go to the hint line."""
        street, house_number = street_and_number(p.street)
        values = {
            "nachname": p.surname, "vorname": p.first_name, "geburtsname": p.birth_name,
            "titel": p.title,
            "geburtsdatum": p.birth_date.strftime("%d.%m.%Y") if p.birth_date else "",
            "strasse": street, "hausnummer": house_number, "plz": p.postcode, "ort": p.place,
            "telefone": "; ".join(p.phones),
            "emails": "; ".join(p.emails), "versichertennummer": p.insurance_number,
            "patienten_id": p.patient_id, "aktenzeichen": "; ".join(p.case_numbers),
        }
        for name, value in values.items():
            self.f[name].setText(value)
        self.sex.setCurrentIndex({"m": 1, "w": 2}.get(p.sex, 0))
        self.relatives.setPlainText("\n".join(a.full_name for a in p.relatives))
        self.referrers.setPlainText("\n".join(" ".join(t for t in (z.title, z.full_name) if t)
                                              for z in p.referrers))
        self.terms.setPlainText("\n".join(p.terms))
        if p.notes:
            self.note.setText(self.tr("Notizen der Karte: ") + " | ".join(map(shown, p.notes)))
        else:
            self.note.setText("")

    def patient(self) -> Patient:
        """The patient as the fields describe them now; street and house number are joined
        again, an unreadable birth date becomes None."""
        def listing(name: str) -> list[str]:
            """The non-empty ;-separated entries of a field."""
            return [t.strip() for t in self.f[name].text().split(";") if t.strip()]

        birth_date: dt.date | None = parse_date(self.f["geburtsdatum"].text()) \
            if self.f["geburtsdatum"].text().strip() else None
        return Patient(
            surname=self.f["nachname"].text().strip(),
            first_name=self.f["vorname"].text().strip(),
            sex={1: "m", 2: "w"}.get(self.sex.currentIndex(), ""),
            title=self.f["titel"].text().strip(),
            birth_name=self.f["geburtsname"].text().strip(),
            birth_date=birth_date,
            street=" ".join(t for t in (self.f["strasse"].text().strip(),
                                        self.f["hausnummer"].text().strip()) if t),
            postcode=self.f["plz"].text().strip(),
            place=self.f["ort"].text().strip(),
            phones=listing("telefone"),
            emails=listing("emails"),
            insurance_number=self.f["versichertennummer"].text().strip(),
            patient_id=self.f["patienten_id"].text().strip(),
            case_numbers=listing("aktenzeichen"),
            relatives=_parse_persons(self.relatives.toPlainText()),
            referrers=_parse_persons(self.referrers.toPlainText()),
            terms=[b.strip() for b in self.terms.toPlainText().splitlines() if b.strip()],
        )

    def adopt(self) -> None:
        """Hand the patient to the session and emit ``adopted``; a missing surname or an
        unreadable birth date is refused with a warning."""
        p = self.patient()
        if not p.surname:
            QMessageBox.warning(self, self.tr("Patient"), self.tr("Der Nachname fehlt."))
            return
        if self.f["geburtsdatum"].text().strip() and p.birth_date is None:
            QMessageBox.warning(self, self.tr("Patient"),
                                self.tr("Das Geburtsdatum ist nicht lesbar (TT.MM.JJJJ)."))
            return
        self.session.patient = p
        self.session.matching = None
        self.note.setText(self.tr("Übernommen: {name}").format(name=p.full_name))
        self.adopted.emit(p)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.search.hasFocus():
            self.run_search()
            return
        super().keyPressEvent(event)


__all__ = ["PatientPage", "Path"]
