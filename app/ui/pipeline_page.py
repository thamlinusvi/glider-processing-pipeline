"""
Pipeline page — step-by-step runner with live log streaming.
One StageCard per pipeline stage; each runs a QThread worker.
Manual stages show an instruction note and Mark as Done / Not Run buttons.
"""
import os

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QFrame, QSizePolicy, QScrollArea, QGroupBox,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QTextCursor

from pipeline.stages import STAGE_INFO
from pipeline.state  import (
    load_state, set_stage_status,
    STATUS_NOT_RUN, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED,
)
from workers.pipeline_worker import PipelineWorker


STATUS_LABEL = {
    STATUS_NOT_RUN: 'NOT RUN',
    STATUS_RUNNING: 'RUNNING…',
    STATUS_DONE:    '✔ DONE',
    STATUS_FAILED:  '✖ FAILED',
}
STATUS_BADGE_STYLE = {
    STATUS_NOT_RUN: 'background:#e8edf2; color:#6b7f94; border:1px solid #c8d4e0;',
    STATUS_RUNNING: 'background:#d6eaf8; color:#1a5276; border:1px solid #a9cce3;',
    STATUS_DONE:    'background:#d5f5e3; color:#1a7a40; border:1px solid #a9dfbf;',
    STATUS_FAILED:  'background:#fadbd8; color:#922b21; border:1px solid #f1948a;',
}
LOG_COLORS = {
    'error':   '#ff6b6b',
    'warning': '#feca57',
    'default': '#7ec8a0',
    'info':    '#74b9ff',
}


def _line_color(line):
    ll = line.lower()
    if 'error' in ll or 'traceback' in ll or 'exception' in ll:
        return LOG_COLORS['error']
    if 'warning' in ll or 'warn' in ll:
        return LOG_COLORS['warning']
    if line.startswith('Building') or line.startswith('Found') or line.startswith('Saved'):
        return LOG_COLORS['info']
    return LOG_COLORS['default']


class StageCard(QGroupBox):
    """One pipeline stage — header, paths, controls, live log."""

    def __init__(self, stage_key, base_dir, deployment, parent=None):
        info = STAGE_INFO[stage_key]
        super().__init__(info['label'], parent)
        self.stage_key  = stage_key
        self.base_dir   = base_dir
        self.deployment = deployment
        self._worker    = None
        self._is_manual = info.get('manual', False)

        self.setCheckable(False)
        self._build(info)
        self._refresh_status()

    # ── UI construction ────────────────────────────────────────────────────────
    def _build(self, info):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        # Description + badge row
        top = QHBoxLayout()
        desc = QLabel(info['description'])
        desc.setStyleSheet('color:#666; font-size:12px;')
        desc.setWordWrap(True)
        top.addWidget(desc, 1)

        self._badge = QLabel()
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedWidth(90)
        self._badge.setStyleSheet(
            'border-radius:10px; padding:2px 8px; font-size:11px; font-weight:bold;')
        top.addWidget(self._badge)
        layout.addLayout(top)

        # Source / output paths (updated in set_deployment)
        self._paths_lbl = QLabel()
        self._paths_lbl.setStyleSheet('color:#888; font-size:11px;')
        self._paths_lbl.setTextFormat(Qt.RichText)
        self._paths_lbl.setWordWrap(True)
        layout.addWidget(self._paths_lbl)
        self._update_paths()

        if self._is_manual:
            # ── Manual stage: instruction note + mark-done buttons ────────────
            note_text = info.get('manual_note', 'This step is performed manually.')
            note = QLabel(note_text)
            note.setStyleSheet(
                'background:#fef9e7; border:1px solid #f9ca24; border-radius:6px;'
                'padding:10px; color:#555; font-size:12px;'
            )
            note.setWordWrap(True)
            layout.addWidget(note)

            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)

            self._done_btn = QPushButton('✔  Mark as Done')
            self._done_btn.setObjectName('btn_success')
            self._done_btn.clicked.connect(self._mark_done)
            btn_row.addWidget(self._done_btn)

            self._reset_btn = QPushButton('↺  Mark as Not Run')
            self._reset_btn.setObjectName('btn_secondary')
            self._reset_btn.clicked.connect(self._mark_not_run)
            btn_row.addWidget(self._reset_btn)

            btn_row.addStretch()
            layout.addLayout(btn_row)

            # No log pane for manual stages
            self._run_btn   = None
            self._abort_btn = None
            self._log       = None

        else:
            # ── Automated stage: run/abort + live log ──────────────────────────
            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)

            self._run_btn = QPushButton('▶  Run Stage')
            self._run_btn.setObjectName('btn_success')
            self._run_btn.clicked.connect(self._run)
            btn_row.addWidget(self._run_btn)

            self._abort_btn = QPushButton('■  Abort')
            self._abort_btn.setObjectName('btn_danger')
            self._abort_btn.setVisible(False)
            self._abort_btn.clicked.connect(self._abort)
            btn_row.addWidget(self._abort_btn)

            self._clear_btn = QPushButton('Clear log')
            self._clear_btn.setObjectName('btn_secondary')
            self._clear_btn.clicked.connect(self._clear_log)
            btn_row.addWidget(self._clear_btn)

            btn_row.addStretch()
            layout.addLayout(btn_row)

            # Log pane (hidden until first run)
            self._log = QTextEdit()
            self._log.setObjectName('log_pane')
            self._log.setReadOnly(True)
            self._log.setFixedHeight(200)
            self._log.setVisible(False)
            layout.addWidget(self._log)

    # ── Paths label ────────────────────────────────────────────────────────────
    def _update_paths(self):
        if not self.deployment:
            self._paths_lbl.setText('')
            return
        info = STAGE_INFO[self.stage_key]
        src = info['source_fn'](self.base_dir, self.deployment)
        out = info['output_fn'](self.base_dir, self.deployment)
        self._paths_lbl.setText(f'<b>Source:</b> {src}<br><b>Output:</b> {out}')

    # ── Status refresh ─────────────────────────────────────────────────────────
    def _refresh_status(self):
        if not self.deployment:
            self._badge.setText(STATUS_LABEL[STATUS_NOT_RUN])
            self._badge.setStyleSheet(
                STATUS_BADGE_STYLE[STATUS_NOT_RUN] +
                'border-radius:10px; padding:2px 8px; font-size:11px; font-weight:bold;'
            )
            return
        year    = self.deployment.split('-')[1][:4]
        dep_dir = os.path.join(self.base_dir, 'deployments', year, self.deployment)
        status  = load_state(dep_dir).get(self.stage_key, {}).get('status', STATUS_NOT_RUN)

        self._badge.setText(STATUS_LABEL[status])
        self._badge.setStyleSheet(
            STATUS_BADGE_STYLE[status] +
            'border-radius:10px; padding:2px 8px; font-size:11px; font-weight:bold;'
        )

        if not self._is_manual:
            is_running = (status == STATUS_RUNNING)
            self._run_btn.setVisible(not is_running)
            self._abort_btn.setVisible(is_running)
            if status == STATUS_DONE:
                self._run_btn.setText('↺  Re-run Stage')
            else:
                self._run_btn.setText('▶  Run Stage')

    # ── Log helpers ────────────────────────────────────────────────────────────
    def _append_log(self, line):
        if self._log is None:
            return
        self._log.setVisible(True)
        color = _line_color(line)
        self._log.moveCursor(QTextCursor.End)
        self._log.insertHtml(
            f'<span style="color:{color}; font-family:Consolas,monospace;">'
            f'{line.replace("&","&amp;").replace("<","&lt;")}</span><br>'
        )
        self._log.moveCursor(QTextCursor.End)

    def _clear_log(self):
        if self._log is not None:
            self._log.clear()

    # ── Manual stage handlers ──────────────────────────────────────────────────
    def _mark_done(self):
        if not self.deployment:
            return
        year    = self.deployment.split('-')[1][:4]
        dep_dir = os.path.join(self.base_dir, 'deployments', year, self.deployment)
        set_stage_status(dep_dir, self.stage_key, STATUS_DONE)
        self._refresh_status()

    def _mark_not_run(self):
        if not self.deployment:
            return
        year    = self.deployment.split('-')[1][:4]
        dep_dir = os.path.join(self.base_dir, 'deployments', year, self.deployment)
        set_stage_status(dep_dir, self.stage_key, STATUS_NOT_RUN)
        self._refresh_status()

    # ── Automated stage run / abort ────────────────────────────────────────────
    def _run(self):
        if not self.deployment or self._is_manual:
            return
        info = STAGE_INFO[self.stage_key]
        cmd, cwd, env = info['command_fn'](self.base_dir, self.deployment)

        year    = self.deployment.split('-')[1][:4]
        dep_dir = os.path.join(self.base_dir, 'deployments', year, self.deployment)
        set_stage_status(dep_dir, self.stage_key, STATUS_RUNNING)
        self._refresh_status()

        self._append_log(f'$ {" ".join(str(c) for c in cmd)}')
        self._append_log('─' * 60)

        self._worker = PipelineWorker(cmd, cwd=cwd, env=env, parent=self)
        self._worker.log_line.connect(self._append_log)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _abort(self):
        if self._worker and self._worker.isRunning():
            self._worker.abort()

    def _on_finished(self, success, message):
        year    = self.deployment.split('-')[1][:4]
        dep_dir = os.path.join(self.base_dir, 'deployments', year, self.deployment)
        status  = STATUS_DONE if success else STATUS_FAILED
        set_stage_status(dep_dir, self.stage_key, status)
        self._refresh_status()
        color = LOG_COLORS['info'] if success else LOG_COLORS['error']
        self._append_log('─' * 60)
        self._append_log(f'● {message}')

    # ── Public ────────────────────────────────────────────────────────────────
    def set_deployment(self, deployment):
        self.deployment = deployment
        self._update_paths()
        self._refresh_status()
        if self._log is not None:
            self._log.clear()
            self._log.setVisible(False)


class PipelinePage(QWidget):
    def __init__(self, base_dir, parent=None):
        super().__init__(parent)
        self.base_dir   = base_dir
        self.deployment = None
        self._cards     = {}
        self._build()

    # ── UI construction ────────────────────────────────────────────────────────
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(0)

        title = QLabel('Pipeline')
        title.setObjectName('page_title')
        outer.addWidget(title)

        self._sub = QLabel('Select a deployment from the sidebar to begin.')
        self._sub.setObjectName('page_subtitle')
        outer.addWidget(self._sub)

        outer.addSpacing(12)

        # Scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        self._stage_layout = QVBoxLayout(container)
        self._stage_layout.setSpacing(12)
        self._stage_layout.setContentsMargins(0, 0, 0, 0)

        for key in STAGE_INFO:
            card = StageCard(key, self.base_dir, None, self)
            self._cards[key] = card
            self._stage_layout.addWidget(card)

        self._stage_layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

    # ── Public ────────────────────────────────────────────────────────────────
    def set_deployment(self, deployment):
        self.deployment = deployment
        self._sub.setText(f'Deployment: {deployment}')
        for card in self._cards.values():
            card.set_deployment(deployment)
