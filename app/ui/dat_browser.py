"""
DAT Browser — browse merged DAT files, inspect variables, and choose
which sensors feed into the NetCDF conversion.
"""
import os
import glob

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QSplitter, QFrame, QHeaderView, QAbstractItemView, QCheckBox,
    QTextEdit, QGroupBox,
)
from PyQt5.QtCore import Qt, pyqtSignal


def _parse_dat_header(filepath):
    """
    Read the header of a Slocum DAT file and return a list of
    (variable_name, unit, n_bytes) tuples.
    """
    vars_info = []
    try:
        with open(filepath, 'r', errors='replace') as f:
            lines = [f.readline().rstrip() for _ in range(4)]
        if len(lines) < 4:
            return vars_info
        names = lines[0].split()
        units = lines[1].split()
        nbytes = lines[2].split()
        for i, name in enumerate(names):
            unit = units[i] if i < len(units) else ''
            nb   = nbytes[i] if i < len(nbytes) else ''
            vars_info.append({'name': name, 'unit': unit, 'bytes': nb})
    except Exception:
        pass
    return vars_info


def _count_rows(filepath):
    """Count data rows (non-header) in a DAT file."""
    try:
        with open(filepath, 'r', errors='replace') as f:
            for _ in range(4):          # skip 4-line header
                f.readline()
            return sum(1 for _ in f if _.strip())
    except Exception:
        return 0


class DatBrowserPage(QWidget):
    def __init__(self, base_dir, parent=None):
        super().__init__(parent)
        self.base_dir   = base_dir
        self.deployment = None
        self._build()

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(0)

        title = QLabel('DAT File Browser')
        title.setObjectName('page_title')
        outer.addWidget(title)

        self._sub = QLabel('Inspect merged DAT files and review available sensor variables.')
        self._sub.setObjectName('page_subtitle')
        outer.addWidget(self._sub)

        outer.addSpacing(12)

        # Splitter: file list | variable table
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)

        # ── Left: file list ────────────────────────────────────────
        left = QWidget()
        lv   = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)

        lv.addWidget(QLabel('<b>DAT files</b>'))
        self._file_list = QListWidget()
        self._file_list.setAlternatingRowColors(True)
        self._file_list.currentItemChanged.connect(self._on_file_selected)
        lv.addWidget(self._file_list)

        self._file_count_lbl = QLabel('')
        self._file_count_lbl.setStyleSheet('color:#888; font-size:11px;')
        lv.addWidget(self._file_count_lbl)

        splitter.addWidget(left)

        # ── Right: variable table ──────────────────────────────────
        right = QWidget()
        rv    = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)

        rv.addWidget(QLabel('<b>Variables in selected file</b>'))

        self._var_table = QTableWidget(0, 4)
        self._var_table.setHorizontalHeaderLabels(
            ['Variable', 'Unit', 'Bytes', 'Data rows'])
        self._var_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._var_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._var_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._var_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._var_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._var_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._var_table.setAlternatingRowColors(True)
        rv.addWidget(self._var_table)

        # Preview (first 5 data lines)
        rv.addWidget(QLabel('<b>Raw data preview (first 5 rows)</b>'))
        self._preview = QTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setObjectName('log_pane')
        self._preview.setFixedHeight(110)
        rv.addWidget(self._preview)

        splitter.addWidget(right)
        splitter.setSizes([280, 700])

        outer.addWidget(splitter)

    # ── Public ─────────────────────────────────────────────────────────────────
    def set_deployment(self, deployment):
        self.deployment = deployment
        self._sub.setText(
            f'Deployment: {deployment}  ·  '
            f'dat_files/{deployment}/merged_data/'
        )
        self._load_files()

    # ── Internals ──────────────────────────────────────────────────────────────
    def _load_files(self):
        self._file_list.clear()
        self._var_table.setRowCount(0)
        self._preview.clear()

        if not self.deployment:
            return

        dat_dir = os.path.join(
            self.base_dir, 'dat_files', self.deployment, 'merged_data')
        if not os.path.isdir(dat_dir):
            self._file_count_lbl.setText('Directory not found.')
            return

        files = sorted(glob.glob(os.path.join(dat_dir, '*.dat')))
        self._file_count_lbl.setText(f'{len(files)} files')

        for fp in files:
            item = QListWidgetItem(os.path.basename(fp))
            item.setData(Qt.UserRole, fp)
            self._file_list.addItem(item)

        if files:
            self._file_list.setCurrentRow(0)

    def _on_file_selected(self, current, _previous):
        self._var_table.setRowCount(0)
        self._preview.clear()
        if not current:
            return

        filepath = current.data(Qt.UserRole)
        vars_info = _parse_dat_header(filepath)
        n_rows    = _count_rows(filepath)

        self._var_table.setRowCount(len(vars_info))
        for row, v in enumerate(vars_info):
            self._var_table.setItem(row, 0, QTableWidgetItem(v['name']))
            self._var_table.setItem(row, 1, QTableWidgetItem(v['unit']))
            self._var_table.setItem(row, 2, QTableWidgetItem(v['bytes']))
            count_item = QTableWidgetItem(str(n_rows) if row == 0 else '')
            count_item.setTextAlignment(Qt.AlignCenter)
            self._var_table.setItem(row, 3, count_item)

        # Preview first 5 data rows
        try:
            with open(filepath, 'r', errors='replace') as f:
                for _ in range(4):
                    f.readline()
                lines = [f.readline().rstrip() for _ in range(5)]
            self._preview.setPlainText('\n'.join(l for l in lines if l))
        except Exception as e:
            self._preview.setPlainText(f'Error reading file: {e}')
