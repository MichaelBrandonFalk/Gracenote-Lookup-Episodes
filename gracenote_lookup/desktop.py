"""Desktop UI. Browser work stays off the Qt event loop."""
import csv
from copy import deepcopy
from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt

from . import __version__
from .csv_io import read_csv
from .avails import preview
from .runtime import RunCancelled, runtime


class LookupWorker(QThread):
    message = Signal(str)
    prompt = Signal(str)
    progress = Signal(int, int)
    rows = Signal(object)
    result = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.reply_ready = Event()
        self.stop_requested = Event()
        self.answer = ""

    def respond(self, answer=""):
        self.answer = answer
        self.reply_ready.set()

    def stop(self):
        self.stop_requested.set()
        self.reply_ready.set()

    def ask(self, text):
        self.reply_ready.clear()
        self.prompt.emit(text)
        while not self.reply_ready.wait(0.2):
            runtime.check()
        runtime.check()
        return self.answer

    def run(self):
        runtime.cancelled = self.stop_requested
        runtime.log = self.message.emit
        runtime.ask = self.ask
        runtime.progress = self.progress.emit
        runtime.rows = lambda rows: self.rows.emit(deepcopy(rows))
        try:
            from .automation import main
            self.result.emit(main(self.config))
        except RunCancelled:
            self.stopped.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            # Do not retain deleted Qt signal receivers between runs.
            runtime.log = lambda text: None
            runtime.ask = lambda text: ""
            runtime.progress = lambda current, total: None
            runtime.rows = lambda rows: None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Gracenote Episode Lookup · v{__version__}")
        self.resize(1140, 820)
        self.worker = None
        self.close_after_stop = False
        self.output_path = None
        self.all_rows = []
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        title = QLabel("Gracenote Episode Lookup")
        title.setObjectName("title")
        heading.addWidget(title)
        heading.addStretch()
        version = QLabel(f"v{__version__}  /  macOS")
        version.setObjectName("muted")
        heading.addWidget(version)
        layout.addLayout(heading)
        subtitle = QLabel("Choose an avails workbook or episode CSV, sign in to Gracenote, and review the results here.")
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)

        self.settings = QFrame()
        self.settings.setObjectName("panel")
        grid = QGridLayout(self.settings)
        grid.setContentsMargins(18, 18, 18, 18)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Choose an XLSX workbook or CSV…")
        self.input.setAccessibleName("Input workbook or CSV")
        browse_input = QPushButton("Choose file")
        browse_input.clicked.connect(self.choose_input)
        self.output = QLineEdit()
        self.output.setPlaceholderText("Choose where to save the results…")
        self.output.setAccessibleName("Output workbook or CSV")
        browse_output = QPushButton("Save as…")
        browse_output.clicked.connect(self.choose_output)
        grid.addWidget(QLabel("Input"), 0, 0)
        grid.addWidget(self.input, 0, 1, 1, 3)
        grid.addWidget(browse_input, 0, 4)
        grid.addWidget(QLabel("Output"), 1, 0)
        grid.addWidget(self.output, 1, 1, 1, 3)
        grid.addWidget(browse_output, 1, 4)
        self.mode = QComboBox()
        self.mode.addItems(["Automatic, then resolve choices", "Manual program selection"])
        self.mode.setAccessibleName("Lookup mode")
        self.ignore_season = QCheckBox("Search every season")
        self.ignore_season.setToolTip("Check every available season; duplicate titles are flagged for review.")
        self.resume = QCheckBox("Resume from existing output")
        self.resume.setChecked(True)
        grid.addWidget(QLabel("Mode"), 2, 0)
        grid.addWidget(self.mode, 2, 1)
        grid.addWidget(self.ignore_season, 2, 2)
        grid.addWidget(self.resume, 2, 3, 1, 2)
        template = QPushButton("Download CSV template")
        template.clicked.connect(self.save_template)
        hint = QLabel("Avails XLSX: fill blank movie, series and episode IDs. Existing values and formatting are preserved.\nEpisode CSV: SeriesTitle, EpisodeTitle, Season; other columns are retained.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        grid.addWidget(hint, 3, 0, 1, 4)
        grid.addWidget(template, 3, 4)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(3, 1)
        layout.addWidget(self.settings)

        actions = QHBoxLayout()
        self.start = QPushButton("Start lookup")
        self.start.setObjectName("primary")
        self.start.clicked.connect(self.start_lookup)
        self.stop = QPushButton("Stop and save")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.stop_lookup)
        self.open_output = QPushButton("Open results")
        self.open_output.setEnabled(False)
        self.open_output.clicked.connect(self.open_results)
        self.open_report = QPushButton("Open review report")
        self.open_report.setEnabled(False)
        self.open_report.clicked.connect(self.open_review_report)
        actions.addWidget(self.start)
        actions.addWidget(self.stop)
        actions.addStretch()
        actions.addWidget(self.open_output)
        actions.addWidget(self.open_report)
        layout.addLayout(actions)

        self.status = QLabel("Ready · Chrome must be installed. You sign in directly in the browser.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        layout.addWidget(self.progress)

        self.prompt_panel = QFrame()
        self.prompt_panel.setObjectName("prompt")
        prompt_layout = QHBoxLayout(self.prompt_panel)
        self.prompt_text = QLabel()
        self.prompt_text.setWordWrap(True)
        prompt_layout.addWidget(self.prompt_text, 1)
        self.continue_button = QPushButton("Continue")
        self.continue_button.setObjectName("primary")
        self.continue_button.clicked.connect(lambda: self.respond(""))
        self.skip_button = QPushButton("Skip this program")
        self.skip_button.clicked.connect(lambda: self.respond("skip"))
        prompt_layout.addWidget(self.continue_button)
        prompt_layout.addWidget(self.skip_button)
        self.prompt_panel.hide()
        layout.addWidget(self.prompt_panel)

        results_heading = QHBoxLayout()
        self.row_count = QLabel("Results · no file selected")
        self.row_count.setObjectName("section")
        self.review_only = QCheckBox("Show rows needing review")
        self.review_only.toggled.connect(self.render_rows)
        results_heading.addWidget(self.row_count)
        results_heading.addStretch()
        results_heading.addWidget(self.review_only)
        layout.addLayout(results_heading)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Movie / Series", "Episode", "Season", "Series TMS ID", "Movie / Episode ID", "Notes"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate([180, 210, 65, 135, 140, 260]):
            self.table.setColumnWidth(col, width)
        self.table.setAccessibleName("Lookup results")
        splitter.addWidget(self.table)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Lookup activity appears here. Progress is saved after every row.")
        self.log.document().setMaximumBlockCount(2000)
        self.log.setAccessibleName("Lookup activity")
        splitter.addWidget(self.log)
        splitter.setSizes([340, 130])
        layout.addWidget(splitter, 1)

    def choose_input(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Choose avails workbook or episode CSV", str(Path.home()), "Supported files (*.xlsx *.csv)")
        if filename:
            self.input.setText(filename)
            path = Path(filename)
            self.output.setText(str(path.with_name(path.stem + "_with_tms_ids" + path.suffix.lower())))
            try:
                self.update_rows(preview(filename))
                blanks = sum(len(r.get('_blank_fields', [])) for r in self.all_rows)
                self.status.setText(f"Ready · {len(self.all_rows)} rows loaded." + (f" {blanks} blank ID cells to look up." if blanks else ""))
            except Exception as exc:
                self.update_rows([])
                QMessageBox.warning(self, "Check your input file", str(exc))

    def choose_output(self):
        extension = '.xlsx' if Path(self.input.text()).suffix.lower() == '.xlsx' else '.csv'
        filename, _ = QFileDialog.getSaveFileName(self, "Save lookup results", self.output.text(), f"Results (*{extension})")
        if filename:
            self.output.setText(filename if filename.lower().endswith(extension) else filename + extension)

    def save_template(self):
        filename, _ = QFileDialog.getSaveFileName(self, "Save CSV template", str(Path.home() / "InputEpisodes.csv"), "CSV files (*.csv)")
        if filename:
            try:
                with open(filename, "w", newline="", encoding="utf-8-sig") as stream:
                    csv.writer(stream).writerow(["SeriesTitle", "EpisodeTitle", "Season", "EpisodeNumber", "Part", "SeriesTMSID", "EpisodeTMSID", "Notes"])
                self.status.setText(f"Template saved · {filename}")
            except OSError as exc:
                QMessageBox.warning(self, "Could not save template", str(exc))

    def start_lookup(self):
        if self.worker and self.worker.isRunning():
            return
        try:
            if not self.input.text().strip() or not self.output.text().strip():
                raise ValueError("Choose both an input file and an output location.")
            source = Path(self.input.text()).expanduser().resolve()
            target = Path(self.output.text()).expanduser().resolve()
            if source == target:
                raise ValueError("Choose a different output file to preserve the input.")
            if source.suffix.lower() not in ('.csv', '.xlsx') or target.suffix.lower() != source.suffix.lower():
                raise ValueError("Choose CSV or XLSX input and the same file type for output.")
            self.update_rows(preview(source))
        except Exception as exc:
            QMessageBox.warning(self, "Check your files", str(exc))
            return
        if self.worker:
            self.worker.deleteLater()
        self.output_path = target
        self.log.clear()
        self.status.setText("Starting lookup · opening Chrome…")
        self.settings.setEnabled(False)
        self.start.setEnabled(False)
        self.stop.setEnabled(True)
        self.open_output.setEnabled(False)
        self.open_report.setEnabled(False)
        self.progress.setRange(0, 0)
        self.worker = LookupWorker({
            "input_csv": str(source), "output_csv": str(target),
            "second_pass_only": self.mode.currentIndex() == 1,
            "ignore_season_search": self.ignore_season.isChecked(),
            "resume_existing": self.resume.isChecked(), "wait_timeout": 15,
        }, self)
        self.worker.message.connect(self.log.appendPlainText)
        self.worker.prompt.connect(self.show_prompt)
        self.worker.progress.connect(self.update_progress)
        self.worker.rows.connect(self.update_rows)
        self.worker.result.connect(self.completed)
        self.worker.failed.connect(self.failed)
        self.worker.stopped.connect(lambda: self.status.setText("Stopped · completed results were saved."))
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def show_prompt(self, text):
        if self.worker.stop_requested.is_set():
            return
        self.prompt_text.setText(text.replace("press ENTER here", "click Continue").replace("press ENTER", "click Continue"))
        self.skip_button.setVisible("skip" in text.lower())
        self.prompt_panel.show()
        self.status.setText("Waiting for you · complete the browser step, then continue here.")

    def respond(self, text):
        if self.worker:
            self.worker.respond(text)
        self.prompt_panel.hide()
        self.status.setText("Lookup in progress · saving results after each row.")

    def stop_lookup(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.stop.setEnabled(False)
            self.prompt_panel.hide()
            self.status.setText("Stopping and saving · waiting for the current browser operation…")

    def update_progress(self, current, total):
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(current if total else 1)
        if total:
            self.status.setText(f"Lookup in progress · {current} of {total} rows in this pass.")

    def update_rows(self, rows):
        self.all_rows = rows
        self.render_rows()

    def render_rows(self):
        from .matching import _value_is_one, extract_tms_id
        rows = self.all_rows
        if self.review_only.isChecked():
            def needs_review(row):
                if 'Kind' in row:
                    return not row.get('_skip') and any(not row.get(f) for f in row['_blank_fields'])
                return (not extract_tms_id(row.get('EpisodeTMSID', ''), 'EP') and
                        not _value_is_one(row.get('EpisodeTMSID')) and not _value_is_one(row.get('SeriesTMSID')))
            rows = [r for r in rows if needs_review(r)]
        shown = rows[:500]
        self.table.setRowCount(len(shown))
        for index, row in enumerate(shown):
            values = [row.get('MovieTitle') or row.get('SeriesTitle', ''), row.get('EpisodeTitle', ''),
                      row.get('Season', ''), row.get('SeriesTMSID', ''),
                      row.get('MovieTMSID') or row.get('EpisodeTMSID', ''), row.get('Notes', '')]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                location = f"{row['_sheet']} · row {row['_row']}\n" if '_sheet' in row else ''
                item.setToolTip(location + value)
                self.table.setItem(index, col, item)
        suffix = " · showing first 500; output includes every row" if len(rows) > 500 else ""
        self.row_count.setText(f"Results · {len(rows)} rows{suffix}")

    def completed(self, result):
        text = (f"Finished · {result['filled']} blank ID cells filled · {result['remaining']} remain blank · {result['review']} rows need review"
                if 'filled' in result else f"Finished · {result['found']} found · {result['skipped']} skipped · {result['review']} need review")
        self.status.setText(text)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)

    def failed(self, message):
        self.status.setText(f"Lookup stopped · {message}")
        self.log.appendPlainText(f"Error: {message}")
        QMessageBox.warning(self, "Lookup needs attention", message)

    def worker_finished(self):
        self.settings.setEnabled(True)
        self.start.setEnabled(True)
        self.stop.setEnabled(False)
        self.prompt_panel.hide()
        self.open_output.setEnabled(bool(self.output_path and self.output_path.exists()))
        report = self.report_path()
        self.open_report.setEnabled(bool(report and report.exists()))
        if self.close_after_stop:
            self.close()

    def open_results(self):
        if self.output_path and self.output_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_path)))

    def report_path(self):
        if self.output_path and self.output_path.suffix.lower() == '.xlsx':
            return self.output_path.with_name(self.output_path.stem + '_lookup_report.csv')

    def open_review_report(self):
        report = self.report_path()
        if report and report.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(report)))

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            choice = QMessageBox.question(self, "Stop lookup?", "Stop the lookup, save progress, and close the app?",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            event.ignore()
            if choice == QMessageBox.StandardButton.Yes:
                self.close_after_stop = True
                self.stop_lookup()
        else:
            event.accept()


def main():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Gracenote Episode Lookup")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("MichaelBrandonFalk")
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { font-family: '.AppleSystemUIFont', 'Segoe UI', sans-serif; font-size: 13px; color: #20362f; }
        QMainWindow, QWidget#central { background: #f5f7f5; }
        QLabel#title { font-size: 26px; font-weight: 700; }
        QLabel#muted { color: #66756e; font-size: 12px; }
        QLabel#section { font-weight: 600; }
        QFrame#panel { background: #ffffff; border: 1px solid #dce4de; border-radius: 10px; }
        QFrame#prompt { background: #edf4dd; border: 1px solid #c9dba9; border-radius: 8px; }
        QLineEdit, QComboBox { background: white; border: 1px solid #ccd8d0; border-radius: 6px; padding: 8px; }
        QPushButton { background: #fff; border: 1px solid #ccd8d0; border-radius: 6px; padding: 9px 14px; }
        QPushButton:hover { background: #eef4ee; }
        QPushButton#primary { background: #21664d; color: white; border: 1px solid #21664d; font-weight: 600; }
        QPushButton#primary:hover { background: #194f3c; }
        QPushButton:disabled { color: #93a098; background: #edf0ed; border-color: #dce4de; }
        QTableWidget, QPlainTextEdit { background: white; border: 1px solid #dce4de; border-radius: 6px; gridline-color: #eef2ed; }
        QHeaderView::section { background: #eef3ef; border: 0; border-right: 1px solid #dce4de; padding: 8px; font-weight: 600; }
        QProgressBar { border: 0; background: #e0e8e0; border-radius: 4px; }
        QProgressBar::chunk { background: #21664d; border-radius: 4px; }
    """)
    window = MainWindow()
    window.centralWidget().setObjectName("central")
    window.show()
    # Used by release CI to verify the actual frozen app, Qt event loop,
    # worker signals and CSV output without opening Chrome or needing login.
    import sys
    if '--self-test' in sys.argv:
        import tempfile
        from PySide6.QtCore import QTimer
        from .csv_io import write_csv
        temp = tempfile.TemporaryDirectory()
        source = Path(temp.name) / 'input.csv'
        output = Path(temp.name) / 'output.csv'
        sample = [{'SeriesTitle': 'Example Series', 'EpisodeTitle': 'The Pilot', 'Season': '1',
                   'EpisodeTMSID': 'EP000000000001'},
                  {'SeriesTitle': 'Example Series', 'EpisodeTitle': 'Finale', 'Season': '1',
                   'EpisodeTMSID': '1'}]
        write_csv(source, sample, list(sample[0]))
        window.input.setText(str(source))
        window.output.setText(str(output))
        def finish_test():
            try:
                from .avails import AvailsWorkbook
                saved = AvailsWorkbook(output.with_suffix('.xlsx'))
                assert len(saved.rows) == 2 and not saved.targets
                assert window.open_report.isEnabled()
                assert '0 blank ID cells filled' in window.status.text()
                index = sys.argv.index('--self-test')
                if index + 1 < len(sys.argv):
                    path = Path(sys.argv[index + 1]).resolve()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    assert window.grab().save(str(path))
                print(f'Packaged UI, CSV and XLSX worker checks passed · v{__version__}')
                app.exit(0)
            except Exception as exc:
                print(f'Self-test failed: {exc}', file=sys.stderr)
                app.exit(1)
            finally:
                temp.cleanup()

        def verify():
            try:
                saved = read_csv(output)
                assert len(saved) == 2 and saved[0]['EpisodeTMSID'] == 'EP000000000001'
                assert window.start.isEnabled() and window.open_output.isEnabled()
                assert '1 found' in window.status.text() and '1 skipped' in window.status.text()
                from .self_test import create_workbook
                create_workbook(source.with_suffix('.xlsx'))
                window.input.setText(str(source.with_suffix('.xlsx')))
                window.output.setText(str(output.with_suffix('.xlsx')))
                window.start_lookup()
                window.worker.finished.connect(lambda: QTimer.singleShot(0, finish_test))
            except Exception as exc:
                print(f'Self-test failed: {exc}', file=sys.stderr)
                temp.cleanup()
                app.exit(1)
        def begin():
            window.start_lookup()
            window.worker.finished.connect(lambda: QTimer.singleShot(0, verify))
        QTimer.singleShot(0, begin)
        QTimer.singleShot(30000, lambda: app.exit(2))
    return app.exec()
