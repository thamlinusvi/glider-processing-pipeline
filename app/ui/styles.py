STYLESHEET = """
/* ── Global ──────────────────────────────────────────────────────── */
QMainWindow, QDialog {
    background: #f0f2f5;
}
QWidget {
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 13px;
    color: #2c3e50;
}

/* ── Sidebar ─────────────────────────────────────────────────────── */
#sidebar {
    background: #1a2535;
    border-right: 1px solid #111d2b;
}

/* Brand block at the top of the sidebar */
#app_title {
    color: #ffffff;
    font-size: 16px;
    font-weight: bold;
    padding: 20px 16px 2px 16px;
    background: #1a2535;
    letter-spacing: 0.5px;
}
#app_subtitle {
    color: #5d7a99;
    font-size: 10px;
    padding: 0px 16px 16px 16px;
    background: #1a2535;
    letter-spacing: 1px;
    text-transform: uppercase;
}

/* Deployment selector */
#deployment_label {
    color: #4a6580;
    font-size: 9px;
    padding: 12px 16px 4px 16px;
    background: #1a2535;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    font-weight: bold;
}
#deployment_combo {
    background: #243447;
    color: #c8d8e8;
    border: 1px solid #2f4560;
    border-radius: 5px;
    padding: 6px 10px;
    margin: 0px 12px 16px 12px;
    font-size: 12px;
}
#deployment_combo::drop-down {
    border: none;
    width: 20px;
}
#deployment_combo QAbstractItemView {
    background: #243447;
    color: #c8d8e8;
    border: 1px solid #2f4560;
    selection-background-color: #2980b9;
    selection-color: white;
    outline: none;
}

/* Navigation list */
#nav_list {
    background: #1a2535;
    border: none;
    outline: none;
    padding: 4px 0px;
}
#nav_list::item {
    color: #7fa3c4;
    padding: 11px 16px;
    border-left: 3px solid transparent;
    border-radius: 0px;
    font-size: 13px;
}
#nav_list::item:hover {
    background: #22334a;
    color: #c8d8e8;
}
#nav_list::item:selected {
    background: #1e3552;
    color: #ffffff;
    border-left: 3px solid #3498db;
    font-weight: 600;
}

/* ── Content area ────────────────────────────────────────────────── */
#content {
    background: #f0f2f5;
}
#page_title {
    font-size: 22px;
    font-weight: 700;
    color: #1a2535;
    padding: 0px;
    margin-bottom: 2px;
    letter-spacing: -0.3px;
}
#page_subtitle {
    font-size: 12px;
    color: #8a9bac;
    margin-bottom: 0px;
    padding-bottom: 0px;
}

/* ── Stage cards (QGroupBox) ─────────────────────────────────────── */
QGroupBox {
    background: white;
    border: 1px solid #dde4ec;
    border-radius: 8px;
    margin-top: 20px;
    padding: 12px 16px 16px 16px;
    font-weight: 600;
    font-size: 13px;
    color: #1a2535;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 4px 14px;
    left: 14px;
    color: #1a2535;
    background: #eef3f8;
    border: 1px solid #c8d8e8;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.2px;
}

/* ── Deployment card (dashboard) ─────────────────────────────────── */
#deployment_card {
    background: white;
    border: 1px solid #dde4ec;
    border-radius: 8px;
}
#deployment_card:hover {
    border-color: #3498db;
    border-left: 3px solid #3498db;
}

/* ── Buttons ─────────────────────────────────────────────────────── */
QPushButton {
    background: #2e86c1;
    color: white;
    border: none;
    border-radius: 5px;
    padding: 7px 16px;
    font-weight: 600;
    font-size: 12px;
    letter-spacing: 0.2px;
}
QPushButton:hover   { background: #2471a3; }
QPushButton:pressed { background: #1f618d; }
QPushButton:disabled { background: #c8d0d8; color: #9eabb5; }

QPushButton#btn_secondary {
    background: #edf0f3;
    color: #4a5568;
    border: 1px solid #c8d0d8;
}
QPushButton#btn_secondary:hover   { background: #dde3ea; border-color: #b0bcc8; }

QPushButton#btn_danger {
    background: #e74c3c;
}
QPushButton#btn_danger:hover { background: #cb4335; }

QPushButton#btn_success {
    background: #27ae60;
}
QPushButton#btn_success:hover { background: #229954; }

/* ── Log pane ────────────────────────────────────────────────────── */
#log_pane {
    background: #141e2e;
    color: #7ec8a0;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 11px;
    border: none;
    border-radius: 6px;
    padding: 10px 12px;
}

/* ── Tables ──────────────────────────────────────────────────────── */
QTableWidget {
    background: white;
    border: 1px solid #dde4ec;
    border-radius: 6px;
    gridline-color: #edf0f3;
    selection-background-color: #d6eaf8;
    selection-color: #1a2535;
    alternate-background-color: #f8fafc;
}
QTableWidget::item { padding: 5px 8px; }
QHeaderView::section {
    background: #f4f7fb;
    border: none;
    border-bottom: 2px solid #dde4ec;
    border-right: 1px solid #dde4ec;
    padding: 7px 10px;
    font-weight: 600;
    color: #4a5568;
    font-size: 12px;
}

/* ── Text inputs ─────────────────────────────────────────────────── */
QLineEdit, QTextEdit, QPlainTextEdit {
    border: 1px solid #c8d4e0;
    border-radius: 5px;
    padding: 6px 10px;
    background: white;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border-color: #3498db;
    outline: none;
}

/* ── Tab widget ──────────────────────────────────────────────────── */
QTabWidget::pane {
    border: 1px solid #dde4ec;
    border-radius: 0 6px 6px 6px;
    background: white;
}
QTabBar::tab {
    background: #edf0f3;
    border: 1px solid #dde4ec;
    border-bottom: none;
    padding: 8px 18px;
    margin-right: 3px;
    border-radius: 5px 5px 0 0;
    color: #7a8fa4;
    font-size: 12px;
}
QTabBar::tab:selected {
    background: white;
    color: #1a2535;
    font-weight: 600;
    border-top: 2px solid #3498db;
}
QTabBar::tab:hover { color: #2c3e50; background: #e4e9ef; }

/* ── Scroll bars ─────────────────────────────────────────────────── */
QScrollBar:vertical {
    background: transparent; width: 7px; margin: 0;
}
QScrollBar::handle:vertical {
    background: #c0ccd8; border-radius: 4px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #9aafc2; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QScrollBar:horizontal {
    background: transparent; height: 7px; margin: 0;
}
QScrollBar::handle:horizontal {
    background: #c0ccd8; border-radius: 4px; min-width: 30px;
}
QScrollBar::handle:horizontal:hover { background: #9aafc2; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ── Splitter ────────────────────────────────────────────────────── */
QSplitter::handle { background: #dde4ec; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical   { height: 1px; }

/* ── Status bar ──────────────────────────────────────────────────── */
QStatusBar {
    background: #141e2e;
    color: #4a6580;
    font-size: 11px;
    border-top: 1px solid #0d1620;
}

/* ── Scroll area ─────────────────────────────────────────────────── */
QScrollArea {
    border: none;
    background: transparent;
}
"""
