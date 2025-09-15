import os
import sys
import tempfile
import subprocess
from dataclasses import dataclass
from typing import Optional, List

from PySide6.QtCore import Qt, QSize, QPoint, QRect
from PySide6.QtGui import QIcon, QPixmap, QAction, QColor, QPainter, QPen, QImage, QBrush
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QFileDialog, QPushButton, QFrame, QMessageBox,
    QStackedWidget, QMenu, QAbstractItemView, QGraphicsDropShadowEffect, QAbstractItemView
)

# Try to import PyMuPDF for thumbnails (optional)
try:
    import fitz  # PyMuPDF
    HAVE_FITZ = True
except Exception:
    HAVE_FITZ = False


@dataclass
class PageRef:
    src_path: str   # absolute path to source PDF
    page_index: int # 0-based index


def info_box(title: str, text: str, parent=None):
    QMessageBox.information(parent, title, text)


def error_box(title: str, text: str, parent=None):
    QMessageBox.critical(parent, title, text)


def command_exists(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


class TrashBox(QFrame):
    """A drop area that discards pages dragged from the PageList."""
    def __init__(self, on_pages_dropped, parent=None):
        super().__init__(parent)
        self.on_pages_dropped = on_pages_dropped
        self.setAcceptDrops(True)
        self.setObjectName("TrashBox")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(8)

        self.label = QLabel("Drop here to discard")
        self.label.setAlignment(Qt.AlignCenter)
        lay.addStretch(1)
        lay.addWidget(self.label, 0, Qt.AlignCenter)
        lay.addStretch(1)

        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(QSize(260, 120))  # a bit smaller than the small DropBox

    def dragEnterEvent(self, e):
        # Accept drags coming from our PageList (internal drags)
        src = e.source()
        if isinstance(src, QListWidget):
            e.acceptProposedAction()
        else:
            # If it’s not from the list (e.g., external files), ignore
            e.ignore()

    def dropEvent(self, e):
        src = e.source()
        if isinstance(src, QListWidget):
            # Let the main window decide what to do (usually: remove selected items)
            self.on_pages_dropped(src)
            e.acceptProposedAction()
        else:
            e.ignore()


class DropBox(QFrame):
    """Large or small drop area to add PDFs."""
    def __init__(self, on_files_dropped, small=False, parent=None):
        super().__init__(parent)
        self.on_files_dropped = on_files_dropped
        self.setAcceptDrops(True)
        self.setObjectName("DropBox")
        self.setProperty("small", small)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(8)

        self.label = QLabel("Drop PDF files here\n(or click to choose)")
        self.label.setAlignment(Qt.AlignCenter)
        lay.addStretch(1)
        lay.addWidget(self.label, 0, Qt.AlignCenter)
        lay.addStretch(1)

        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(QSize(300, 180) if small else QSize(520, 320))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            files, _ = QFileDialog.getOpenFileNames(self, "Choose PDF files", "", "PDF Files (*.pdf)")
            if files:
                self.on_files_dropped(files)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            for u in e.mimeData().urls():
                if u.toLocalFile().lower().endswith(".pdf"):
                    e.acceptProposedAction()
                    return
        e.ignore()

    def dropEvent(self, e):
        paths = []
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if p.lower().endswith(".pdf") and os.path.exists(p):
                paths.append(p)
        if paths:
            self.on_files_dropped(paths)
            e.acceptProposedAction()
        else:
            e.ignore()


class DropMarker(QWidget):
    """Painted vertical bar shown on the list viewport, above items."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DropMarker")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._color = QColor("#2DAA36")  # vivid kiwi green
        self._width = 4
        self.hide()

    def show_at(self, rect: QRect, at_left: bool):
        # rect is in viewport coordinates
        x = rect.left() if at_left else rect.right()
        self.setGeometry(x - self._width // 2, rect.top(), self._width, rect.height())
        self.raise_()   # ensure on top
        self.show()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(self._color)
        pen.setWidth(self._width)
        p.setPen(pen)
        # draw a vertical line centered in our small widget
        x = self.width() // 2
        p.drawLine(x, 0, x, self.height())



class PageList(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.IconMode)
        self.setResizeMode(QListWidget.Adjust)
        self.setFlow(QListWidget.LeftToRight)
        self.setWrapping(True)
        self.setSpacing(10)
        self.setIconSize(QSize(120, 168))
        self.setUniformItemSizes(True)
        self.setWordWrap(True)

        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(False)  # we render our own

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_context_menu)

        # Marker is on viewport so coordinates match visualItemRect()
        self._marker = DropMarker(self.viewport())
        self._drop_index = None

    # --- context & delete
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Delete:
            self.remove_selected()
        else:
            super().keyPressEvent(e)

    def open_context_menu(self, pos: QPoint):
        menu = QMenu(self)
        act_remove = QAction("Remove selected", self)
        act_remove.triggered.connect(self.remove_selected)
        menu.addAction(act_remove)
        menu.exec(self.mapToGlobal(pos))

    def remove_selected(self):
        rows = sorted({i.row() for i in self.selectedIndexes()}, reverse=True)
        for r in rows:
            it = self.takeItem(r)
            del it

    # --- DnD with marker
    def dragEnterEvent(self, e):
        e.acceptProposedAction()

    def dragMoveEvent(self, e):
        pos = e.position().toPoint()
        idx, rect, before = self._compute_drop_position(pos)
        if rect is not None:
            self._marker.show_at(rect, at_left=before)
            self._drop_index = idx if before else idx + 1
            e.acceptProposedAction()
        else:
            self._marker.hide()
            self._drop_index = None
            e.ignore()

    def dropEvent(self, e):
        if self._drop_index is None:
            self._marker.hide()
            return super().dropEvent(e)

        selected_rows = sorted({i.row() for i in self.selectedIndexes()})
        if not selected_rows:
            self._marker.hide()
            return super().dropEvent(e)

        # Take items preserving on-screen order
        items = [self.takeItem(r - k) for k, r in enumerate(selected_rows)]

        insert_at = self._drop_index
        removed_before = sum(1 for r in selected_rows if r < insert_at)
        insert_at -= removed_before

        for i, it in enumerate(items):
            self.insertItem(insert_at + i, it)

        self._marker.hide()
        self._drop_index = None
        e.acceptProposedAction()

    def leaveEvent(self, _):
        self._marker.hide()
        self._drop_index = None

    def _compute_drop_position(self, pos: QPoint):
        """
        Center-based insertion logic for IconMode.

        Rule:
          - While hovering over a row, each item's vertical CENTER (x) is the decision boundary.
          - If cursor.x() < center(item i)  -> insert BEFORE item i.
          - Else keep walking; if no center is to the right of the cursor, insert AFTER the last item in that row.

        Returns (index, rect, before) where:
          - index: target item index
          - rect: visual rect of that item (viewport coords)
          - before: True -> marker at LEFT of that item; False -> marker at RIGHT of that item
        """
        count = self.count()
        if count == 0:
            return 0, QRect(12, 12, 40, 80), True

        # Find the row under the cursor (with a small vertical tolerance)
        tol_y = max(6, self.spacing())
        row = []
        for i in range(count):
            it = self.item(i)
            r = self.visualItemRect(it)
            if r.top() - tol_y <= pos.y() <= r.bottom() + tol_y:
                row.append((i, r))

        # If the cursor is above first row or below last row, clamp
        if not row:
            first_rect = self.visualItemRect(self.item(0))
            last_rect  = self.visualItemRect(self.item(count - 1))
            if pos.y() < first_rect.top():
                return 0, first_rect, True   # before very first
            else:
                return count - 1, last_rect, False  # after very last

        # Sort row left→right
        row.sort(key=lambda t: t[1].left())

        # Walk items and use their centers as boundaries
        for idx, rect in row:
            cx = rect.center().x()
            if pos.x() < cx:
                # Insert BEFORE this item (marker at its left edge)
                return idx, rect, True

        # Cursor is to the right of all centers in this row → AFTER the last item
        last_idx, last_rect = row[-1]
        return last_idx, last_rect, False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("pdfkiwi — organize your pdf")
        self.resize(1100, 720)

        # Central stack: [0] large drop, [1] editor (thumbs + small drop + create)
        self.stack = QStackedWidget(self)
        self.setCentralWidget(self.stack)

        # Page 0: Large drop screen
        self.large_drop = DropBox(on_files_dropped=self.add_pdfs, small=False)
        page0 = QWidget()
        p0_lay = QVBoxLayout(page0)
        p0_lay.addStretch(1)
        p0_lay.addWidget(self.large_drop, 0, Qt.AlignCenter)
        p0_lay.addStretch(1)
        self.stack.addWidget(page0)

        # Page 1: Editor view
        self.page_list = PageList()
        self.page_list.model().rowsInserted.connect(lambda *_: self._update_trash_visibility())
        self.page_list.model().rowsRemoved.connect(lambda *_: self._update_trash_visibility())
        self.small_drop = DropBox(on_files_dropped=self.add_pdfs, small=True)
        self.trash_box = TrashBox(on_pages_dropped=self._trash_from_list)
        self.trash_box.hide()  # only show when there are pages
        self.create_btn = QPushButton("Create")
        self.create_btn.setMinimumHeight(52)
        self.create_btn.setCursor(Qt.PointingHandCursor)
        self.create_btn.setObjectName("CreateButton")
        self.create_btn.clicked.connect(self.create_pdf)
        
        self.create_btn.setMinimumHeight(56)
        self.create_btn.setCursor(Qt.PointingHandCursor)
        shadow = QGraphicsDropShadowEffect(self.create_btn)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 60))
        self.create_btn.setGraphicsEffect(shadow)

        editor = QWidget()
        ed_lay = QHBoxLayout(editor)
        ed_lay.setContentsMargins(16, 16, 16, 16)
        ed_lay.setSpacing(12)

        # Center: thumbnails + small bottom drop
        center = QWidget()
        c_lay = QVBoxLayout(center)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.setSpacing(10)
        c_lay.addWidget(self.page_list, 1)

        small_wrap = QWidget()
        sw_lay = QHBoxLayout(small_wrap)
        sw_lay.setSpacing(12)
        sw_lay.addStretch(1)
        sw_lay.addWidget(self.small_drop, 0, Qt.AlignCenter)
        sw_lay.addWidget(self.trash_box, 0, Qt.AlignCenter)
        sw_lay.addStretch(1)
        c_lay.addWidget(small_wrap, 0)

        ed_lay.addWidget(center, 1)

        # Right column: Create button
        right_col = QWidget()
        r_lay = QVBoxLayout(right_col)
        r_lay.addStretch(1)
        r_lay.addWidget(self.create_btn, 0, Qt.AlignRight)
        ed_lay.addWidget(right_col, 0)

        self.stack.addWidget(editor)

        # Toolbar (optional)
        act_clear = QAction("Clear all pages", self)
        act_clear.triggered.connect(self.clear_all)
        self.toolbar = self.addToolBar("Main")
        self.toolbar.addAction(act_clear)

        # ---- Styling: Kiwi palette, darker base to avoid empty feel ----
        self.setStyleSheet("""
            QWidget {
                background-color: #E1E8DE;   /* slightly darker, less empty */
                font-family: "Segoe UI","Ubuntu","DejaVu Sans",sans-serif;
                font-size: 14px;
                color: #2C2C2C;
            }

            /* Thumbnails container surface */
            QListWidget {
                background: #F6F8F5;              /* not pure white to separate from page white */
                border: 1px solid #C9D2C2;
                border-radius: 10px;
                padding: 10px;
            }

            /* Drop areas */
            QFrame#DropBox[small="false"], QFrame#DropBox[small="true"] {
                border: 2px dashed #7FB069;       /* calmer kiwi */
                border-radius: 14px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                            stop:0 #F1F6ED, stop:1 #E7EFE2);
            }
            QFrame#DropBox QLabel {
                color: #4E7A34;
                font-size: 16px;
                font-weight: 600;
            }

            /* Button */
            QPushButton {
                background-color: #88C057;
                color: #FFFFFF;
                border-radius: 12px;
                padding: 12px 22px;
                border: none;
                font-weight: 700;
                font-size: 16px;
            }
            QPushButton:hover { background-color: #6EA83F; }
            QPushButton:pressed { background-color: #5A8C36; }

            /* Selection feedback */
            QListWidget::item:selected {
                background: rgba(136,192,87,0.12);
                border: 1px solid rgba(136,192,87,0.35);
                border-radius: 8px;
            }
            QListWidget::item:hover {
                background: rgba(0,0,0,0.03);
                border-radius: 8px;
            }
        """)
        
        self.setStyleSheet(self.styleSheet() + """
            /* Vertical scrollbar */
            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 2px 2px 2px 2px;
            }
            QScrollBar::handle:vertical {
                background: #C7D3C2;
                min-height: 24px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background: #B6C6B0;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
                background: none;
            }

            /* Horizontal scrollbar */
            QScrollBar:horizontal {
                background: transparent;
                height: 10px;
                margin: 2px 2px 2px 2px;
            }
            QScrollBar::handle:horizontal {
                background: #C7D3C2;
                min-width: 24px;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #B6C6B0;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0;
                background: none;
            }
        """)
        self.setStyleSheet(self.styleSheet() + """
            QFrame#TrashBox {
                border: 2px dashed #C75252;       /* calm red */
                border-radius: 14px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                            stop:0 #FCEEEE, stop:1 #F9E3E3);
            }
            QFrame#TrashBox QLabel {
                color: #8E2E2E;
                font-size: 15px;
                font-weight: 700;
            }
        """)



    # ---------- Data & UI helpers ----------

    def add_pdfs(self, paths: List[str]):
        """Add PDFs (append their pages) and switch to editor view."""
        added_any = False
        for p in paths:
            try:
                n_pages = self._count_pages(p)
                if n_pages <= 0:
                    continue
                for i in range(n_pages):
                    self._append_page_item(PageRef(src_path=os.path.abspath(p), page_index=i))
                added_any = True
            except Exception as ex:
                error_box("Load error", f"Cannot read PDF:\n{p}\n\n{ex}", self)
        if added_any:
            self.stack.setCurrentIndex(1)  # show editor
            self._update_trash_visibility()

    def _count_pages(self, path: str) -> int:
        if HAVE_FITZ:
            with fitz.open(path) as doc:
                return doc.page_count
        # Fallback: pdfinfo
        if command_exists("pdfinfo"):
            try:
                out = subprocess.check_output(["pdfinfo", path], text=True, stderr=subprocess.STDOUT)
                for line in out.splitlines():
                    if line.lower().startswith("pages:"):
                        return int(line.split(":")[1].strip())
            except Exception:
                pass
        return 0

    def _thumb_for(self, page: PageRef, max_w=120, max_h=168) -> QIcon:
        """
        Build a composed thumbnail:
          - kiwi-tinted matte background
          - white page card with subtle border
          - rendered page inside with small padding
        """
        # 1) render PDF page to QPixmap (pm_render) using PyMuPDF if available
        pm_render = None
        if HAVE_FITZ:
            try:
                with fitz.open(page.src_path) as doc:
                    pg = doc.load_page(page.page_index)
                    pix = pg.get_pixmap(matrix=fitz.Matrix(0.6, 0.6), alpha=False)
                    raw = pix.tobytes("ppm")
                    pm_render = QPixmap()
                    pm_render.loadFromData(raw)
            except Exception as e:
                print("Thumbnail error:", e)

        # Fallback blank render if needed
        if pm_render is None or pm_render.isNull():
            pm_render = QPixmap(max_w, max_h)
            pm_render.fill(Qt.white)

        # Fit the render into a "page" rect (content inset inside the page card)
        canvas_w, canvas_h = max_w, max_h
        page_margin = 6         # inner padding between page border and content
        card_radius = 8

        # Create matte canvas
        canvas = QImage(canvas_w, canvas_h, QImage.Format_ARGB32_Premultiplied)
        canvas.fill(QColor("#EEF3EA"))   # soft kiwi matte (not pure white)

        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # 2) draw a subtle shadow-ish underlay (very soft)
        under = QColor(0, 0, 0, 18)
        painter.setPen(Qt.NoPen)
        painter.setBrush(under)
        painter.drawRoundedRect(4, 6, canvas_w-8, canvas_h-10, card_radius, card_radius)

        # 3) draw white page card
        page_rect = QRect(6, 6, canvas_w-12, canvas_h-12)
        painter.setBrush(QBrush(Qt.white))
        painter.setPen(QPen(QColor("#D7D7D7")))  # thin border
        painter.drawRoundedRect(page_rect, card_radius, card_radius)

        # 4) draw the page content scaled to page_rect minus inner margin
        content_rect = page_rect.adjusted(page_margin, page_margin, -page_margin, -page_margin)
        pm_scaled = pm_render.scaled(content_rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)

        # center the content rect
        cx = content_rect.x() + (content_rect.width() - pm_scaled.width()) // 2
        cy = content_rect.y() + (content_rect.height() - pm_scaled.height()) // 2
        painter.drawPixmap(cx, cy, pm_scaled)

        painter.end()
        return QIcon(QPixmap.fromImage(canvas))

    def _append_page_item(self, page: PageRef):
        icon = self._thumb_for(page)
        label = f"{os.path.basename(page.src_path)}\npage {page.page_index + 1}"
        item = QListWidgetItem(icon, label)
        # item.setSizeHint(QSize(132, 210))  # cell size (icon + text)
        # item = QListWidgetItem(icon, "")  # no text
        item.setSizeHint(QSize(132, 190))
        # item.setToolTip(f"{os.path.basename(page.src_path)} — page {page.page_index+1}")
        item.setData(Qt.UserRole, page)
        self.page_list.addItem(item)

    def clear_all(self):
        self.page_list.clear()
        self._update_trash_visibility()
        self.stack.setCurrentIndex(0)

    def _gather_current_pages(self) -> List[PageRef]:
        lst = []
        for i in range(self.page_list.count()):
            it = self.page_list.item(i)
            lst.append(it.data(Qt.UserRole))
        return lst

    # ---------- Export via pdfseparate + pdfunite ----------

    def create_pdf(self):
        pages = self._gather_current_pages()
        if not pages:
            info_box("Nothing to export", "Add pages first.", self)
            return

        if not command_exists("pdfseparate") or not command_exists("pdfunite"):
            error_box("Missing tools",
                      "This app uses `pdfseparate` and `pdfunite` (poppler-utils).\n"
                      "Install them, e.g.: sudo apt install poppler-utils", self)
            return

        out_path, _ = QFileDialog.getSaveFileName(self, "Save output PDF", "output.pdf", "PDF Files (*.pdf)")
        if not out_path:
            return

        try:
            with tempfile.TemporaryDirectory(prefix="pdfkiwi_") as tmpdir:
                part_files = []
                for idx, pr in enumerate(pages, start=1):
                    single_path = os.path.join(tmpdir, f"part_{idx:05d}.pdf")
                    cmd = ["pdfseparate", "-f", str(pr.page_index + 1), "-l", str(pr.page_index + 1),
                           pr.src_path, single_path]
                    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    if r.returncode != 0 or not os.path.exists(single_path):
                        raise RuntimeError(f"pdfseparate failed for {os.path.basename(pr.src_path)} page {pr.page_index+1}\n{r.stderr}")
                    part_files.append(single_path)

                cmd = ["pdfunite", *part_files, out_path]
                r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if r.returncode != 0:
                    raise RuntimeError(f"pdfunite failed:\n{r.stderr}")

            info_box("Done", f"Created:\n{out_path}", self)
        except Exception as ex:
            error_box("Export error", str(ex), self)

    def _trash_from_list(self, lst: QListWidget):
        """Remove the items currently being dragged (the selection) from the given list."""
        # Remove in reverse row order to avoid index shifts
        rows = sorted({i.row() for i in lst.selectedIndexes()}, reverse=True)
        for r in rows:
            it = lst.takeItem(r)
            del it
        self._update_trash_visibility()

    def _update_trash_visibility(self):
        has_pages = self.page_list.count() > 0
        self.trash_box.setVisible(has_pages)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

