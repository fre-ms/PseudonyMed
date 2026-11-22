"""Step 5: put the clear data back into the model's text."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.config import load_settings, save_settings
from pseudonymed.gui.session import Session
from pseudonymed.pseudonyms import capsule as capsule_module
from pseudonymed.pseudonyms import pgp as pgp_module
from pseudonymed.pseudonyms.capsule import Capsule
from pseudonymed.pseudonyms.matching import match_candidates
from pseudonymed.pseudonyms.reidentification import reidentify


class _MappingSource(NamedTuple):
    """Where the clear data come from: the mapping pseudonym → clear text, the patient
    info and the identifiers this source knows (an opened capsule or the local store)."""

    mapping: dict[str, str]
    info: dict[str, Any] | None
    known: list[str]


class ReidentifyPage(QWidget):
    """Step "10 Reidentifikation": re-identify a language model's answer locally, from the
    store's mapping of a chosen person or from a sender's capsule."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        """Build the page region by region and fill the person box from the store."""
        super().__init__(parent)
        self.session = session
        self.synchronous = False
        self.capsule: Capsule | None = None
        self.passphrase: str | None = None  # tests set it; the app asks
        outer = QVBoxLayout(self)
        self._build_capsule_group(outer)
        self._build_person_row(outer)
        self._build_input(outer)
        self._build_output(outer)
        self.refresh()

    # -- build -------------------------------------------------------------------------------------
    def _build_capsule_group(self, outer: QVBoxLayout) -> None:
        """The capsule group: open and close buttons, the private-key field with its file
        picker, and the explanation; disabled without an OpenPGP engine."""
        capsule_group = QGroupBox(self.tr("Schlüsselkapsel eines Absenders"))
        capsule_layout = QVBoxLayout(capsule_group)
        line = QHBoxLayout()
        self.capsule_button = QPushButton(self.tr("Kapsel öffnen …"))
        self.capsule_button.clicked.connect(self.choose_capsule)
        self.close_capsule_button = QPushButton(self.tr("Kapsel schließen"))
        self.close_capsule_button.clicked.connect(self.close_capsule)
        self.close_capsule_button.setEnabled(False)
        line.addWidget(self.capsule_button)
        line.addWidget(self.close_capsule_button)
        line.addWidget(QLabel(self.tr("Privater Schlüssel (Datei; leer = GnuPG-Schlüsselbund)")))
        self.private_key = QLineEdit(load_settings().pgp_private_key)
        self.private_key.editingFinished.connect(self._remember_key)
        line.addWidget(self.private_key, 1)
        choose_key = QPushButton("…")
        choose_key.clicked.connect(self.choose_key)
        line.addWidget(choose_key)
        capsule_layout.addLayout(line)
        self.capsule_info = QLabel(self.tr(
            "Eine Kapsel (<name>_pseudonym.kapsel.asc) kommt vom Absender eines pseudonymisierten"
            " Dokuments und enthält die Zuordnung, verschlüsselt für Ihren Schlüssel."))
        self.capsule_info.setWordWrap(True)
        capsule_layout.addWidget(self.capsule_info)
        if not pgp_module.available():
            capsule_group.setEnabled(False)
            capsule_group.setToolTip(self.tr("Extra 'pgp' (pysequoia) oder GnuPG installieren."))
        outer.addWidget(capsule_group)

    def _build_person_row(self, outer: QVBoxLayout) -> None:
        """The person box, the name form box and the button that reloads the list."""
        top = QHBoxLayout()
        top.addWidget(QLabel(self.tr("Person:")))
        self.person = QComboBox()
        top.addWidget(self.person, 1)
        self.form = QComboBox()
        self.form.addItem(self.tr("voller Name"), "vollname")
        self.form.addItem(self.tr("Herr/Frau Nachname"), "anrede")
        top.addWidget(self.form)
        new = QPushButton(self.tr("Liste aktualisieren"))
        new.clicked.connect(self.refresh)
        top.addWidget(new)
        outer.addLayout(top)

    def _build_input(self, outer: QVBoxLayout) -> None:
        """The input text with the load button and the re-identify button beneath it."""
        self.input_text = QPlainTextEdit()
        self.input_text.setPlaceholderText(self.tr("Text des Sprachmodells hier einfügen …"))
        outer.addWidget(self.input_text, 1)
        middle = QHBoxLayout()
        load = QPushButton(self.tr("Datei laden …"))
        load.clicked.connect(self.load)
        button = QPushButton(self.tr("Rückersetzen"))
        button.clicked.connect(self.reidentify)
        middle.addWidget(load)
        middle.addStretch(1)
        middle.addWidget(button)
        outer.addLayout(middle)

    def _build_output(self, outer: QVBoxLayout) -> None:
        """The read-only output text with the hint line and the copy and save buttons."""
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        outer.addWidget(self.output, 1)
        bottom = QHBoxLayout()
        self.note = QLabel("")
        self.note.setWordWrap(True)
        copy = QPushButton(self.tr("Kopieren"))
        copy.clicked.connect(self.copy)
        save = QPushButton(self.tr("Speichern …"))
        save.clicked.connect(self.save)
        bottom.addWidget(self.note, 1)
        bottom.addWidget(copy)
        bottom.addWidget(save)
        outer.addLayout(bottom)

    def refresh(self) -> None:
        """Reload the person box from the store and preselect the session's patient."""
        self.person.clear()
        with self.session.store() as sp:
            for e in sp.find():
                self.person.addItem(f"{e['kennung']}  {e['nachname']}, {e['vorname']}",
                                    e["schluessel"])
        if self.session.patient is not None:
            with self.session.store() as sp:
                info = sp.patient_info(self.session.patient.key())
            if info:
                i = self.person.findData(info["schluessel"])
                if i >= 0:
                    self.person.setCurrentIndex(i)

    def load(self) -> None:
        """Pick a text file and put its content into the input field."""
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Text"), "",
                                              self.tr("Text (*.txt *.md);;Alle (*)"))
        if path:
            with open(path, encoding="utf-8", errors="replace") as f:
                self.input_text.setPlainText(f.read())

    # -- capsules ----------------------------------------------------------------------------------
    def choose_key(self) -> None:
        """Pick the private key file and remember it in the settings."""
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Privater Schlüssel"), "",
            self.tr("OpenPGP-Schlüssel (*.asc *.gpg *.pgp);;Alle Dateien (*)"))
        if path:
            self.private_key.setText(path)
            self._remember_key()

    def _remember_key(self) -> None:
        """Store the key field in the settings when it changed; a failed write is ignored."""
        e = load_settings()
        if e.pgp_private_key != self.private_key.text().strip():
            e.pgp_private_key = self.private_key.text().strip()
            try:
                save_settings(e)
            except OSError:
                pass

    def choose_capsule(self) -> None:
        """Pick a capsule file and open it."""
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Schlüsselkapsel"), "",
            self.tr("Kapseln (*.kapsel.asc *.asc);;Alle Dateien (*)"))
        if path:
            self.open_capsule(Path(path))

    def open_capsule(self, path: Path, passphrase: str | None = None) -> Capsule | None:
        """Decrypt a capsule (asking for the passphrase when a key file needs one) and
        switch the re-identification to its mapping."""
        try:
            k = self._decrypt_capsule(path, passphrase)
        except (pgp_module.PGPError, capsule_module.CapsuleError, OSError) as e:
            self.capsule_info.setText(self.tr("Kapsel nicht lesbar: {fehler}").format(fehler=e))
            if self.synchronous:
                raise RuntimeError(str(e)) from e
            return None
        if k is None:
            return None
        self.capsule = k
        self.capsule_info.setText(self.tr("Kapsel geöffnet: {beschreibung}.").format(
            beschreibung=k.description()) + self._local_matching(k))
        self.person.setEnabled(False)
        self.close_capsule_button.setEnabled(True)
        return k

    def _decrypt_capsule(self, path: Path, passphrase: str | None) -> Capsule | None:
        """Open the capsule with the configured key; when the key wants a passphrase that was
        not given, ask for it once (None when the dialogue is cancelled).  Every other
        failure propagates to the caller."""
        key = self.private_key.text().strip() or None
        passphrase = passphrase if passphrase is not None else self.passphrase
        try:
            return capsule_module.open_file(path, key, passphrase)
        except pgp_module.PGPError as e:
            if "Passphrase" not in str(e) or self.synchronous:
                raise
        input_text, ok = QInputDialog.getText(
            self, self.tr("Passphrase"),
            self.tr("Passphrase des privaten Schlüssels:"), QLineEdit.EchoMode.Password)
        if not ok:
            return None
        return capsule_module.open_file(path, key, input_text)

    def _local_matching(self, k: Capsule) -> str:
        """A sentence saying whether the capsule's person matches an entry of the local
        store; empty when the store cannot be read."""
        try:
            with self.session.store() as sp:
                same_entries = [v for v in match_candidates(k.as_patient(), sp.candidates())
                                if v.same]
        except OSError:
            return ""
        if same_entries:
            return self.tr(" Entspricht dem lokalen Eintrag {kennung}.").format(
                kennung=same_entries[0].code)
        return self.tr(" Kein passender Eintrag in der lokalen Datenbank.")

    def close_capsule(self) -> None:
        """Drop the capsule and re-identify from the local store again."""
        self.capsule = None
        self.person.setEnabled(True)
        self.close_capsule_button.setEnabled(False)
        self.capsule_info.setText(self.tr("Kapsel geschlossen; wieder lokale Datenbank."))

    def reidentify(self) -> None:
        """Put the clear data back into the input text and report what stayed unresolved."""
        source = self._mapping_source()
        if source is None:
            self.note.setText(self.tr("Keine Person gewählt."))
            return
        r = reidentify(self.input_text.toPlainText(), source.mapping, source.info,
                       form=self.form.currentData(), known_codes=source.known)
        self.output.setPlainText(r.text)
        if r.remaining:
            parts = [f"{rest} ({r.notes[rest]})" if rest in r.notes else rest
                     for rest in r.remaining]
            self.note.setText(self.tr("{n} Ersetzungen; nicht aufgelöst: ").format(n=r.replaced)
                              + ", ".join(parts))
        else:
            self.note.setText(self.tr("{n} Ersetzungen, nichts offen.").format(n=r.replaced))

    def _mapping_source(self) -> _MappingSource | None:
        """The opened capsule if there is one, else the person chosen from the local store;
        None when no person is chosen."""
        if self.capsule is not None:
            return _MappingSource(self.capsule.mappings, self.capsule.info(),
                                  [self.capsule.code])
        key = self.person.currentData()
        if not key:
            return None
        with self.session.store() as sp:
            return _MappingSource(sp.mapping(key), sp.patient_info(key),
                                  sp.codes())

    def copy(self) -> None:
        """Copy the output text to the clipboard."""
        QApplication.clipboard().setText(self.output.toPlainText())

    def save(self) -> None:
        """Save the output text to a file of the user's choice."""
        path, _ = QFileDialog.getSaveFileName(self, self.tr("Speichern"), "",
                                              self.tr("Text (*.txt)"))
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.output.toPlainText())
