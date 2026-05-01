from __future__ import annotations

from dataclasses import dataclass

from calibre.ebooks.metadata.book.base import Metadata
from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import prefs
from .core import AuthorRegistry, Suggestion, ai_prompt_payload, local_suggestion, pretty_authors, split_author_blob
from .openai_client import call_openai_responses, resolve_api_key


@dataclass
class ReviewRow:
    book_id: int
    title: str
    authors: list[str]
    path: str
    suggestion: Suggestion | None = None


class MetadataReviewDialog(QDialog):
    def __init__(self, action: 'MetadataReviewAction', rows: list[ReviewRow], parent=None):
        super().__init__(parent)
        self.action = action
        self.rows = rows
        self.setWindowTitle('AI Metadata Review')
        self.resize(1200, 700)

        layout = QVBoxLayout(self)

        self.summary = QLabel(f'{len(rows)} selected book(s)')
        layout.addWidget(self.summary)

        self.use_ai = QCheckBox('Use OpenAI fallback for unresolved records')
        self.use_ai.setChecked(bool(prefs['use_openai']))
        layout.addWidget(self.use_ai)

        controls = QHBoxLayout()
        self.preview_btn = QPushButton('Build suggestions')
        self.preview_btn.clicked.connect(self.build_suggestions)
        controls.addWidget(self.preview_btn)

        self.apply_btn = QPushButton('Apply checked')
        self.apply_btn.clicked.connect(self.apply_checked)
        controls.addWidget(self.apply_btn)

        self.close_btn = QPushButton('Close')
        self.close_btn.clicked.connect(self.accept)
        controls.addWidget(self.close_btn)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['Apply', 'Book ID', 'Title', 'Current Authors', 'Suggested Authors', 'Source', 'Reason'])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText('Select a row to see JSON details.')
        layout.addWidget(self.details)

        self.table.itemSelectionChanged.connect(self.update_details)
        self.build_suggestions()

    def build_suggestions(self):
        self.table.setRowCount(0)
        self._applying = False
        registry = self.action.registry
        api_key = resolve_api_key(prefs['openai_api_key'])
        use_ai = self.use_ai.isChecked() and bool(prefs['use_openai']) and bool(api_key)
        progress = QProgressDialog('Analyzing metadata…', 'Cancel', 0, len(self.rows), self)
        progress.setWindowTitle('AI Metadata Review')
        progress.setMinimumDuration(0)
        progress.show()

        for idx, row in enumerate(self.rows, start=1):
            if progress.wasCanceled():
                break
            suggestion = local_suggestion(row.title, row.authors, registry)
            if suggestion is None and use_ai:
                try:
                    prompt = ai_prompt_payload(row.book_id, row.title, row.authors, row.path, registry)
                    suggestion = call_openai_responses(
                        api_key=api_key,
                        base_url=prefs['openai_base_url'],
                        model=prefs['openai_model'],
                        prompt=prompt,
                        current_title=row.title,
                        current_authors=row.authors,
                    )
                except Exception as err:
                    suggestion = Suggestion(
                        title=row.title,
                        authors=row.authors,
                        source='openai-error',
                        confidence=0.0,
                        reason=f'OpenAI call failed: {err}',
                        action='leave',
                        raw={'error': str(err)},
                    )
            if suggestion is None:
                suggestion = Suggestion(
                    title=row.title,
                    authors=row.authors,
                    source='none',
                    confidence=0.0,
                    reason='No obvious change.',
                    action='leave',
                    raw={},
                )
            row.suggestion = suggestion
            self._append_row(row)
            progress.setValue(idx)

        progress.close()
        self.update_details()

    def _append_row(self, row: ReviewRow):
        r = self.table.rowCount()
        self.table.insertRow(r)
        checkbox = QCheckBox()
        checkbox.setChecked(row.suggestion is not None and row.suggestion.action == 'update' and row.suggestion.confidence >= 0.7)
        self.table.setCellWidget(r, 0, checkbox)

        values = [
            str(row.book_id),
            row.title,
            pretty_authors(row.authors),
            pretty_authors(row.suggestion.authors if row.suggestion else row.authors),
            row.suggestion.source if row.suggestion else 'none',
            row.suggestion.reason if row.suggestion else '',
        ]
        for col, value in enumerate(values, start=1):
            item = QTableWidgetItem(value)
            self.table.setItem(r, col, item)

    def update_details(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.rows):
            self.details.clear()
            return
        review_row = self.rows[row]
        payload = {
            'book_id': review_row.book_id,
            'title': review_row.title,
            'authors': review_row.authors,
            'path': review_row.path,
            'suggestion': review_row.suggestion.raw if review_row.suggestion else None,
        }
        self.details.setPlainText(json_dumps(payload))

    def apply_checked(self):
        ids = []
        updates = []
        for row_index, review_row in enumerate(self.rows):
            widget = self.table.cellWidget(row_index, 0)
            if not widget or not widget.isChecked():
                continue
            if not review_row.suggestion or review_row.suggestion.action != 'update':
                continue
            ids.append(review_row.book_id)
            updates.append(review_row)
        if not updates:
            info_dialog(self, 'AI Metadata Review', 'No checked suggestions to apply.', show=True)
            return
        changed = self.action.apply_rows(updates)
        info_dialog(
            self,
            'AI Metadata Review',
            f'Applied metadata to {changed} book(s).',
            show=True,
        )
        self.accept()


def json_dumps(payload):
    import json
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


class MetadataReviewAction(InterfaceAction):
    name = 'AI Metadata Review'
    action_spec = ('AI Metadata Review', None, 'Review and fix metadata with registry and AI', None)

    def genesis(self):
        self.registry = AuthorRegistry.load_default()
        self.qaction.triggered.connect(self.run_review)

    def run_review(self):
        ids = self._selected_book_ids()
        if not ids:
            error_dialog(self.gui, 'AI Metadata Review', 'Select one or more books first.', show=True)
            return
        max_books = int(prefs['max_books_per_run'])
        ids = ids[:max_books]
        rows = [self._load_row(book_id) for book_id in ids]
        rows = [r for r in rows if r is not None]
        if not rows:
            error_dialog(self.gui, 'AI Metadata Review', 'No readable books found in the current selection.', show=True)
            return
        dlg = MetadataReviewDialog(self, rows, parent=self.gui)
        dlg.exec()

    def _selected_book_ids(self) -> list[int]:
        rows = self.gui.library_view.selectionModel().selectedRows()
        if not rows:
            return []
        model = self.gui.library_view.model()
        return [model.id(row) for row in rows]

    def _load_row(self, book_id: int) -> ReviewRow | None:
        db = self.gui.current_db.new_api
        mi = db.get_metadata(book_id, get_cover=False, get_user_categories=False)
        title = mi.title or ''
        authors = list(mi.authors or [])
        path = db.path(book_id) if hasattr(db, 'path') else ''
        if not authors:
            authors = ['Unknown']
        return ReviewRow(book_id=book_id, title=title, authors=authors, path=path)

    def apply_rows(self, rows: list[ReviewRow]) -> int:
        db = self.gui.current_db.new_api
        changed = 0
        for row in rows:
            suggestion = row.suggestion
            if not suggestion or suggestion.action != 'update':
                continue
            mi = db.get_metadata(row.book_id, get_cover=True, cover_as_data=True)
            if suggestion.title and suggestion.title != mi.title:
                mi.title = suggestion.title
            if suggestion.authors:
                mi.authors = suggestion.authors
            else:
                mi.authors = ['Unknown']
            db.set_metadata(row.book_id, mi, force_changes=True)
            changed += 1
        try:
            self.gui.library_view.model().refresh_ids([row.book_id for row in rows])
        except Exception:
            pass
        return changed

    def apply_settings(self):
        self.registry = AuthorRegistry.load_default()
