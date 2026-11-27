# ruff: noqa: F821, B018
# Names vulture reads as unused although a framework calls them.  Vulture's own format: a
# bare name or ``_.attribute`` counts as a use.  Keep this list honest: a name here is a
# decision, not a way to silence a real finding.
_ = object()

# typer: every command is registered by its decorator
_main_command
database_status
load_extra_data
database_location
export_database
import_database
show_key
create_key
medical_terms_status
medical_terms_lists
tick_medical_terms
untick_medical_terms
add_medical_terms
remove_medical_terms
export_medical_terms
check_medical_terms
merge_medical_terms
import_medical_terms
identifiers_status
identifiers_lists
tick_identifiers
untick_identifiers
add_identifiers
remove_identifiers
export_identifiers
import_identifiers
check_identifiers
set_up
local_contacts
address_books
run_search
show_contact
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
isolated_data_folder
carddav_environment
lexicon_on
small_lexicon
without_variables
appauthor

# fields of ocrdoc's page text that PseudonyMed fills for a text file and ocrdoc reads
_.sizes
_.baselines

# dataclass fields, attributes of foreign objects (PyMuPDF widgets in a test), the *exc of __exit__
_.field_name
_.field_type
one_time_key
vcard
_.row_factory
_.max_length
exc
