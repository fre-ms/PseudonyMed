"""Pseudonym codes: derivation rules that the detection must never trip over."""

from __future__ import annotations


def test_code_never_starts_with_az() -> None:
    """A code that starts with "AZ" would be taken for a file number by the label rule
    (and by the re-check on a scan); the derivation skips such codes deterministically."""
    from pseudonymed.pseudonyms.patient_code import hash_code, is_valid_code

    key = bytes(range(32))
    codes = [hash_code(key, f"ort:test{i}") for i in range(3000)]
    assert not any(c.startswith("AZ") for c in codes)
    assert all(is_valid_code(c) for c in codes) and len(set(codes)) > 2990
    assert hash_code(key, "ort:test7") == codes[7]  # still deterministic
