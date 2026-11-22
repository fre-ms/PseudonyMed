"""Where a patient's identifiers come from: manual files, local vCards, CardDAV."""

from pseudonymed.sources.manual import load_patient, template
from pseudonymed.sources.vcf import load_vcf, parse_vcards, patient_from_vcard, patients_from_vcf

__all__ = [
    "load_patient",
    "load_vcf",
    "parse_vcards",
    "patient_from_vcard",
    "patients_from_vcf",
    "template",
]
