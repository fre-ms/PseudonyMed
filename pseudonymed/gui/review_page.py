"""Step 7: look at every page, drop false hits, draw boxes over what was missed."""

from __future__ import annotations

from pathlib import Path

from optdoc.pagetext import Box
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pseudonymed import working_state as state_mod
from pseudonymed.core.model import Hit
from pseudonymed.gui.session import Document, Session
from pseudonymed.gui.translation import shown, shown_error

# Traffic light: red = will be redacted, yellow = uncertain (read badly, blanked as a
# precaution), green = stays (medical term, discarded hit).  The source of a hit is in
# the table; guesses (NER, fuzzy search, identifiers) get a dashed outline.
from pseudonymed.gui.views import (
    PREVIEW_DPI,
    PageView,
    TextView,
)
from pseudonymed.pipeline import Preparation, TextPreparation


class ReviewPage(QWidget):
    """Step 7, the visual review: the tree of documents and pages on the left, the page or
    the text in the middle, and on the right the table of hits with the controls to
    discard, take up, box and mask, and the button that saves the working state."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        """Build the three regions of the splitter; nothing is shown until
        :meth:`refresh` fills the tree."""
        super().__init__(parent)
        self.session = session
        self.document: Document | None = None
        self.page = 0
        self.selected: Hit | None = None
        self.selection: tuple[int, int] | None = None  # offsets of words picked on the page
        self.synchronous = False

        outer = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._build_tree(splitter)
        self._build_views(splitter)
        splitter.addWidget(self._build_right())
        splitter.setSizes([180, 620, 360])
        outer.addWidget(splitter, 1)

    # -- building ----------------------------------------------------------------------------------
    def _build_tree(self, splitter: QSplitter) -> None:
        """Left: the tree of documents and their pages."""
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([self.tr("Dokument / Seite")])
        self.tree.currentItemChanged.connect(self._page_selected)
        splitter.addWidget(self.tree)

    def _build_views(self, splitter: QSplitter) -> None:
        """Middle: the page view and the text view, stacked; one of them is in front."""
        self.view = PageView()
        self.view.match_selected.connect(self.select_hit)
        self.view.box_drawn.connect(self.box)
        self.view.point_selected.connect(self.point)
        self.text_view = TextView()
        self.text_view.match_selected.connect(self.select_hit)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.text_view)
        splitter.addWidget(self.stack)

    def _build_right(self) -> QWidget:
        """Right: the legend, the table of hits, the options, the replacement text, the
        buttons, the hint line and the working-state button."""
        right = QWidget()
        r = QVBoxLayout(right)
        self._build_legend(r)
        self._build_table(r)
        self._build_options(r)
        self._build_buttons(r)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        r.addWidget(self.note)
        self._build_save(r)
        return right

    def _build_legend(self, r: QVBoxLayout) -> None:
        """The legend: what the colours mean and how mouse and keys work."""
        legend = QLabel(self.tr(
            "Rot: wird geschwärzt (gestrichelt: Vorschlag von NER oder unscharfer Suche) ·"
            " Gelb: unsicher gelesen (unlesbare Zeilen werden vorsorglich geschwärzt) ·"
            " Grün: bleibt stehen (Fachbegriff, verworfener Treffer; Klick stellt ihn wieder"
            " her). Klick auf ein Wort wählt es (blau), Strg/Cmd + Klick erweitert die Auswahl"
            " bis dorthin, „Auswahl aufnehmen“ macht daraus einen Treffer; so kommen auch"
            " Fachbegriffe oder Wörter in die Liste, die die Analyse nicht gefunden hat."
            " Ziehen zeichnet einen Kasten mit dem Ersatztext aus dem Feld, mit „Maske"
            " zeichnen“ eine schwarze Maske; Umschalt + Ziehen verschiebt den Ausschnitt,"
            " Strg/Cmd + Rad zoomt. In Textdateien nimmt „Auswahl aufnehmen“ den"
            " ausgewählten Text auf. Die Quelle jedes Treffers steht in der Tabelle."))
        legend.setWordWrap(True)
        r.addWidget(legend)

    def _build_table(self, r: QVBoxLayout) -> None:
        """The table of hits: page, rule, pseudonym, source; a row selects its hit."""
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([self.tr("Seite"), self.tr("Regel"),
                                              self.tr("Pseudonym"), self.tr("Quelle")])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._row_selected)
        r.addWidget(self.table, 1)

    def _build_options(self, r: QVBoxLayout) -> None:
        """The two options (show medical terms, remember discards) and the field with the
        replacement text for boxes and take-ups."""
        self.show_medical_terms = QCheckBox(self.tr("Fachbegriffe markieren"))
        self.show_medical_terms.setChecked(True)
        self.show_medical_terms.toggled.connect(lambda _on: self.draw())
        r.addWidget(self.show_medical_terms)
        self.remember = QCheckBox(self.tr(
            "Verworfene Treffer für diesen Patienten merken (beim Export gespeichert)"))
        self.remember.setChecked(True)
        self.session.options.remember_discarded = True
        self.remember.toggled.connect(self._apply_remember)
        r.addWidget(self.remember)
        replacement = QHBoxLayout()
        replacement.addWidget(QLabel(self.tr("Ersatztext für Kästen und Aufnahmen")))
        self.replacement_text = QLineEdit(self.tr("[geschwärzt]"))
        self.replacement_text.setToolTip(self.tr(
            "Für Kästen der Text, der hineingeschrieben wird. Für aufgenommene Wörter gilt:"
            " bleibt hier „[geschwärzt]“ stehen, bekommt das Wort einen eigenen Code"
            " ([Begriff …]), den die Reidentifikation zurücksetzt."))
        replacement.addWidget(self.replacement_text, 1)
        r.addLayout(replacement)

    def _build_buttons(self, r: QVBoxLayout) -> None:
        """The row of buttons: discard, take up, mask mode and the three zoom buttons."""
        buttons = QHBoxLayout()
        self.discard_button = QPushButton(self.tr("Treffer verwerfen"))
        self.discard_button.clicked.connect(self.discard)
        buttons.addWidget(self.discard_button)
        self.mark_button = QPushButton(self.tr("Auswahl aufnehmen"))
        self.mark_button.setToolTip(self.tr(
            "Die gewählten Wörter (Seite) oder den markierten Text (Textdatei) in die"
            " Trefferliste aufnehmen."))
        self.mark_button.clicked.connect(lambda: self.take_up())
        buttons.addWidget(self.mark_button)
        self.mask_mode_button = QPushButton(self.tr("Maske zeichnen"))
        self.mask_mode_button.setCheckable(True)
        self.mask_mode_button.setToolTip(self.tr(
            "Eingeschaltet: Ziehen schwärzt den Bereich schwarz, ohne Ersatztext;"
            " Text und Bild darunter verschwinden."))
        self.mask_mode_button.toggled.connect(self._toggle_mask)
        buttons.addWidget(self.mask_mode_button)
        for text, action in (("+", lambda: self._active_view().zoom(1.25)),
                             ("−", lambda: self._active_view().zoom(0.8)),
                             ("1:1", lambda: self._active_view().reset_zoom())):
            k = QPushButton(text)
            k.setMaximumWidth(40)
            k.clicked.connect(action)
            buttons.addWidget(k)
        r.addLayout(buttons)

    def _build_save(self, r: QVBoxLayout) -> None:
        """The button that writes the working state."""
        self.save = QPushButton(self.tr("Arbeitsstand speichern …"))
        self.save.setToolTip(self.tr(
            "Das aufbereitete Dokument und eine Konfigurationsdatei schreiben, aus denen sich"
            " die Sichtprüfung später wieder aufnehmen und abwandeln lässt (Aufbereitung,"
            " Texterkennung, Treffer, Entscheidungen). Beide Dateien enthalten Klardaten."))
        self.save.clicked.connect(self.choose_working_state)
        r.addWidget(self.save)

    # -- navigation -------------------------------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the tree from the session's analysed, not yet exported documents and go
        to the first page."""
        self.tree.clear()
        for d in self.session.documents:
            if d.preparation is None or d.preparation.finalised:
                continue
            node = self._tree_node(d, d.preparation)
            self.tree.addTopLevelItem(node)
            node.setExpanded(True)
        first = self.tree.topLevelItem(0)
        if first is not None:
            kind = first.child(0)
            self.tree.setCurrentItem(kind if kind is not None else first)

    def _tree_node(self, d: Document, v: Preparation | TextPreparation) -> QTreeWidgetItem:
        """The node of one document with a child per page: "Text" for a text file, else the
        page number with an OCR mark; the user-role data names document and page."""
        node = QTreeWidgetItem([d.path.name])  # file name, not translated
        node.setData(0, Qt.ItemDataRole.UserRole, (id(d), -1))
        for s in v.pages:
            if v.is_text:
                kind = QTreeWidgetItem([self.tr("Text")])
            else:
                kind = QTreeWidgetItem([self.tr("Seite {nummer}").format(nummer=s.number + 1)
                                        + (" (OCR)" if s.ocr is not None else "")])
            kind.setData(0, Qt.ItemDataRole.UserRole, (id(d), s.number))
            node.addChild(kind)
        return node

    def _page_selected(self, current: QTreeWidgetItem | None, _previous=None) -> None:
        """The tree's current item changed: show that document and page, nothing selected."""
        if current is None:
            return
        code, number = current.data(0, Qt.ItemDataRole.UserRole)
        d = next((d for d in self.session.documents if id(d) == code), None)
        if d is None or d.preparation is None:
            return
        self.document = d
        self.page = max(number, 0)
        self.selected = None
        self.selection = None
        self.draw()

    def _preparation(self) -> Preparation | TextPreparation | None:
        """The preparation under review, or None before a document was chosen."""
        return None if self.document is None else self.document.preparation

    def _page_preparation(self) -> Preparation | None:
        """The preparation under review when it has pages to click into; None for a text
        file or before a document was chosen."""
        v = self._preparation()
        return v if isinstance(v, Preparation) else None

    def hits(self) -> list[Hit]:
        """The hits of the page under review (all of them for a text file)."""
        v = self._preparation()
        if v is None:
            return []
        return [f for f in v.hits if f.page == self.page]

    def _active_view(self) -> PageView | TextView:
        """The view in front: the text view for a text file, else the page view."""
        return (self.text_view if self.stack.currentWidget() is self.text_view
                else self.view)

    # -- drawing -----------------------------------------------------------------------------------
    def _show_view(self, matches: list[Hit]) -> None:
        """Show the hits in the view that fits the document and adapt the buttons to it."""
        v = self._preparation()
        if v is None:
            return
        discarded = [f for f in v.discarded if f.page == self.page]
        if isinstance(v, TextPreparation):
            self._show_text(v, matches, discarded)
        else:
            self._show_page(v, matches, discarded)
        self._adjust_buttons(v)

    def _show_text(self, v: TextPreparation, matches: list[Hit],
                   discarded: list[Hit]) -> None:
        """Text file: the text view in front, medical terms as spans when wanted."""
        terms = v.medical_term_spans(self.page) if self.show_medical_terms.isChecked() else []
        self.text_view.display(v.text, matches, self.selected, terms, discarded)
        self.stack.setCurrentWidget(self.text_view)

    def _show_page(self, v: Preparation, matches: list[Hit],
                   discarded: list[Hit]) -> None:
        """Page document: the page image with every kind of mark, the page view in front."""
        terms = v.medical_term_rects(self.page) if self.show_medical_terms.isChecked() else []
        selection = self._selection_rects(v)
        self.view.display(v.page_image(self.page, PREVIEW_DPI), matches, self.selected,
                          terms, discarded, v.uncertain_rects(self.page), selection)
        self.stack.setCurrentWidget(self.view)

    def _selection_rects(self, v: Preparation) -> list[Box]:
        """Boxes of the words the user picked on the page, one per line; empty without a
        selection."""
        if self.selection is None:
            return []
        return [tuple(r) for r in v.pages[self.page].text_base.rects(*self.selection)]

    def _adjust_buttons(self, v: Preparation | TextPreparation) -> None:
        """The buttons follow the document: a take-up needs a selection on a page, masks
        exist only on pages, and the discard button turns into restore for a discarded hit."""
        self.mark_button.setEnabled(v.is_text or self.selection is not None)
        self.mask_mode_button.setVisible(not v.is_text)
        discarded_selected = self.selected is not None and self.selected in v.discarded
        self.discard_button.setText(self.tr("Treffer wiederherstellen") if discarded_selected
                                    else self.tr("Treffer verwerfen"))

    def draw(self) -> None:
        """Redraw the page under review: the view, the table of hits and the hint line."""
        v = self._preparation()
        if v is None:
            return
        matches = self.hits()
        self._show_view(matches)
        self._fill_table(matches)
        self._show_note(v)

    def _fill_table(self, matches: list[Hit]) -> None:
        """One row per hit (page, rule, pseudonym, source); the selected hit's row is
        selected without firing the selection signal."""
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for f in matches:
            line = self.table.rowCount()
            self.table.insertRow(line)
            for column, value in enumerate((str(f.page + 1), f.rule, f.pseudonym, f.source)):
                self.table.setItem(line, column, QTableWidgetItem(value))
            if f is self.selected:
                self.table.selectRow(line)
        self.table.blockSignals(False)

    def _show_note(self, v: Preparation | TextPreparation) -> None:
        """The hint line: how many medical terms are marked, and the first note about OCR,
        the text layer or the fuzzy search."""
        warnings = [w for w in v.warnings + v.notes
                    if "OCR" in w or "Textebene" in w or w.startswith("Unscharfe Suche")]
        note = self.tr("{n} Fachbegriffe markiert").format(n=v.medical_term_count)
        self.note.setText(" · ".join([note] + [shown(w) for w in warnings[:1]]))

    # -- actions ---------------------------------------------------------------------------------
    def select_hit(self, f: Hit) -> None:
        """Select a hit (a click in the view) and redraw."""
        self.selected = f
        self.draw()

    def _row_selected(self) -> None:
        """A row of the table was selected: select that hit in the view."""
        lines = self.table.selectionModel().selectedRows()
        if lines:
            matches = self.hits()
            i = lines[0].row()
            if 0 <= i < len(matches):
                self.selected = matches[i]
                self._show_view(matches)

    def discard(self) -> None:
        """Discard the selected hit, or take a discarded one back."""
        v = self._preparation()
        if self.selected is None or v is None:
            return
        if self.selected in v.discarded:
            v.restore(self.selected)
        else:
            v.discard(self.selected)
        self.selected = None
        self.draw()

    def _toggle_mask(self, on: bool) -> None:
        """The mask button: dragging on the page draws a mask now (or a box again)."""
        self.view.mask_mode = on

    def _apply_remember(self, on: bool) -> None:
        """The remember-discards option goes straight into the session's options."""
        self.session.options.remember_discarded = on

    def box(self, rect: Box, pseudonym: str | None = None) -> None:
        """A drawn rectangle: a black mask in mask mode, otherwise a box with the
        replacement text from the field (no dialog, so nothing can get lost behind
        the window).  The new hit is selected; errors are shown in the hint line.
        Nothing happens for a text file, which has no page to draw on."""
        v = self._page_preparation()
        if v is None:
            return
        try:
            if pseudonym is None and self.mask_mode_button.isChecked():
                added = v.add_mask(self.page, rect)
            else:
                text = pseudonym if pseudonym is not None else self.replacement_text.text().strip()
                added = v.box(self.page, rect, text or self.tr("[geschwärzt]"))
        except Exception as e:  # noqa: BLE001 - must be visible, never silent
            self.note.setText(self.tr("Kasten nicht gesetzt: {fehler}").format(
                fehler=shown_error(e)))
            return
        self.selected = added[0] if added else None
        self.draw()
        if added:
            self.note.setText(self.tr("{n} Kasten gesetzt: {pseudonym}").format(
                n=len(added), pseudonym=added[0].pseudonym) + " · " + self.note.text())

    def point(self, x: float, y: float, extend: bool = False) -> None:
        """A click on the page: select the word there, or extend the selection to it."""
        v = self._page_preparation()
        if v is None:
            return
        span = v.word_at(self.page, x, y)
        if span is None:
            self.selection = None
        elif extend and self.selection is not None:
            self.selection = (min(self.selection[0], span[0]), max(self.selection[1], span[1]))
        else:
            self.selection = span
        self.selected = None
        self.draw()

    def _replacement(self, pseudonym: str | None) -> str | None:
        """The replacement for a take-up: the argument, the field, or None for a code
        when the field still holds the default."""
        if pseudonym is not None:
            return pseudonym
        text = self.replacement_text.text().strip()
        return None if text in ("", "[geschwärzt]", self.tr("[geschwärzt]")) else text

    def take_up(self, pseudonym: str | None = None) -> None:
        """Take the picked words (page) or the selected text (text file) into the list."""
        v = self._preparation()
        if v is None:
            return
        span = self.text_view.selection() if v.is_text else self.selection
        if span is None:
            if self.synchronous:
                raise ValueError("Keine Auswahl.")
            self.note.setText(self.tr("Erst ein Wort anklicken oder Text markieren."))
            return
        try:
            f = v.take_up(self.page, span[0], span[1], self._replacement(pseudonym))
        except ValueError as e:
            self.note.setText(shown_error(e))
            return
        self.selection = None
        self.selected = f
        self.draw()
        self.note.setText(self.tr("Aufgenommen: {pseudonym}").format(pseudonym=f.pseudonym)
                          + " · " + self.note.text())

    def mark_selection(self, pseudonym: str | None = None) -> None:
        """Text files: the selected text becomes a manual hit with the given replacement
        (kept for scripts; the button calls :meth:`take_up`)."""
        v = self._preparation()
        if not isinstance(v, TextPreparation):
            return
        span = self.text_view.selection()
        if span is None:
            if self.synchronous:
                raise ValueError("Keine Auswahl.")
            return
        v.mark(self.page, span[0], span[1], pseudonym or self.tr("[geschwärzt]"))
        self.selected = None
        self.draw()

    # -- working state -----------------------------------------------------------------------------
    def choose_working_state(self) -> None:
        """Ask where to put the working state (next to the source by default) and save it."""
        v = self._preparation()
        if v is None:
            return
        suggestion = v.source.with_name(v.source.stem + state_mod.EXTENSION)
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Arbeitsstand speichern"), str(suggestion),
            self.tr("Arbeitsstand (*.arbeitsstand.json)"))
        if path:
            self.save_working_state(Path(path))

    def save_working_state(self, path: Path) -> tuple[Path, Path] | None:
        """Write document and configuration; returns the two paths."""
        v = self._preparation()
        if v is None:
            return None
        try:
            document, configuration = state_mod.save(v, path)
        except (state_mod.WorkingStateError, OSError) as e:
            if self.synchronous:
                raise
            QMessageBox.warning(self, self.tr("Arbeitsstand"), shown_error(e))
            return None
        self.note.setText(self.tr(
            "Arbeitsstand gespeichert: {dokument} und {konfiguration} (beide mit Klardaten,"
            " wie das Original aufbewahren).").format(dokument=document.name,
                                                     konfiguration=configuration.name))
        return document, configuration
