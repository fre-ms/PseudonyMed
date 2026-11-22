"""PseudonyMed: local de-identification of patient documents.

Identifiers of a patient are replaced by stable pseudonyms so that the
document can be handed to a cloud language model; the mapping back to the
clear data never leaves the machine.
"""

__version__ = "0.1.0"
TOOL = f"PseudonyMed {__version__}"
