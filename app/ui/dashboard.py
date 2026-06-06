"""
Dashboard page — shows all deployments with pipeline stage status cards.
"""
import os
import glob

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QGridLayout, QSpacerItem,
)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont

from pipeline.state import load_state, STAGES, STATUS_DONE, STATUS_FAILED, STATUS_RUNNING

STAGE_LABELS = {
    'dbd_to_dat':           'DBD→DAT',
    'dat_to_profile_nc':    'Profile NC',
    'dat_to_trajectory_nc': 'Traj NC',
    'qc':                   'QC',
    'report':               'Report',
}

STATUS_COLORS = {
    STATUS_DONE:    ('#d5f5e3', '#1a7a40'),
    STATUS_RUNNING: ('#d6eaf8', '#1a5276'),
    STATUS_FAILED:  ('#fadbd8', '#922b21'),
    'not_run':      ('#f2f3f4', '#7f8c8d'),
}


class DeploymentCard(QFrame):
    open_requested = pyqtSignal(str)   # deployment name

    def __init__(self, deployment, deployment_dir, parent=None):
        super().__init__(parent)
        self.deployment = deployment
        self.deployment_dir = deployment_dir
        self.setObjectName('deployment_card')
        self.setStyleSheet("""
            #deployment_card {
                background: white;
                border: 1px solid #dde4ec;
                border-left: 4px solid #3498db;
                border-radius: 6px;
            }
            #deployment_card:hover {
                border-color: #2980b9;
                border-left-color: #2980b9;
                background: #fafcff;
            }
        """)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # Header row
        header = QHBoxLayout()
        name_lbl = QLabel(self.deployment)
        name_lbl.setFont(QFont('Segoe UI', 12, QFont.Bold))
        header.addWidget(name_lbl)
        header.addStretch()

        open_btn = QPushButton('Open →')
        open_btn.setFixedWidth(90)
        open_btn.clicked.connect(lambda: self.open_requested.emit(self.deployment))
        header.addWidget(open_btn)
        layout.addLayout(header)

        # Stage badges
        state = load_state(self.deployment_dir)
        badges = QHBoxLayout()
        badges.setSpacing(6)
        for stage in STAGES:
            status = state.get(stage, {}).get('status', 'not_run')
            bg, fg = STATUS_COLORS.get(status, STATUS_COLORS['not_run'])
            badge = QLabel(STAGE_LABELS.get(stage, stage))
            badge.setStyleSheet(
                f'background:{bg}; color:{fg}; border-radius:10px;'
                f'padding:2px 8px; font-size:11px; font-weight:bold;'
            )
            badge.setAlignment(Qt.AlignCenter)
            badges.addWidget(badge)
        badges.addStretch()
        layout.addLayout(badges)

        # Paths
        nc_dir = os.path.join(
            self.deployment_dir,
            'data', 'out', 'nc', 'sci-profile', 'delayed',
        )
        if os.path.isdir(nc_dir):
            n_nc = len(glob.glob(os.path.join(nc_dir, '*.nc')))
            info = QLabel(f'{n_nc:,} QC\'d profile files  ·  {nc_dir}')
        else:
            info = QLabel('QC output not yet generated')
        info.setStyleSheet('color:#8a9bac; font-size:11px;')
        info.setWordWrap(True)
        layout.addWidget(info)


class DashboardPage(QWidget):
    open_deployment = pyqtSignal(str)   # emitted when user clicks Open

    def __init__(self, base_dir, parent=None):
        super().__init__(parent)
        self.base_dir = base_dir
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(0)

        # Title
        title = QLabel('Deployments')
        title.setObjectName('page_title')
        outer.addWidget(title)

        sub = QLabel('All glider deployments found in this pipeline directory.')
        sub.setObjectName('page_subtitle')
        outer.addWidget(sub)

        outer.addSpacing(12)

        # Scroll area for cards
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        self._card_container = QWidget()
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setSpacing(10)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.addStretch()

        scroll.setWidget(self._card_container)
        outer.addWidget(scroll)

        self.refresh()

    def refresh(self):
        # Remove old cards (keep the stretch at end)
        while self._card_layout.count() > 1:
            item = self._card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        deployments = self._find_deployments()
        if not deployments:
            lbl = QLabel('No deployments found.\n\nAdd deployment folders under:\n'
                         f'  incoming_data/<deployment-name>/')
            lbl.setStyleSheet('color:#95a5a6; font-size:13px;')
            lbl.setAlignment(Qt.AlignCenter)
            self._card_layout.insertWidget(0, lbl)
            return

        for i, (name, dep_dir) in enumerate(deployments):
            card = DeploymentCard(name, dep_dir, self)
            card.open_requested.connect(self.open_deployment)
            self._card_layout.insertWidget(i, card)

    def _find_deployments(self):
        """
        Look for deployment names in incoming_data/ and cross-reference
        with dat_files/ and deployments/.
        """
        results = []
        incoming = os.path.join(self.base_dir, 'incoming_data')
        if not os.path.isdir(incoming):
            return results

        for name in sorted(os.listdir(incoming)):
            dep_path = os.path.join(incoming, name)
            if not os.path.isdir(dep_path):
                continue
            year = name.split('-')[1][:4] if '-' in name and len(name.split('-')) > 1 else '2025'
            dep_dir = os.path.join(self.base_dir, 'deployments', year, name)
            os.makedirs(dep_dir, exist_ok=True)
            results.append((name, dep_dir))
        return results
