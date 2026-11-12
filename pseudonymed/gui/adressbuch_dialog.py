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

from pseudonymed.konfig import (
    CardDAVKonfig,
    lade_einstellungen,
    lade_konfig,
    passwort_laden,
    passwort_speichern,
    schluesselbund,
    speichere_einstellungen,
    speichere_konfig,
)
from pseudonymed.quellen.adressbuch import lokale_dateien
from pseudonymed.quellen.carddav import CardDAVClient, CardDAVFehler


class AdressbuchDialog(QDialog):
    """Where patients come from: a local vCard file or folder, and a Nextcloud address
    book over CardDAV with its app password kept in the keyring."""

    def __init__(self, eltern: QWidget | None = None) -> None:
        super().__init__(eltern)
        self.setWindowTitle(self.tr("Adressbücher einrichten"))
        self.setMinimumWidth(640)
        aussen = QVBoxLayout(self)

        lokal = QGroupBox(self.tr("Lokales Adressbuch"))
        lf = QFormLayout(lokal)
        zeile = QHBoxLayout()
        self.lokal = QLineEdit(lade_einstellungen().adressbuch_lokal)
        self.lokal.setPlaceholderText(self.tr(
            "vCard-Datei (.vcf, auch mit vielen Karten) oder Ordner mit vCard-Dateien"))
        datei = QPushButton(self.tr("Datei …"))
        datei.clicked.connect(self.lokal_datei)
        ordner = QPushButton(self.tr("Ordner …"))
        ordner.clicked.connect(self.lokal_ordner)
        zeile.addWidget(self.lokal, 1)
        zeile.addWidget(datei)
        zeile.addWidget(ordner)
        lf.addRow(zeile)
        self.lokal_info = QLabel(self.tr(
            "Exportieren Sie Kontakte aus Apple Kontakte, Thunderbird, Outlook oder Nextcloud als"
            " vCard; die Datei bleibt auf diesem Rechner."))
        self.lokal_info.setWordWrap(True)
        lf.addRow(self.lokal_info)
        aussen.addWidget(lokal)

        k = lade_konfig()
        cloud = QGroupBox(self.tr("Nextcloud (CardDAV)"))
        cf = QFormLayout(cloud)
        self.url = QLineEdit(k.url)
        self.url.setPlaceholderText("https://cloud.example.org")
        cf.addRow(self.tr("Adresse"), self.url)
        self.benutzer = QLineEdit(k.benutzer)
        cf.addRow(self.tr("Benutzer"), self.benutzer)
        self.passwort = QLineEdit(passwort_laden(k) or "" if k.vollstaendig else "")
        self.passwort.setEchoMode(QLineEdit.EchoMode.Password)
        self.passwort.setPlaceholderText(self.tr(
            "App-Passwort (Nextcloud → Einstellungen → Sicherheit → Geräte & Sitzungen)"))
        cf.addRow(self.tr("App-Passwort"), self.passwort)
        zeile2 = QHBoxLayout()
        self.adressbuch = QComboBox()
        self.adressbuch.setEditable(True)
        if k.adressbuch:
            self.adressbuch.addItem(k.adressbuch, k.adressbuch)
        laden = QPushButton(self.tr("Adressbücher laden"))
        laden.clicked.connect(self.lade_adressbuecher)
        zeile2.addWidget(self.adressbuch, 1)
        zeile2.addWidget(laden)
        cf.addRow(self.tr("Adressbuch"), zeile2)
        self.cloud_info = QLabel(self.tr(
            "Das App-Passwort kommt in den Schlüsselbund des Betriebssystems ({bund}).").format(
            bund=schluesselbund()) if schluesselbund() else self.tr(
            "Kein Schlüsselbund verfügbar: das App-Passwort wird in der Konfigurationsdatei"
            " abgelegt (nur für Sie lesbar)."))
        self.cloud_info.setWordWrap(True)
        cf.addRow(self.cloud_info)
        aussen.addWidget(cloud)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        aussen.addWidget(self.status)
        knoepfe = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        knoepfe.accepted.connect(self.speichern)
        knoepfe.rejected.connect(self.reject)
        aussen.addWidget(knoepfe)

    # -- local -------------------------------------------------------------------------------------
    def lokal_datei(self) -> None:
        """Pick a vCard file for the local address book."""
        pfad, _ = QFileDialog.getOpenFileName(self, self.tr("vCard-Datei"), "",
                                              self.tr("vCard (*.vcf *.vcard);;Alle Dateien (*)"))
        if pfad:
            self.lokal.setText(pfad)

    def lokal_ordner(self) -> None:
        """Pick a folder of vCard files for the local address book."""
        pfad = QFileDialog.getExistingDirectory(self, self.tr("Ordner mit vCard-Dateien"))
        if pfad:
            self.lokal.setText(pfad)

    # -- CardDAV -----------------------------------------------------------------------------------
    def _konfig(self) -> CardDAVKonfig:
        """The CardDAV settings as the fields hold them now (without the password)."""
        return CardDAVKonfig(url=self.url.text().strip(), benutzer=self.benutzer.text().strip(),
                             adressbuch=(self.adressbuch.currentData()
                                         or self.adressbuch.currentText()).strip())

    def lade_adressbuecher(self) -> list[str]:
        """List the server's address books in the box, keeping the one chosen before;
        returns their URIs, empty when the fields are incomplete or the server
        refuses."""
        k = self._konfig()
        if not k.vollstaendig or not self.passwort.text():
            self.status.setText(self.tr("Adresse, Benutzer und App-Passwort eingeben."))
            return []
        try:
            buecher = CardDAVClient(k.url, k.benutzer, self.passwort.text()).adressbuecher()
        except CardDAVFehler as e:
            self.status.setText(self.tr("Verbindung fehlgeschlagen: {fehler}").format(fehler=e))
            return []
        gewaehlt = k.adressbuch
        self.adressbuch.clear()
        for b in buecher:
            self.adressbuch.addItem(f"{b.name} ({b.uri})", b.uri)
        if gewaehlt:
            for i in range(self.adressbuch.count()):
                if self.adressbuch.itemData(i) == gewaehlt \
                        or gewaehlt in self.adressbuch.itemText(i):
                    self.adressbuch.setCurrentIndex(i)
        self.status.setText(self.tr("{n} Adressbuch/Adressbücher gefunden; bitte wählen.").format(
            n=len(buecher)))
        return [b.uri for b in buecher]

    def speichern(self) -> bool:
        """Check both sources and write the settings; a CardDAV password goes to the
        keyring, or into the configuration file when there is none.  Closes the dialog
        on success; False (with the reason in the status line) otherwise."""
        e = lade_einstellungen()
        lokal = self.lokal.text().strip()
        if lokal and not lokale_dateien(lokal):
            self.status.setText(self.tr("{pfad}: keine vCard-Datei(en) gefunden.").format(
                pfad=lokal))
            return False
        e.adressbuch_lokal = str(Path(lokal).expanduser()) if lokal else ""
        speichere_einstellungen(e)
        k = self._konfig()
        passwort = self.passwort.text()
        if k.vollstaendig:
            if not passwort:
                self.status.setText(self.tr("Bitte das App-Passwort eingeben."))
                return False
            try:
                client = CardDAVClient(k.url, k.benutzer, passwort)
                client.adressbuch(k.adressbuch)
            except CardDAVFehler as ex:
                self.status.setText(self.tr("Verbindung fehlgeschlagen: {fehler}").format(
                    fehler=ex))
                return False
            if not passwort_speichern(k, passwort):
                k.passwort_datei = passwort
        speichere_konfig(k)
        self.accept()
        return True
