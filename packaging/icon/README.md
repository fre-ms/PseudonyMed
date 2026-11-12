# Icon

`pseudonymed.svg` is the app icon (chosen on 12 November 2026): a page in which one line is
replaced by a pseudonym, the six characters of a code such as `K7Q4MX`, on a petrol tile with
mint as the accent. `pseudonymed_marke.svg` is the same mark without the tile, drawn in
`currentColor` for web pages and documents. Both are plain SVG without text, gradients or
filters.

`entwuerfe/` keeps the two other proposals of the same day, with their marks: `b_klammer`
(the square brackets of the pseudonyms around the P of the name) and `c_schluessel` (a page
with a redacted line and the key that stays with the physician).

`python packaging/icon.py` renders the chosen SVG into the sizes the programs need:
`pseudonymed/gui/icon/pseudonymed_<n>.png` for the window and the Linux desktop,
`pseudonymed.ico` for Windows and, on macOS, `pseudonymed.icns` through `iconutil`. The
rendered files are committed, so a build needs no extra tool; run the script again after
changing the SVG.
