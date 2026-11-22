"""Setting up the address books: a local vCard file or folder, and Nextcloud (CardDAV)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.config import (
    CardDAVConfig,
    keyring_backend,
    load_config,
    load_password,
    load_settings,
    save_config,
    save_password,
    save_settings,
)
from pseudonymed.gui.translation import shown_error
from pseudonymed.sources.address_book import local_files
from pseudonymed.sources.carddav import CardDAVClient, CardDAVError


class AddressBookDialog(QDialog):
    """Where patients come from: a local vCard file or folder, and a Nextcloud address
    book over CardDAV with its app password kept in the keyring."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Adressbücher einrichten"))
        self.setMinimumWidth(640)
        outer = QVBoxLayout(self)

        local = QGroupBox(self.tr("Lokales Adressbuch"))
        lf = QFormLayout(local)
        line = QHBoxLayout()
        self.local = QLineEdit(load_settings().local_address_book)
        self.local.setPlaceholderText(self.tr(
            "vCard-Datei (.vcf, auch mit vielen Karten) oder Ordner mit vCard-Dateien"))
        file = QPushButton(self.tr("Datei …"))
        file.clicked.connect(self.choose_local_file)
        folder = QPushButton(self.tr("Ordner …"))
        folder.clicked.connect(self.choose_local_folder)
        line.addWidget(self.local, 1)
        line.addWidget(file)
        line.addWidget(folder)
        lf.addRow(line)
        self.local_info = QLabel(self.tr(
            "Exportieren Sie Kontakte aus Apple Kontakte, Thunderbird, Outlook oder Nextcloud als"
            " vCard; die Datei bleibt auf diesem Rechner."))
        self.local_info.setWordWrap(True)
        lf.addRow(self.local_info)
        outer.addWidget(local)

        k = load_config()
        cloud = QGroupBox(self.tr("Nextcloud (CardDAV)"))
        cf = QFormLayout(cloud)
        self.url = QLineEdit(k.url)
        self.url.setPlaceholderText("https://cloud.example.org")
        cf.addRow(self.tr("Adresse"), self.url)
        self.user = QLineEdit(k.user)
        cf.addRow(self.tr("Benutzer"), self.user)
        self.password = QLineEdit(load_password(k) or "" if k.complete else "")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText(self.tr(
            "App-Passwort (Nextcloud → Einstellungen → Sicherheit → Geräte & Sitzungen)"))
        cf.addRow(self.tr("App-Passwort"), self.password)
        row2 = QHBoxLayout()
        self.address_book = QComboBox()
        self.address_book.setEditable(True)
        if k.address_book:
            self.address_book.addItem(k.address_book, k.address_book)
        load = QPushButton(self.tr("Adressbücher laden"))
        load.clicked.connect(self.load_address_books)
        row2.addWidget(self.address_book, 1)
        row2.addWidget(load)
        cf.addRow(self.tr("Adressbuch"), row2)
        self.cloud_info = QLabel(self.tr(
            "Das App-Passwort kommt in den Schlüsselbund des Betriebssystems ({bund}).").format(
            bund=keyring_backend()) if keyring_backend() else self.tr(
            "Kein Schlüsselbund verfügbar: das App-Passwort wird in der Konfigurationsdatei"
            " abgelegt (nur für Sie lesbar)."))
        self.cloud_info.setWordWrap(True)
        cf.addRow(self.cloud_info)
        outer.addWidget(cloud)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # -- local -------------------------------------------------------------------------------------
    def choose_local_file(self) -> None:
        """Pick a vCard file for the local address book."""
        path, _ = QFileDialog.getOpenFileName(self, self.tr("vCard-Datei"), "",
                                              self.tr("vCard (*.vcf *.vcard);;Alle Dateien (*)"))
        if path:
            self.local.setText(path)

    def choose_local_folder(self) -> None:
        """Pick a folder of vCard files for the local address book."""
        path = QFileDialog.getExistingDirectory(self, self.tr("Ordner mit vCard-Dateien"))
        if path:
            self.local.setText(path)

    # -- CardDAV -----------------------------------------------------------------------------------
    def _config(self) -> CardDAVConfig:
        """The CardDAV settings as the fields hold them now (without the password)."""
        return CardDAVConfig(url=self.url.text().strip(), user=self.user.text().strip(),
                             address_book=(self.address_book.currentData()
                                           or self.address_book.currentText()).strip())

    def load_address_books(self) -> list[str]:
        """List the server's address books in the box, keeping the one chosen before;
        returns their URIs, empty when the fields are incomplete or the server
        refuses."""
        k = self._config()
        if not k.complete or not self.password.text():
            self.status.setText(self.tr("Adresse, Benutzer und App-Passwort eingeben."))
            return []
        try:
            books = CardDAVClient(k.url, k.user, self.password.text()).address_books()
        except CardDAVError as e:
            self.status.setText(self.tr("Verbindung fehlgeschlagen: {fehler}").format(
                fehler=shown_error(e)))
            return []
        selected = k.address_book
        self.address_book.clear()
        for b in books:
            self.address_book.addItem(f"{b.name} ({b.uri})", b.uri)
        if selected:
            for i in range(self.address_book.count()):
                if self.address_book.itemData(i) == selected \
                        or selected in self.address_book.itemText(i):
                    self.address_book.setCurrentIndex(i)
        self.status.setText(self.tr("{n} Adressbuch/Adressbücher gefunden; bitte wählen.").format(
            n=len(books)))
        return [b.uri for b in books]

    def save(self) -> bool:
        """Check both sources and write the settings; a CardDAV password goes to the
        keyring, or into the configuration file when there is none.  Closes the dialog
        on success; False (with the reason in the status line) otherwise."""
        e = load_settings()
        local = self.local.text().strip()
        if local and not local_files(local):
            self.status.setText(self.tr("{pfad}: keine vCard-Datei(en) gefunden.").format(
                pfad=local))
            return False
        e.local_address_book = str(Path(local).expanduser()) if local else ""
        save_settings(e)
        k = self._config()
        password = self.password.text()
        if k.complete:
            if not password:
                self.status.setText(self.tr("Bitte das App-Passwort eingeben."))
                return False
            try:
                client = CardDAVClient(k.url, k.user, password)
                client.address_book(k.address_book)
            except CardDAVError as ex:
                self.status.setText(self.tr("Verbindung fehlgeschlagen: {fehler}").format(
                    fehler=shown_error(ex)))
                return False
            if not save_password(k, password):
                k.password_in_file = password
        save_config(k)
        self.accept()
        return True
