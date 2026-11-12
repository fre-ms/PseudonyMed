# ruff: noqa: F821, B018
# Names vulture reads as unused although a framework calls them.  Vulture's own format: a
# bare name or ``_.attribute`` counts as a use.  Keep this list honest: a name here is a
# decision, not a way to silence a real finding.
_ = object()

# typer: every command is registered by its decorator
_hauptbefehl
datenbank_status
datenbank_zusatzdaten
datenbank_speicherort
datenbank_exportieren
datenbank_importieren
schluessel_zeigen
schluessel_erzeugen
fachbegriffe_status
fachbegriffe_hinzufuegen
fachbegriffe_entfernen
fachbegriffe_exportieren
fachbegriffe_pruefen
fachbegriffe_ergaenzen
fachbegriffe_importieren
identifikatoren_status
identifikatoren_hinzufuegen
identifikatoren_entfernen
identifikatoren_exportieren
identifikatoren_importieren
identifikatoren_pruefen
einrichten
kontakte_lokal
adressbuecher
suchen
zeigen
export

# Qt virtuals and model methods
_.dragEnterEvent
_.dragMoveEvent
_.dropEvent
_.columnCount
_.headerData

# html.parser and http.server hooks
_.handle_starttag
_.handle_endtag
_.handle_data
attrs
_.do_PROPFIND
_.do_REPORT
_.do_GET
_.log_message
protocol_version

# pytest hooks and fixtures (used through their parameter names)
pytest_configure
pytest_unconfigure
config
pytestmark
datenordner_isoliert
carddav_umgebung
lexikon_an
lexikon_klein
ohne_variablen
appauthor

# dataclass fields, attributes of foreign objects (PyMuPDF widgets in a test), the *exc of __exit__
_.field_name
_.field_type
einmalschluessel
vcard
_.row_factory
_.max_length
exc
