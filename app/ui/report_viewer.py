"""
Report Viewer — embedded HTML QC report + plot image gallery.
"""
import os
import glob

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QScrollArea, QFrame, QGridLayout, QSizePolicy,
    QToolButton,
)
from PyQt5.QtCore import Qt, QUrl, QSize
from PyQt5.QtGui import QPixmap, QIcon, QDesktopServices

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
    _HAS_WEBENGINE = True
except ImportError:
    _HAS_WEBENGINE = False


class PlotThumbnail(QWidget):
    """Clickable image thumbnail with filename label."""

    def __init__(self, filepath, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignCenter)

        btn = QToolButton()
        btn.setFixedSize(220, 160)
        btn.setCursor(Qt.PointingHandCursor)
        pix = QPixmap(filepath).scaled(
            210, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        btn.setIcon(QIcon(pix) if not pix.isNull() else QIcon())
        btn.setIconSize(QSize(210, 150))
        btn.setStyleSheet('border:1px solid #ddd; border-radius:4px; background:white;')
        btn.clicked.connect(self._open)
        layout.addWidget(btn, alignment=Qt.AlignCenter)

        lbl = QLabel(os.path.basename(filepath))
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet('color:#555; font-size:11px;')
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(220)
        layout.addWidget(lbl)

        self.setStyleSheet('background:white; border-radius:6px; border:1px solid #e0e4e8;')

    def _open(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.filepath))


class ReportViewerPage(QWidget):
    def __init__(self, base_dir, parent=None):
        super().__init__(parent)
        self.base_dir   = base_dir
        self.deployment = None
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(0)

        title = QLabel('Reports & Plots')
        title.setObjectName('page_title')
        outer.addWidget(title)

        self._sub = QLabel('Select a deployment to view QC report and visualisations.')
        self._sub.setObjectName('page_subtitle')
        outer.addWidget(self._sub)

        outer.addSpacing(8)

        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        # ── QC Report tab ──────────────────────────────────────────
        if _HAS_WEBENGINE:
            self._web = QWebEngineView()
            self._tabs.addTab(self._web, 'QC Report')
        else:
            no_web = QLabel(
                'QWebEngineView not available.\n'
                'Click the button below to open the report in your browser.')
            no_web.setAlignment(Qt.AlignCenter)
            self._web = None
            self._tabs.addTab(no_web, 'QC Report')

        # ── Plots tab ──────────────────────────────────────────────
        plot_widget = QWidget()
        pv = QVBoxLayout(plot_widget)
        pv.setContentsMargins(0, 0, 0, 0)

        btn_row = QHBoxLayout()
        self._open_figs_btn = QPushButton('Open figs folder')
        self._open_figs_btn.setObjectName('btn_secondary')
        self._open_figs_btn.clicked.connect(self._open_figs_folder)
        btn_row.addWidget(self._open_figs_btn)
        btn_row.addStretch()
        pv.addLayout(btn_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        self._grid_widget = QWidget()
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(10)
        self._grid.setContentsMargins(0, 8, 0, 0)

        scroll.setWidget(self._grid_widget)
        pv.addWidget(scroll)

        self._tabs.addTab(plot_widget, 'Plots')

    # ── Public ─────────────────────────────────────────────────────────────────
    def set_deployment(self, deployment):
        self.deployment = deployment
        year = deployment.split('-')[1][:4]
        dep_dir = os.path.join(self.base_dir, 'deployments', year, deployment)
        self._sub.setText(f'Deployment: {deployment}')
        self._load_report(dep_dir)
        self._load_plots(dep_dir)

    # ── Internals ──────────────────────────────────────────────────────────────
    def _load_report(self, dep_dir):
        report_path = os.path.join(
            dep_dir, 'reports', f'qc_report_{self.deployment}.html')
        if not os.path.exists(report_path):
            if self._web:
                self._web.setHtml(
                    '<h3 style="color:#888;font-family:Arial">Report not yet generated.</h3>'
                    f'<p style="color:#aaa">Expected: {report_path}</p>')
            return
        if self._web:
            self._web.load(QUrl.fromLocalFile(report_path))
        else:
            # Offer to open in browser
            pass

    def _load_plots(self, dep_dir):
        # Clear existing thumbnails
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        figs_dir = os.path.join(dep_dir, 'figs')
        if not os.path.isdir(figs_dir):
            lbl = QLabel('Figures not yet generated.')
            lbl.setStyleSheet('color:#888;')
            self._grid.addWidget(lbl, 0, 0)
            return

        pngs = sorted(glob.glob(os.path.join(figs_dir, '*.png')))
        # Also check profiles subfolder
        profile_pngs = sorted(
            glob.glob(os.path.join(figs_dir, 'profiles', '*.png')))

        col_count = 4
        for i, fp in enumerate(pngs):
            thumb = PlotThumbnail(fp)
            self._grid.addWidget(thumb, i // col_count, i % col_count)

        if profile_pngs:
            row_offset = (len(pngs) + col_count - 1) // col_count + 1
            header = QLabel(f'<b>Profiles ({len(profile_pngs)} files)</b>')
            self._grid.addWidget(header, row_offset, 0, 1, col_count)
            for i, fp in enumerate(profile_pngs[:20]):   # show first 20
                thumb = PlotThumbnail(fp)
                self._grid.addWidget(
                    thumb, row_offset + 1 + i // col_count, i % col_count)

        if not pngs and not profile_pngs:
            lbl = QLabel('No PNG files found in figs directory.')
            lbl.setStyleSheet('color:#888;')
            self._grid.addWidget(lbl, 0, 0)

    def _open_figs_folder(self):
        if not self.deployment:
            return
        year = self.deployment.split('-')[1][:4]
        figs_dir = os.path.join(
            self.base_dir, 'deployments', year, self.deployment, 'figs')
        os.makedirs(figs_dir, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(figs_dir))
