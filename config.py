from __future__ import annotations

from calibre.utils.config import JSONConfig
from qt.core import QCheckBox, QFormLayout, QHBoxLayout, QLineEdit, QSpinBox, QWidget, QLabel


prefs = JSONConfig('plugins/ai_metadata_review')
prefs.defaults['openai_api_key'] = ''
prefs.defaults['openai_base_url'] = 'https://api.openai.com/v1'
prefs.defaults['openai_model'] = 'gpt-5.4-mini'
prefs.defaults['use_openai'] = True
prefs.defaults['max_books_per_run'] = 50
prefs.defaults['auto_apply_local_only'] = False


class ConfigWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QFormLayout(self)

        self.use_openai = QCheckBox('Enable OpenAI fallback for ambiguous records')
        self.use_openai.setChecked(bool(prefs['use_openai']))
        layout.addRow(self.use_openai)

        self.api_key = QLineEdit(self)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setText(prefs['openai_api_key'])
        layout.addRow('OpenAI API key', self.api_key)

        self.base_url = QLineEdit(self)
        self.base_url.setText(prefs['openai_base_url'])
        layout.addRow('API base URL', self.base_url)

        self.model = QLineEdit(self)
        self.model.setText(prefs['openai_model'])
        layout.addRow('Model', self.model)

        self.max_books = QSpinBox(self)
        self.max_books.setRange(1, 500)
        self.max_books.setValue(int(prefs['max_books_per_run']))
        layout.addRow('Max books per run', self.max_books)

        self.auto_apply_local_only = QCheckBox('Auto-apply local registry fixes without prompting')
        self.auto_apply_local_only.setChecked(bool(prefs['auto_apply_local_only']))
        layout.addRow(self.auto_apply_local_only)

        note = QLabel('Leave the API key blank to read OPENAI_API_KEY from the environment.')
        note.setWordWrap(True)
        layout.addRow(note)

    def save_settings(self):
        prefs['use_openai'] = self.use_openai.isChecked()
        prefs['openai_api_key'] = self.api_key.text().strip()
        prefs['openai_base_url'] = self.base_url.text().strip() or 'https://api.openai.com/v1'
        prefs['openai_model'] = self.model.text().strip() or 'gpt-5.4-mini'
        prefs['max_books_per_run'] = int(self.max_books.value())
        prefs['auto_apply_local_only'] = self.auto_apply_local_only.isChecked()

