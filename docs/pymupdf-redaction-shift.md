# PyMuPDF 2.0: glyphs shift after a text redaction

Observed on 30 October 2026 with PyMuPDF 2.0 (MuPDF 1.28.5), macOS arm64, Python 3.13.

## Symptom

After `Page.apply_redactions()` removes a word from the middle of a text run, the
remaining characters to the right of the gap move left. The offset equals the
advance width of the *first* removed glyph: for "Herrn Flantz, geb." with
"Flantz" redacted, the comma moved 6.4 pt (Helvetica "F" at 10.5 pt), with a
Typst document 5.3 pt (Libertinus "F" at 11 pt). The rewritten content stream
shows the adjustment: `[(Herrn )-2111.9996(, geb. ...)]TJ` where 2724 would be
correct for all six glyphs.

Documents whose producers emit one `Tj` per run (LibreOffice, macOS CUPS) are
not affected because MuPDF splits them with an explicit `TD`.

## Impact

A fill box or pseudonym placed at the original position hides the shifted
neighbours; without a box they overlap the pseudonym. The text layer still
contains the neighbours, so a text-only check does not notice.

## Versions tested

| PyMuPDF | MuPDF  | shift |
|---------|--------|-------|
| 1.24.14 | 1.24.11 | none |
| 1.25.5  | 1.25.6 | none |
| 1.26.4  | 1.26.7 | none |
| 1.27.1  | 1.27.1 | none |
| 1.28.2  | 1.28.2 | none |
| 2.0     | 1.28.5 | **yes** |

## Decision

* `pyproject.toml` pins `pymupdf>=1.25,<2`.
* `pseudonymed.redaction.pdf.layout_intact()` compares the position of every
  character outside the hits before and after the redaction. If anything moved,
  `rasterise_page()` replaces the page by an image of the original page with the
  hits painted over, and the report warns. This keeps the output safe even if a
  future release reintroduces the problem.

Re-test with each new PyMuPDF release (`tests/test_redaction.py` covers it)
and lift the pin once a 2.x release positions correctly.
