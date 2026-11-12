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

from pseudonymed.dokument.textdatei import lese_textdatei
from pseudonymed.erkennung.varianten import strasse_und_nummer
from pseudonymed.gui.adressbuch_dialog import AdressbuchDialog
from pseudonymed.gui.sitzung import Sitzung
from pseudonymed.kern.modell import Patient, Person, parse_datum
from pseudonymed.quellen import adressbuch
from pseudonymed.quellen.manuell import lade_patient, vorlage
from pseudonymed.quellen.vcf import patienten_aus_vcf


class Listenfeld(QPlainTextEdit):
    """One entry per line; a text file dropped on it adds its lines (one term per line,
    blank lines and # comments skipped)."""

    def canInsertFromMimeData(self, quelle) -> bool:  # noqa: N802
        return quelle.hasUrls() or super().canInsertFromMimeData(quelle)

    def insertFromMimeData(self, quelle) -> None:  # noqa: N802
        if quelle.hasUrls():
            zeilen: list[str] = []
            for url in quelle.urls():
                if url.isLocalFile():
                    zeilen += zeilen_aus_datei(Path(url.toLocalFile()))
            self.haenge_an(zeilen)
            return
        super().insertFromMimeData(quelle)

    def haenge_an(self, zeilen: list[str]) -> int:
        """Append the lines that are not in the field yet and return how many were new."""
        vorhanden = [z.strip() for z in self.toPlainText().splitlines() if z.strip()]
        neue = [z for z in zeilen if z not in vorhanden]
        if neue:
            self.setPlainText("\n".join(vorhanden + neue))
        return len(neue)


def zeilen_aus_datei(pfad: Path) -> list[str]:
    """The distinct non-empty lines of a text file, # comments skipped; an unreadable
    file yields nothing."""
    try:
        text = lese_textdatei(pfad)
    except OSError:
        return []
    aus: list[str] = []
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if zeile and not zeile.startswith("#") and zeile not in aus:
            aus.append(zeile)
    return aus


def _personen(text: str) -> list[Person]:
    """One person per non-empty line: leading titles (Dr., Prof., med., PD, Dipl.) are
    split off, "Nachname, Vorname" is read as such, otherwise the last word is the
    surname."""
    personen = []
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if not zeile:
            continue
        titel = ""
        teile = zeile.split()
        while teile and teile[0].rstrip(".").lower() in ("dr", "prof", "med", "pd", "dipl"):
            titel = (titel + " " + teile.pop(0)).strip()
        if not teile:
            continue
        if "," in zeile:
            nach, _, vor = zeile.replace(titel, "", 1).partition(",")
            personen.append(Person(nachname=nach.strip(), vorname=vor.strip(), titel=titel))
        else:
            personen.append(Person(nachname=teile[-1], vorname=" ".join(teile[:-1]), titel=titel))
    return personen


class PatientSeite(QWidget):
    """Step "1 Patientendaten": the patient from the address book, a vCard or a patient
    file, with the derived identifiers in editable fields and three list fields for
    relatives, referrers and further terms."""

    uebernommen = Signal(object)  # Patient

    def __init__(self, sitzung: Sitzung, eltern: QWidget | None = None) -> None:
        """Build the page region by region, top to bottom."""
        super().__init__(eltern)
        self.sitzung = sitzung
        self.synchron = False
        self._treffer: list[Patient] = []
        aussen = QVBoxLayout(self)
        self._baue_quelle(aussen)
        self._baue_treffer(aussen)
        self._baue_felder(aussen)
        self._baue_zusatz(aussen)
        self._baue_fusszeile(aussen)

    # -- build -------------------------------------------------------------------------------------
    def _baue_quelle(self, aussen: QVBoxLayout) -> None:
        """The "Quelle" group: the search field and the buttons for address book, patient
        file, template and vCard."""
        quelle = QGroupBox(self.tr("Quelle"))
        q = QHBoxLayout(quelle)
        self.suche = QLineEdit()
        self.suche.setPlaceholderText(self.tr("Name, Geburtsjahr oder Geburtsdatum …"))
        self.suche.returnPressed.connect(self.suchen)
        knopf_suche = QPushButton(self.tr("Im Adressbuch suchen"))
        knopf_suche.clicked.connect(self.suchen)
        knopf_adressbuch = QPushButton(self.tr("Adressbücher einrichten …"))
        knopf_adressbuch.setToolTip(self.tr(
            "Lokale vCard-Datei oder -Ordner und Nextcloud (CardDAV)"))
        knopf_adressbuch.clicked.connect(self.adressbuecher_einrichten)
        knopf_datei = QPushButton(self.tr("Patientendatei …"))
        knopf_datei.setToolTip(self.tr(
            "JSON- oder TOML-Datei mit den Identifikatoren, wie 'pseudonymed kontakte export' sie"
            " schreibt; eine leere Vorlage gibt „Vorlage speichern …“."))
        knopf_datei.clicked.connect(self.datei_laden)
        knopf_vorlage = QPushButton(self.tr("Vorlage speichern …"))
        knopf_vorlage.setToolTip(self.tr("Leere Patientendatei (JSON) zum Ausfüllen"))
        knopf_vorlage.clicked.connect(self.vorlage_speichern)
        knopf_vcf = QPushButton(self.tr("vCard …"))
        knopf_vcf.setToolTip(self.tr("Eine einzelne vCard-Datei (.vcf) laden"))
        knopf_vcf.clicked.connect(self.vcf_laden)
        q.addWidget(self.suche, 1)
        q.addWidget(knopf_suche)
        q.addWidget(knopf_adressbuch)
        q.addWidget(knopf_datei)
        q.addWidget(knopf_vorlage)
        q.addWidget(knopf_vcf)
        aussen.addWidget(quelle)

    def _baue_treffer(self, aussen: QVBoxLayout) -> None:
        """The hit list, hidden until a search or a file delivered patients."""
        self.treffer_gruppe = QGroupBox(self.tr("Treffer (anklicken übernimmt die Daten)"))
        tg = QVBoxLayout(self.treffer_gruppe)
        self.liste = QListWidget()
        self.liste.setMaximumHeight(120)
        self.liste.itemSelectionChanged.connect(self._treffer_gewaehlt)
        tg.addWidget(self.liste)
        self.treffer_gruppe.setVisible(False)  # shown once a search or a file delivered hits
        aussen.addWidget(self.treffer_gruppe)

    def _baue_felder(self, aussen: QVBoxLayout) -> None:
        """The form of identifiers: the salutation box and one line field per identifier,
        reachable by name through ``self.f``."""
        felder = QGroupBox(self.tr("Identifikatoren (werden geschwärzt)"))
        form = QFormLayout(felder)
        self.geschlecht = QComboBox()
        self.geschlecht.addItems([self.tr("unbekannt"), self.tr("Herr"),
                                  self.tr("Frau")])
        form.addRow(self.tr("Anrede"), self.geschlecht)
        self.f: dict[str, QLineEdit] = {}
        for name, titel in (("titel", self.tr("Titel")), ("vorname", self.tr("Vorname(n)")),
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
            form.addRow(titel, self.f[name])
        self.f["hausnummer"].setMaximumWidth(120)
        aussen.addWidget(felder)

    def _baue_zusatz(self, aussen: QVBoxLayout) -> None:
        """The three list fields for relatives, referrers and further terms, side by side."""
        zusatz = QGroupBox(self.tr(
            "Weitere Personen und Begriffe (eine je Zeile; Textdatei hierher ziehen)"))
        z = QHBoxLayout(zusatz)
        self.angehoerige = Listenfeld()
        self.angehoerige.setPlaceholderText(self.tr(
            "Angehörige: Vorname Nachname\n(Textdatei mit einem Namen je Zeile hierher ziehen)"))
        self.zuweiser = Listenfeld()
        self.zuweiser.setPlaceholderText(self.tr(
            "Zuweiser: Dr. med. Vorname Nachname\n(Textdatei hierher ziehen)"))
        self.begriffe = Listenfeld()
        self.begriffe.setPlaceholderText(self.tr(
            "Begriffe: weitere Namen, Praxen, Nummern\n(Textdatei hierher ziehen)"))
        for w in (self.angehoerige, self.zuweiser, self.begriffe):
            w.setMaximumHeight(90)
            z.addWidget(w)
        aussen.addWidget(zusatz)

    def _baue_fusszeile(self, aussen: QVBoxLayout) -> None:
        """The hint line and the default button that hands the patient to the session."""
        unten = QHBoxLayout()
        self.hinweis = QLabel("")
        self.hinweis.setWordWrap(True)
        knopf = QPushButton(self.tr("Patient übernehmen"))
        knopf.setDefault(True)
        knopf.clicked.connect(self.uebernehmen)
        unten.addWidget(self.hinweis, 1)
        unten.addWidget(knopf)
        aussen.addLayout(unten)

    # -- sources -----------------------------------------------------------------------------------
    def _melde(self, titel: str, text: str) -> None:
        """Show an information box, or raise in the synchronous test mode (no modal dialogue
        can be answered offscreen)."""
        if self.synchron:
            raise RuntimeError(text)
        QMessageBox.information(self, titel, text)

    def adressbuecher_einrichten(self) -> None:
        """Open the address-book dialogue and confirm in the hint line when it was saved."""
        dialog = AdressbuchDialog(self)
        if dialog.exec():
            self.hinweis.setText(self.tr("Adressbücher gespeichert."))

    def suchen(self) -> adressbuch.Suchergebnis | None:
        """Search the configured address books for the field's term and show the hits;
        None without a term or without any address book (then the dialogue opens)."""
        begriff = self.suche.text().strip()
        if not begriff:
            return None
        if not adressbuch.eingerichtet():
            self.hinweis.setText(self.tr(
                "Kein Adressbuch eingerichtet: lokale vCard-Datei oder Nextcloud unter"
                " „Adressbücher einrichten …“."))
            if not self.synchron:
                self.adressbuecher_einrichten()
            return None
        ergebnis = adressbuch.suche(begriff)
        if ergebnis.fehler:
            self.hinweis.setText(" | ".join(ergebnis.fehler))
            if len(ergebnis.fehler) == len(ergebnis.quellen) and not self.synchron:
                QMessageBox.warning(self, self.tr("Adressbuch"), "\n".join(ergebnis.fehler))
        self.zeige_treffer(ergebnis.patienten)
        return ergebnis

    def datei_laden(self) -> None:
        """Pick a JSON or TOML patient file and show its patient as the single hit."""
        pfad, _ = QFileDialog.getOpenFileName(
            self, self.tr("Patientendatei"), "",
            self.tr("Patientendatei JSON/TOML (*.json *.toml);;Alle Dateien (*)"))
        if pfad:
            try:
                self.zeige_treffer([lade_patient(pfad)])
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, self.tr("Patientendatei"), str(e))

    def vorlage_speichern(self) -> None:
        """Write an empty patient file (JSON) to a place of the user's choice."""
        pfad, _ = QFileDialog.getSaveFileName(self, self.tr("Vorlage speichern"),
                                              "patient.json", self.tr("JSON (*.json)"))
        if pfad:
            Path(pfad).write_text(vorlage(), encoding="utf-8")
            self.hinweis.setText(self.tr("Vorlage geschrieben: {pfad}").format(pfad=pfad))

    def vcf_laden(self) -> None:
        """Pick a vCard file and show its named contacts as hits."""
        pfad, _ = QFileDialog.getOpenFileName(self, self.tr("vCard"), "", self.tr("vCard (*.vcf)"))
        if pfad:
            self.zeige_treffer([p for p in patienten_aus_vcf(pfad) if p.nachname or p.vorname])

    def zeige_treffer(self, patienten: list[Patient]) -> None:
        """List the patients found; a single hit fills the form at once, none reports
        "Kein Treffer"."""
        self._treffer = patienten
        self.treffer_gruppe.setVisible(bool(patienten))
        self.liste.clear()
        for p in patienten:
            geb = p.geburtsdatum.strftime("%d.%m.%Y") if p.geburtsdatum else "–"
            ort = " ".join(t for t in (p.plz, p.ort) if t)
            QListWidgetItem(self.tr("{nachname}, {vorname}  geb. {geb}  {ort}").format(
                nachname=p.nachname, vorname=p.vorname, geb=geb, ort=ort), self.liste)
        if len(patienten) == 1:
            self.liste.setCurrentRow(0)
        elif not patienten:
            self.hinweis.setText(self.tr("Kein Treffer."))

    def _treffer_gewaehlt(self) -> None:
        """Fill the form with the hit selected in the list."""
        zeile = self.liste.currentRow()
        if 0 <= zeile < len(self._treffer):
            self.fuelle(self._treffer[zeile])

    # -- form ------------------------------------------------------------------------------------
    def fuelle(self, p: Patient) -> None:
        """Put a patient's identifiers into the fields; the card's notes go to the hint line."""
        strasse, hausnummer = strasse_und_nummer(p.strasse)
        werte = {
            "nachname": p.nachname, "vorname": p.vorname, "geburtsname": p.geburtsname,
            "titel": p.titel,
            "geburtsdatum": p.geburtsdatum.strftime("%d.%m.%Y") if p.geburtsdatum else "",
            "strasse": strasse, "hausnummer": hausnummer, "plz": p.plz, "ort": p.ort,
            "telefone": "; ".join(p.telefone),
            "emails": "; ".join(p.emails), "versichertennummer": p.versichertennummer,
            "patienten_id": p.patienten_id, "aktenzeichen": "; ".join(p.aktenzeichen),
        }
        for name, wert in werte.items():
            self.f[name].setText(wert)
        self.geschlecht.setCurrentIndex({"m": 1, "w": 2}.get(p.geschlecht, 0))
        self.angehoerige.setPlainText("\n".join(a.vollname for a in p.angehoerige))
        self.zuweiser.setPlainText("\n".join(" ".join(t for t in (z.titel, z.vollname) if t)
                                             for z in p.zuweiser))
        self.begriffe.setPlainText("\n".join(p.begriffe))
        if p.hinweise:
            self.hinweis.setText(self.tr("Notizen der Karte: ") + " | ".join(p.hinweise))
        else:
            self.hinweis.setText("")

    def patient(self) -> Patient:
        """The patient as the fields describe them now; street and house number are joined
        again, an unreadable birth date becomes None."""
        def liste(name: str) -> list[str]:
            """The non-empty ;-separated entries of a field."""
            return [t.strip() for t in self.f[name].text().split(";") if t.strip()]

        geburtsdatum: dt.date | None = parse_datum(self.f["geburtsdatum"].text()) \
            if self.f["geburtsdatum"].text().strip() else None
        return Patient(
            nachname=self.f["nachname"].text().strip(),
            vorname=self.f["vorname"].text().strip(),
            geschlecht={1: "m", 2: "w"}.get(self.geschlecht.currentIndex(), ""),
            titel=self.f["titel"].text().strip(),
            geburtsname=self.f["geburtsname"].text().strip(),
            geburtsdatum=geburtsdatum,
            strasse=" ".join(t for t in (self.f["strasse"].text().strip(),
                                         self.f["hausnummer"].text().strip()) if t),
            plz=self.f["plz"].text().strip(),
            ort=self.f["ort"].text().strip(),
            telefone=liste("telefone"),
            emails=liste("emails"),
            versichertennummer=self.f["versichertennummer"].text().strip(),
            patienten_id=self.f["patienten_id"].text().strip(),
            aktenzeichen=liste("aktenzeichen"),
            angehoerige=_personen(self.angehoerige.toPlainText()),
            zuweiser=_personen(self.zuweiser.toPlainText()),
            begriffe=[b.strip() for b in self.begriffe.toPlainText().splitlines() if b.strip()],
        )

    def uebernehmen(self) -> None:
        """Hand the patient to the session and emit ``uebernommen``; a missing surname or an
        unreadable birth date is refused with a warning."""
        p = self.patient()
        if not p.nachname:
            QMessageBox.warning(self, self.tr("Patient"), self.tr("Der Nachname fehlt."))
            return
        if self.f["geburtsdatum"].text().strip() and p.geburtsdatum is None:
            QMessageBox.warning(self, self.tr("Patient"),
                                self.tr("Das Geburtsdatum ist nicht lesbar (TT.MM.JJJJ)."))
            return
        self.sitzung.patient = p
        self.sitzung.abgleich = None
        self.hinweis.setText(self.tr("Übernommen: {name}").format(name=p.vollname))
        self.uebernommen.emit(p)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.suche.hasFocus():
            self.suchen()
            return
        super().keyPressEvent(event)


__all__ = ["PatientSeite", "Path"]
