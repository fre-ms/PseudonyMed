"""Step 3: clean up scans and photos before OCR, checked by eye.

Only pages without a text layer appear here.  Each has an automatic suggestion
(tilt, sheet corners, resolution, background); the user switches steps on or
off, rotates, draws black masks on the result, compares before and after, and
then starts the analysis.
"""

from __future__ import annotations

from optdoc import cleanup as cleanup_module
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pseudonymed.document import optdoc_adapter
from pseudonymed.gui.session import Document, Session
from pseudonymed.gui.translation import interface_language
from pseudonymed.gui.views import PREVIEW_DPI, PageView


class CleanupPage(QWidget):
    """The clean-up panel inside the documents step."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.document: Document | None = None
        self.page = 0
        self._muted = False  # while the form is being filled from the data
        outer = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._build_tree(splitter)
        self._build_images(splitter)
        splitter.addWidget(self._build_controls())
        splitter.setSizes([180, 760, 300])
        outer.addWidget(splitter, 1)
        if not cleanup_module.available():
            self.note.setText(self.tr(
                "Bildaufbereitung braucht OpenCV und NumPy (Extra 'ocr'); hier sind nur"
                " Masken möglich."))

    # -- building the page -------------------------------------------------------------------------
    def _build_tree(self, splitter: QSplitter) -> None:
        """The tree of documents and their pages without a text layer, left of the views."""
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([self.tr("Dokument / Seite")])
        self.tree.currentItemChanged.connect(self._page_selected)
        splitter.addWidget(self.tree)

    def _build_images(self, splitter: QSplitter) -> None:
        """The before and after views side by side, linked in scroll and zoom.  Masks are
        drawn on the result only; Cmd/Ctrl+Z takes the last one back."""
        self.images = QSplitter(Qt.Orientation.Horizontal)  # the user moves the divider
        left_w = QWidget()
        left = QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 0, 0)
        before_title = QLabel(self.tr("Vorher"))
        before_title.setWordWrap(True)
        left.addWidget(before_title)
        self.before = PageView()
        left.addWidget(self.before, 1)
        right_w = QWidget()
        right_column = QVBoxLayout(right_w)
        right_column.setContentsMargins(0, 0, 0, 0)
        after_title = QLabel(self.tr("Nachher (so sieht es die OCR; Ziehen löscht den"
                                     " Bereich weiß, Cmd/Strg+Z holt ihn zurück;"
                                     " Umschalt/Option + Ziehen verschiebt beide Ansichten)"))
        after_title.setWordWrap(True)
        right_column.addWidget(after_title)
        self.after = PageView()
        self.after.mask_mode = True
        self.after.erase_mode = True
        self.after.box_drawn.connect(self.add_mask)
        right_column.addWidget(self.after, 1)
        self.before.mask_mode = True  # drag on the left: nothing drawn, pan with modifier
        self.before.box_drawn.connect(lambda _r: None)
        PageView.link(self.before, self.after)
        self.images.addWidget(left_w)
        self.images.addWidget(right_w)
        self.images.setSizes([400, 400])
        splitter.addWidget(self.images)
        undo = QShortcut(QKeySequence.StandardKey.Undo, self)
        undo.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        undo.activated.connect(self.undo)

    def _build_controls(self) -> QWidget:
        """The column of controls on the right: note, the two step groups, the page
        buttons and the zoom buttons."""
        right = QWidget()
        r = QVBoxLayout(right)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        r.addWidget(self.note)
        steps, form = self._build_steps()
        further = self._build_further_steps()
        self._build_rotate_buttons(form)
        self._build_mask_buttons(form)
        r.addWidget(steps)
        r.addWidget(further)
        self._connect_form()
        r.addLayout(self._build_page_buttons())
        r.addStretch(1)
        r.addLayout(self._build_zoom_buttons())
        return right

    def _build_steps(self) -> tuple[QGroupBox, QFormLayout]:
        """The group "steps for this page": tilt, sheet corners, upscaling, letter mode,
        background and black-and-white.  The form comes back as well, because the rotate
        and mask rows join it only after the sliders of the second group (the order in
        which widgets are created is the tab order)."""
        steps = QGroupBox(self.tr("Schritte für diese Seite"))
        form = QFormLayout(steps)
        self.deskewing = QCheckBox(self.tr("Schräglage begradigen"))
        self.angle = QDoubleSpinBox()
        self.angle.setRange(-15.0, 15.0)
        self.angle.setSingleStep(0.1)
        self.angle.setDecimals(1)
        self.angle.setSuffix("°")
        line = QHBoxLayout()
        line.addWidget(self.deskewing, 1)
        line.addWidget(self.angle)
        form.addRow(line)
        self.dewarping = QCheckBox(self.tr("Fotografiertes Blatt entzerren"))
        form.addRow(self.dewarping)
        self.upscaling = QComboBox()
        for factor, text in ((1, self.tr("nicht")), (2, "2×"), (3, "3×"), (4, "4×")):
            self.upscaling.addItem(text, factor)
        self.upscaling.setToolTip(self.tr(
            "Die OCR liest die Seite mit dem Vielfachen der Auflösung (bis 4×, Obergrenze"
            " 9000 Pixel); die Seite selbst wird nicht vergrößert gespeichert."))
        form.addRow(self.tr("Hochskalieren"), self.upscaling)
        self.text_mode = QCheckBox(self.tr("Buchstabenmodus: Lanczos-Vergrößerung, Striche"
                                           " geschärft"))
        self.text_mode.setToolTip(self.tr(
            "Vergrößert mit Lanczos und schärft die Striche in Graustufen. Eine harte"
            " Schwarz-Weiß-Schwelle verschlechterte die Erkennung in Messungen deutlich,"
            " deshalb bleibt das Bild grau. Ersetzt Hintergrund, Schärfen und Schwarz-Weiß."))
        form.addRow(self.text_mode)
        self.background_smoothing = QCheckBox(
            self.tr("Hintergrund glätten (Schatten, graues Papier)"))
        form.addRow(self.background_smoothing)
        self.black_and_white = QCheckBox(self.tr("Schwarz-Weiß"))
        form.addRow(self.black_and_white)
        return steps, form

    def _build_further_steps(self) -> QGroupBox:
        """The group "further steps": edges, coloured ink, the three steps with a
        strength slider, lines, and the button that detects the orientation."""
        further = QGroupBox(self.tr("Weitere Schritte"))
        wf = QFormLayout(further)
        self.border_cleaning = QCheckBox(self.tr("Dunkle Ränder säubern"))
        self.colour_removal = QCheckBox(self.tr("Farbige Tinte entfernen (Stempel, Marker)"))
        self.denoising = QCheckBox(self.tr("Entrauschen (Körnung, Staub)"))
        self.contrast_boost = QCheckBox(self.tr("Kontrast anheben (blasse Kopien)"))
        self.sharpening = QCheckBox(self.tr("Schärfen"))
        self.line_removal = QCheckBox(self.tr("Linien entfernen (Formulare, Tabellen)"))
        wf.addRow(self.border_cleaning)
        wf.addRow(self.colour_removal)
        self.slider: dict[str, QSlider] = {}
        for name, box in (("entrauschen", self.denoising), ("kontrast", self.contrast_boost),
                          ("schaerfen", self.sharpening)):
            slider = self._build_slider(box)
            row_ = QHBoxLayout()
            row_.addWidget(box, 1)
            row_.addWidget(slider)
            wf.addRow(row_)
            self.slider[name] = slider
        wf.addRow(self.line_removal)
        self.orientation_button = QPushButton(self.tr("Ausrichtung erkennen"))
        self.orientation_button.setToolTip(self.tr(
            "Hoch- oder Querformat aus den Textzeilen; ob der Text auf dem Kopf steht,"
            " entscheidet ein kurzer OCR-Vergleich (dauert einige Sekunden)."))
        self.orientation_button.clicked.connect(self.detect_orientation)
        wf.addRow(self.orientation_button)
        return further

    def _build_slider(self, box: QCheckBox) -> QSlider:
        """A strength slider 1 to 5 for the step of ``box``: enabled with the box,
        applied when the knob is released, not on every tick while it is dragged."""
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(1, 5)
        slider.setValue(2)
        slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        slider.setTickInterval(1)
        slider.setMaximumWidth(140)
        slider.setToolTip(self.tr("Stärke 1 (sanft) bis 5 (kräftig)"))
        slider.setEnabled(False)
        box.toggled.connect(slider.setEnabled)
        slider.valueChanged.connect(
            lambda _w: None if slider.isSliderDown() else self._apply_form())
        slider.sliderReleased.connect(self._apply_form)
        return slider

    def _build_rotate_buttons(self, form: QFormLayout) -> None:
        """The row of rotate buttons in the steps group."""
        rotate_row = QHBoxLayout()
        for text, degrees in ((self.tr("↺ 90°"), 270), (self.tr("↻ 90°"), 90),
                              (self.tr("180°"), 180)):
            k = QPushButton(text)
            k.setMinimumHeight(32)
            k.clicked.connect(lambda _c=False, g=degrees: self.rotate(g))
            rotate_row.addWidget(k)
        form.addRow(self.tr("Drehen"), rotate_row)

    def _build_mask_buttons(self, form: QFormLayout) -> None:
        """The row in the steps group that takes erased areas back: the last one or all."""
        mask_row = QHBoxLayout()
        self.undo_last = QPushButton(self.tr("Letzten Bereich zurückholen (Cmd/Strg+Z)"))
        self.undo_last.setMinimumHeight(32)
        self.undo_last.clicked.connect(self.undo)
        self.clear_masks_button = QPushButton(self.tr("Alle zurückholen"))
        self.clear_masks_button.setMinimumHeight(32)
        self.clear_masks_button.clicked.connect(self.clear_masks)
        mask_row.addWidget(self.undo_last, 1)
        mask_row.addWidget(self.clear_masks_button)
        form.addRow(mask_row)

    def _connect_form(self) -> None:
        """Every switch, the upscaling factor and the angle write the form back into
        the plan of the page."""
        for w in (self.deskewing, self.dewarping, self.text_mode, self.background_smoothing,
                  self.black_and_white, self.border_cleaning, self.colour_removal, self.denoising,
                  self.contrast_boost, self.sharpening, self.line_removal):
            w.toggled.connect(lambda _on: self._apply_form())
        self.upscaling.currentIndexChanged.connect(lambda _i: self._apply_form())
        self.angle.valueChanged.connect(lambda _w: self._apply_form())

    def _build_page_buttons(self) -> QVBoxLayout:
        """The buttons that act on the plan as a whole: restore the suggestion, switch
        the page off, copy the steps to every page."""
        buttons = QVBoxLayout()
        self.suggestion = QPushButton(self.tr("Vorschlag wiederherstellen"))
        self.suggestion.clicked.connect(self.restore_suggestion)
        self.nothing = QPushButton(self.tr("Diese Seite nicht aufbereiten"))
        self.nothing.clicked.connect(self.skip_cleanup)
        self.all_items = QPushButton(self.tr("Schritte auf alle Seiten übertragen"))
        self.all_items.clicked.connect(self.apply_to_all)
        for k in (self.suggestion, self.nothing, self.all_items):
            buttons.addWidget(k)
        return buttons

    def _build_zoom_buttons(self) -> QHBoxLayout:
        """The zoom buttons that act on both views at once."""
        zoom = QHBoxLayout()
        for text, action in (("+", lambda: self._zoom(1.25)), ("−", lambda: self._zoom(0.8)),
                             ("1:1", self._reset_zoom)):
            k = QPushButton(text)
            k.setMaximumWidth(44)
            k.setMinimumHeight(32)
            k.clicked.connect(action)
            zoom.addWidget(k)
        zoom.addStretch(1)
        return zoom

    # -- navigation ------------------------------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the tree of pages without a text layer and show the first of them.
        Without any, both views are emptied and the note says so."""
        self.tree.clear()
        for d in self.session.documents:
            if d.opened is None or not d.opened.candidates:
                continue
            node = QTreeWidgetItem([d.path.name])
            node.setData(0, Qt.ItemDataRole.UserRole, (id(d), -1))
            for n in d.opened.candidates:
                kind = QTreeWidgetItem([self.tr("Seite {nummer}").format(nummer=n + 1)])
                kind.setData(0, Qt.ItemDataRole.UserRole, (id(d), n))
                node.addChild(kind)
            self.tree.addTopLevelItem(node)
            node.setExpanded(True)
        if self._select_first_page():
            return
        self.document = None
        self.before.show_empty()
        self.after.show_empty()
        self.note.setText(self.tr(
            "Keine Seiten ohne Textebene: nichts aufzubereiten. Scans und Fotos erscheinen"
            " hier mit einem Vorschlag."))

    def _select_first_page(self) -> bool:
        """Make the first page of the first document current; False when the tree is empty."""
        first = self.tree.topLevelItem(0)
        if first is None:
            return False
        kind = first.child(0)
        self.tree.setCurrentItem(first if kind is None else kind)
        return True

    def _page_selected(self, current: QTreeWidgetItem | None,
                       _previous: QTreeWidgetItem | None = None) -> None:
        """Show the page behind the current tree item: fill the form, draw both views."""
        if current is None:
            return
        code, number = current.data(0, Qt.ItemDataRole.UserRole)
        d = next((d for d in self.session.documents if id(d) == code), None)
        if d is None or d.opened is None:
            return
        self.document = d
        self.page = max(number, 0)
        self._fill_form()
        self.draw()

    # -- data <-> form -----------------------------------------------------------------------------
    def current_plan(self) -> cleanup_module.CleanupPlan | None:
        """The plan of the current page, created from the suggestion on first access;
        None while no document is shown."""
        if self.document is None or self.document.opened is None:
            return None
        return self.document.cleanup.setdefault(
            self.page, _copy_plan(self.document.opened.suggestions.get(self.page)))

    def _fill_form(self) -> None:
        """Copy the plan of the current page into the switches, muted so that nothing
        is written back while the form is being filled."""
        a = self.current_plan()
        if a is None:
            return
        self._muted = True
        try:
            self.deskewing.setChecked(a.straighten)
            self.angle.setValue(a.angle)
            self.dewarping.setChecked(a.unwarp)
            self.dewarping.setEnabled(bool(a.corners))
            self.upscaling.setCurrentIndex(max(0, min(3, a.upscale - 1)))
            self.text_mode.setChecked(a.letter_mode)
            self.background_smoothing.setChecked(a.background)
            self.black_and_white.setChecked(a.black_white)
            self.border_cleaning.setChecked(a.borders)
            self.colour_removal.setChecked(a.remove_colour)
            self.denoising.setChecked(a.denoise)
            self.contrast_boost.setChecked(a.contrast)
            self.sharpening.setChecked(a.sharpen)
            self.line_removal.setChecked(a.remove_lines)
            self.slider["entrauschen"].setValue(a.denoise_strength)
            self.slider["kontrast"].setValue(a.contrast_strength)
            self.slider["schaerfen"].setValue(a.sharpen_strength)
        finally:
            self._muted = False

    def _apply_form(self) -> None:
        """Write the switches back into the plan of the current page and redraw."""
        if self._muted:
            return
        a = self.current_plan()
        if a is None:
            return
        a.straighten = self.deskewing.isChecked()
        a.angle = self.angle.value()
        a.unwarp = self.dewarping.isChecked() and bool(a.corners)
        a.upscale = int(self.upscaling.currentData() or 1)
        a.letter_mode = self.text_mode.isChecked()
        a.background = self.background_smoothing.isChecked()
        a.black_white = self.black_and_white.isChecked()
        a.borders = self.border_cleaning.isChecked()
        a.remove_colour = self.colour_removal.isChecked()
        a.denoise = self.denoising.isChecked()
        a.contrast = self.contrast_boost.isChecked()
        a.sharpen = self.sharpening.isChecked()
        a.remove_lines = self.line_removal.isChecked()
        a.denoise_strength = self.slider["entrauschen"].value()
        a.contrast_strength = self.slider["kontrast"].value()
        a.sharpen_strength = self.slider["schaerfen"].value()
        self.draw()

    # -- actions -----------------------------------------------------------------------------------
    def rotate(self, degrees: int) -> None:
        """Rotate the current page by ``degrees`` degrees; masks drawn on the old
        orientation are dropped."""
        a = self.current_plan()
        if a is None:
            return
        a.rotation = (a.rotation + degrees) % 360
        a.masks = []  # drawn on the old orientation
        self.draw()

    def add_mask(self, rect: tuple[float, float, float, float]) -> None:
        """Erase the area ``rect`` (points of the result page) on the current page."""
        a = self.current_plan()
        if a is None:
            return
        a.masks.append(rect)
        self.draw()

    def clear_masks(self) -> None:
        """Take back every erased area of the current page."""
        a = self.current_plan()
        if a is not None:
            a.masks = []
            self.draw()

    def undo(self) -> None:
        """Take back the last erased area of this page (Cmd/Ctrl+Z)."""
        a = self.current_plan()
        if a is not None and a.masks:
            a.masks.pop()
            self.draw()

    def detect_orientation(self) -> None:
        """Rotate the page upright: portrait/landscape from the text lines, upside-down
        decided by a quick OCR comparison when an engine is available."""
        a = self.current_plan()
        if a is None or self.document is None or self.document.opened is None:
            return
        try:
            backend = optdoc_adapter.backend(self.session.options.ocr)
        except Exception:  # noqa: BLE001 - no engine: lines only
            backend = None
        image, _scale = cleanup_module.render(self.document.opened.doc[self.page], 150)
        try:
            degrees = cleanup_module.estimate_orientation(image, backend)
        except Exception as e:  # noqa: BLE001
            self.note.setText(self.tr("Ausrichtung nicht erkannt: {fehler}").format(
                fehler=optdoc_adapter.message(e, interface_language())))
            return
        a.rotation = degrees
        a.masks = []
        self.draw()

    def restore_suggestion(self) -> None:
        """Replace the plan of the current page by a fresh copy of the suggestion."""
        if self.document is None or self.document.opened is None:
            return
        self.document.cleanup[self.page] = _copy_plan(
            self.document.opened.suggestions.get(self.page))
        self._fill_form()
        self.draw()

    def skip_cleanup(self) -> None:
        """Switch every step off for the current page: the OCR reads it as it is."""
        if self.document is None:
            return
        self.document.cleanup[self.page] = cleanup_module.CleanupPlan()
        self._fill_form()
        self.draw()

    def apply_to_all(self) -> None:
        """Copy the switches (not the page's own tilt, corners or masks) to every
        candidate page of every document."""
        source = self.current_plan()
        if source is None:
            return
        for d in self.session.documents:
            if d.opened is None:
                continue
            for n in d.opened.candidates:
                target = d.cleanup.setdefault(n, _copy_plan(d.opened.suggestions.get(n)))
                target.straighten = source.straighten and abs(target.angle) >= 0.05
                target.unwarp = source.unwarp and bool(target.corners)
                for field_name in ("upscale", "letter_mode", "background", "black_white",
                                   "borders", "remove_colour", "denoise", "contrast", "sharpen",
                                   "remove_lines"):
                    setattr(target, field_name, getattr(source, field_name))
        self.draw()

    # -- drawing -----------------------------------------------------------------------------------
    def draw(self) -> None:
        """Render the current page before and after the plan and describe the steps in
        the note; a failing step is reported there instead of crashing."""
        a = self.current_plan()
        if a is None or self.document is None or self.document.opened is None:
            return
        opened = self.document.opened
        self.before.scale_factor = PREVIEW_DPI / 72.0
        self.before.display(opened.page_image(self.page, PREVIEW_DPI), [], None)
        try:
            png, _scale = opened.preview(self.page, a, PREVIEW_DPI)
        except Exception as e:  # noqa: BLE001 - shown instead of crashing the step
            self.note.setText(self.tr("Aufbereitung fehlgeschlagen: {fehler}").format(
                fehler=optdoc_adapter.message(e, interface_language())))
            return
        image = QImage.fromData(png)
        self.after.scale_factor = image.width() / opened.page_width(self.page, a)
        # an enlarged image is shown at the size of the original; zooming shows the gain
        self.after.set_base((PREVIEW_DPI / 72.0) / self.after.scale_factor)
        self.after.display(png, [], None)
        text = optdoc_adapter.description(a, interface_language())
        if a.corners is None and not opened.photo:
            text += " · " + self.tr("kein fotografiertes Blatt erkannt")
        self.note.setText(text)

    def _zoom(self, factor: float) -> None:
        """Zoom both views by ``factor``."""
        self.before.zoom(factor)
        self.after.zoom(factor)

    def _reset_zoom(self) -> None:
        """Show both views at 1:1 again."""
        self.before.reset_zoom()
        self.after.reset_zoom()


def _copy_plan(a: cleanup_module.CleanupPlan | None) -> cleanup_module.CleanupPlan:
    """An independent copy of a plan (own mask and corner lists); an empty plan for None."""
    if a is None:
        return cleanup_module.CleanupPlan()
    return cleanup_module.CleanupPlan(**{**a.__dict__, "masks": list(a.masks),
                                    "corners": list(a.corners) if a.corners else None})
