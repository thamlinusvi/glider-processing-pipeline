"""
Main application window — sidebar navigation + stacked content pages.
"""
import os
import glob

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QStackedWidget,
    QComboBox, QFrame, QSizePolicy, QStatusBar,
)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QFont

from ui.styles       import STYLESHEET
from ui.dashboard    import DashboardPage
from ui.pipeline_page import PipelinePage
from ui.dat_browser  import DatBrowserPage
from ui.metadata_editor import MetadataEditorPage
from ui.report_viewer   import ReportViewerPage


NAV_ITEMS = [
    'Dashboard',
    'DAT Browser',
    'Metadata',
    'Pipeline',
    'Reports',
]
# index → page index in stacked widget
PAGE_DASHBOARD  = 0
PAGE_DAT        = 1
PAGE_METADATA   = 2
PAGE_PIPELINE   = 3
PAGE_REPORTS    = 4


class MainWindow(QMainWindow):
    def __init__(self, base_dir, parent=None):
        super().__init__(parent)
        self.base_dir   = base_dir
        self.deployment = None

        self.setWindowTitle('UVI Glider Pipeline')
        self.resize(1280, 820)
        self.setMinimumSize(900, 600)
        self.setStyleSheet(STYLESHEET)

        self._build()
        self._populate_deployments()

    # ── UI construction ────────────────────────────────────────────────────────
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Sidebar ────────────────────────────────────────────────
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(220)
        sv = QVBoxLayout(sidebar)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)

        # App title
        title_lbl = QLabel('UVI Glider')
        title_lbl.setObjectName('app_title')
        sv.addWidget(title_lbl)

        sub_lbl = QLabel('Processing Pipeline')
        sub_lbl.setObjectName('app_subtitle')
        sv.addWidget(sub_lbl)

        # Deployment selector
        dep_hdr = QLabel('DEPLOYMENT')
        dep_hdr.setObjectName('deployment_label')
        sv.addWidget(dep_hdr)

        self._dep_combo = QComboBox()
        self._dep_combo.setObjectName('deployment_combo')
        self._dep_combo.currentIndexChanged.connect(self._on_deployment_changed)
        sv.addWidget(self._dep_combo)

        # Nav list
        self._nav = QListWidget()
        self._nav.setObjectName('nav_list')
        self._nav.setSpacing(2)
        for label in NAV_ITEMS:
            item = QListWidgetItem(f'    {label}')
            item.setSizeHint(QSize(220, 42))
            self._nav.addItem(item)
        self._nav.setCurrentRow(0)
        self._nav.currentRowChanged.connect(self._on_nav_changed)
        sv.addWidget(self._nav, 1)

        # Sidebar footer
        sv.addStretch()
        footer = QLabel(f'<span style="color:#5d7a99; font-size:9px; '
                        f'text-transform:uppercase; letter-spacing:1px;">'
                        f'PIPELINE ROOT</span><br>'
                        f'<span style="color:#3d6080; font-size:10px;">'
                        f'{self.base_dir}</span>')
        footer.setWordWrap(True)
        footer.setStyleSheet(
            'padding:12px 14px; background:#141e2e;'
            'border-top:1px solid #1e2d40;'
        )
        footer.setTextFormat(Qt.RichText)
        sv.addWidget(footer)

        root.addWidget(sidebar)

        # ── Content area ───────────────────────────────────────────
        content = QWidget()
        content.setObjectName('content')
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)

        self._stack = QStackedWidget()

        self._dashboard = DashboardPage(self.base_dir)
        self._dashboard.open_deployment.connect(self._open_deployment)
        self._stack.addWidget(self._dashboard)                       # 0

        self._dat_browser = DatBrowserPage(self.base_dir)
        self._stack.addWidget(self._dat_browser)                     # 1

        self._metadata = MetadataEditorPage(self.base_dir)
        self._stack.addWidget(self._metadata)                        # 2

        self._pipeline = PipelinePage(self.base_dir)
        self._stack.addWidget(self._pipeline)                        # 3

        self._reports = ReportViewerPage(self.base_dir)
        self._stack.addWidget(self._reports)                         # 4

        cv.addWidget(self._stack)
        root.addWidget(content, 1)

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage('Ready')

    # ── Deployment population ──────────────────────────────────────────────────
    def _populate_deployments(self):
        incoming = os.path.join(self.base_dir, 'incoming_data')
        self._dep_combo.blockSignals(True)
        self._dep_combo.clear()
        if os.path.isdir(incoming):
            deps = sorted(
                d for d in os.listdir(incoming)
                if os.path.isdir(os.path.join(incoming, d))
            )
            for d in deps:
                self._dep_combo.addItem(d)
        self._dep_combo.blockSignals(False)

        if self._dep_combo.count():
            self._dep_combo.setCurrentIndex(0)
            self._on_deployment_changed(0)

    # ── Slots ─────────────────────────────────────────────────────────────────
    def _on_nav_changed(self, row):
        self._stack.setCurrentIndex(row)
        page_name = NAV_ITEMS[row]
        self._status.showMessage(page_name)
        # Refresh dashboard when navigating to it
        if row == PAGE_DASHBOARD:
            self._dashboard.refresh()

    def _on_deployment_changed(self, index):
        dep = self._dep_combo.itemText(index)
        if not dep:
            return
        self.deployment = dep
        self._status.showMessage(f'Deployment: {dep}')
        self._dat_browser.set_deployment(dep)
        self._metadata.set_deployment(dep)
        self._pipeline.set_deployment(dep)
        self._reports.set_deployment(dep)

    def _open_deployment(self, deployment):
        """Called from dashboard card — switch to pipeline page."""
        idx = self._dep_combo.findText(deployment)
        if idx >= 0:
            self._dep_combo.setCurrentIndex(idx)
        self._nav.setCurrentRow(PAGE_PIPELINE)
