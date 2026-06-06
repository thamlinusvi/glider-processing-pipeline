"""
PipelineWorker — QThread that runs a subprocess and streams output
line-by-line back to the UI via Qt signals.
"""
import subprocess
from PyQt5.QtCore import QThread, pyqtSignal


class PipelineWorker(QThread):
    log_line  = pyqtSignal(str)          # one stdout/stderr line
    finished  = pyqtSignal(bool, str)    # (success, message)

    def __init__(self, command, cwd=None, env=None, parent=None):
        super().__init__(parent)
        self.command  = command
        self.cwd      = cwd
        self.env      = env
        self._abort   = False

    # ── public ────────────────────────────────────────────────────────────────
    def abort(self):
        self._abort = True

    # ── QThread.run ───────────────────────────────────────────────────────────
    def run(self):
        try:
            proc = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=self.cwd,
                env=self.env,
                bufsize=1,
            )
            for raw_line in proc.stdout:
                if self._abort:
                    proc.terminate()
                    proc.wait()
                    self.finished.emit(False, 'Aborted by user.')
                    return
                self.log_line.emit(raw_line.rstrip())

            proc.wait()
            if proc.returncode == 0:
                self.finished.emit(True, 'Completed successfully.')
            else:
                self.finished.emit(False, f'Process exited with code {proc.returncode}.')

        except FileNotFoundError as exc:
            self.finished.emit(False, f'Command not found: {exc}')
        except Exception as exc:
            self.finished.emit(False, str(exc))
