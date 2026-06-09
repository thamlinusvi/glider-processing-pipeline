"""
Metadata Editor — edit the four JSON config files for a deployment.
Tab per file; Form view (key fields) + Raw JSON toggle.
"""
import json
import os

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QFormLayout, QLineEdit, QTextEdit,
    QMessageBox, QFrame, QScrollArea, QSizePolicy,
)
from PyQt5.QtCore import Qt


JSON_FILES = {
    'deployment.json':         'Deployment',
    'global_attributes.json':  'Global Attributes',
    'instruments.json':        'Instruments',
    'sensor_defs.json':        'Sensor Defs',
}

# Key fields to expose as a form in deployment.json
DEPLOYMENT_FORM_KEYS = [
    ('glider',           'Glider ID'),
    ('trajectory_name',  'Trajectory name'),
]
# Key fields for global_attributes nested under global_attributes key
GLOBAL_FORM_KEYS = [
    ('sea_name',          'Sea name'),
    ('deployment',        'Deployment ID'),
    ('institution_name',  'Institution'),
    ('project',           'Project'),
    ('summary',           'Summary'),
    ('wmo_id',            'WMO ID'),
]


class JsonEditorTab(QWidget):
    """Tab for a single JSON file — form fields + raw JSON editor."""

    def __init__(self, filepath, form_keys=None, nested_key=None, parent=None):
        super().__init__(parent)
        self.filepath   = filepath
        self.form_keys  = form_keys or []
        self.nested_key = nested_key   # if form fields live inside a nested dict
        self._data      = {}
        self._build()
        self.load()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Tabs: Form | Raw JSON
        self._inner_tabs = QTabWidget()

        # ── Form tab ───────────────────────────────────────────────
        form_widget = QScrollArea()
        form_widget.setWidgetResizable(True)
        form_widget.setFrameShape(QFrame.NoFrame)

        form_inner = QWidget()
        self._form = QFormLayout(form_inner)
        self._form.setLabelAlignment(Qt.AlignRight)
        self._form.setContentsMargins(8, 8, 8, 8)
        self._form.setSpacing(8)
        self._form_fields = {}

        if self.form_keys:
            for key, label in self.form_keys:
                field = QLineEdit()
                field.setPlaceholderText(key)
                self._form_fields[key] = field
                self._form.addRow(label + ':', field)
        else:
            self._form.addRow(QLabel(
                'No quick-edit fields defined for this file.\n'
                'Use the Raw JSON tab to edit directly.'))

        form_widget.setWidget(form_inner)
        self._inner_tabs.addTab(form_widget, 'Quick Edit')

        # ── Raw JSON tab ────────────────────────────────────────────
        self._raw_edit = QTextEdit()
        self._raw_edit.setObjectName('log_pane')
        self._raw_edit.setStyleSheet(
            '#log_pane { background:#1a1a2e; color:#a8dadc; '
            'font-family:Consolas,monospace; font-size:12px; }')
        self._inner_tabs.addTab(self._raw_edit, 'Raw JSON')

        layout.addWidget(self._inner_tabs)

        # Save button
        btn_row = QHBoxLayout()
        save_btn = QPushButton('💾  Save')
        save_btn.setObjectName('btn_success')
        save_btn.clicked.connect(self.save)
        btn_row.addWidget(save_btn)

        reload_btn = QPushButton('↺  Reload from disk')
        reload_btn.setObjectName('btn_secondary')
        reload_btn.clicked.connect(self.load)
        btn_row.addWidget(reload_btn)

        btn_row.addStretch()

        self._status_lbl = QLabel('')
        self._status_lbl.setStyleSheet('color:#27ae60; font-size:12px;')
        btn_row.addWidget(self._status_lbl)

        layout.addLayout(btn_row)

    # ── Load / save ────────────────────────────────────────────────────────────
    def load(self):
        self._status_lbl.clear()
        if not self.filepath or not os.path.exists(self.filepath):
            self._raw_edit.setPlainText('File not found:\n' + (self.filepath or '(no path)'))
            return
        try:
            with open(self.filepath, 'r', encoding='utf-8-sig') as f:
                self._data = json.load(f)
            self._raw_edit.setPlainText(json.dumps(self._data, indent=2))
            # Populate form fields
            src = self._data.get(self.nested_key, {}) if self.nested_key else self._data
            for key, _label in self.form_keys:
                val = src.get(key, '')
                if key in self._form_fields:
                    self._form_fields[key].setText(str(val))
        except Exception as e:
            self._raw_edit.setPlainText(f'Error loading JSON:\n{e}')

    def save(self):
        self._status_lbl.clear()
        # If raw JSON tab is active and was edited, parse from there
        if self._inner_tabs.currentIndex() == 1:
            try:
                self._data = json.loads(self._raw_edit.toPlainText())
            except json.JSONDecodeError as e:
                QMessageBox.warning(self, 'Invalid JSON', str(e))
                return
        else:
            # Apply form field values back into _data
            dest = self._data.get(self.nested_key) if self.nested_key else self._data
            if dest is None:
                dest = {}
                if self.nested_key:
                    self._data[self.nested_key] = dest
            for key, _label in self.form_keys:
                if key in self._form_fields:
                    dest[key] = self._form_fields[key].text()
            # Sync raw view
            self._raw_edit.setPlainText(json.dumps(self._data, indent=2))

        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, indent=4)
            self._status_lbl.setText('Saved ✔')
        except Exception as e:
            QMessageBox.critical(self, 'Save failed', str(e))


class MetadataEditorPage(QWidget):
    def __init__(self, base_dir, parent=None):
        super().__init__(parent)
        self.base_dir   = base_dir
        self.deployment = None
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(0)

        title = QLabel('Metadata Editor')
        title.setObjectName('page_title')
        outer.addWidget(title)

        self._sub = QLabel('Edit deployment JSON configuration files.')
        self._sub.setObjectName('page_subtitle')
        outer.addWidget(self._sub)

        outer.addSpacing(12)

        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        self._no_dep_lbl = QLabel(
            'Select a deployment from the sidebar to edit its metadata.')
        self._no_dep_lbl.setAlignment(Qt.AlignCenter)
        self._no_dep_lbl.setStyleSheet('color:#95a5a6; font-size:13px;')
        outer.addWidget(self._no_dep_lbl)

    def set_deployment(self, deployment):
        self.deployment = deployment
        self._sub.setText(f'Deployment: {deployment}')
        self._no_dep_lbl.setVisible(False)
        self._tabs.setVisible(True)
        self._load_tabs()

    def _load_tabs(self):
        self._tabs.clear()
        glider   = self.deployment.split('-')[0]
        cfg_dir  = os.path.join(
            self.base_dir, 'glider_config_files_json',
            glider, self.deployment, 'science_profile')

        for filename, tab_label in JSON_FILES.items():
            filepath = os.path.join(cfg_dir, filename)

            if filename == 'deployment.json':
                tab = JsonEditorTab(filepath, DEPLOYMENT_FORM_KEYS, None)
            elif filename == 'global_attributes.json':
                tab = JsonEditorTab(filepath, GLOBAL_FORM_KEYS, 'global_attributes')
            else:
                tab = JsonEditorTab(filepath)

            self._tabs.addTab(tab, tab_label)
