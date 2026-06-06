#!/usr/bin/env python
"""
UVI Glider Processing Pipeline — Desktop Application
Entry point.

Run:
    python app/main.py
or from the pipeline root:
    python -m app.main
"""
import sys
import os

# Add the app directory to path so ui/pipeline/workers imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

from ui.main_window import MainWindow


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName('UVI Glider Pipeline')
    app.setOrganizationName('UVI')

    # Pipeline root = parent of this app/ directory
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    window = MainWindow(base_dir=base_dir)
    window.show()

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
