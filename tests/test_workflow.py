import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from gracenote_lookup.automation import main
from gracenote_lookup.csv_io import read_csv, write_csv
from gracenote_lookup.pagination import Episode, PaginationError
from gracenote_lookup.runtime import runtime, RunCancelled


class Browser:
    def __init__(self, selection='SH000000000001', automatic=True):
        self.selection = selection
        self.automatic = automatic
        self.calls = []

    def open_series(self, title, identifier, manual):
        self.calls.append((title, identifier, manual))
        return self.selection if self.automatic or manual else ''

    def catalog(self, season):
        return (Episode('The Pilot', 'EP000000000001'), Episode('Finale', 'EP000000000002'))

    def all_episodes(self):
        return self.catalog('1')


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'input.csv'
        self.output = Path(self.temp.name) / 'output.csv'
        self.rows = [dict(SeriesTitle='Example', EpisodeTitle=title, Season='1') for title in ('The Pilot', 'Finale')]
        write_csv(self.source, self.rows, list(self.rows[0]))
        self.saved_runtime = vars(runtime).copy()
        runtime.cancelled = Event()
        runtime.log = lambda text: None
        runtime.rows = lambda rows: None
        runtime.progress = lambda current, total: None
        self.addCleanup(lambda: vars(runtime).update(self.saved_runtime))

    def run_lookup(self, browser, **options):
        return main(dict(input_csv=str(self.source), output_csv=str(self.output), **options), lambda: browser)

    def test_automatic_then_manual_second_pass(self):
        browser = Browser(automatic=False)
        result = self.run_lookup(browser)
        self.assertEqual(result['found'], 2)
        self.assertEqual([call[2] for call in browser.calls], [False, True])
        self.assertEqual([row['EpisodeTMSID'] for row in read_csv(self.output)], ['EP000000000001', 'EP000000000002'])

    def test_skip_is_counted_separately_from_found(self):
        result = self.run_lookup(Browser(selection='skip'), second_pass_only=True)
        self.assertEqual((result['found'], result['skipped'], result['review']), (0, 2, 0))

    def test_resume_does_not_launch_browser_for_completed_rows(self):
        self.run_lookup(Browser())
        with patch('gracenote_lookup.automation.webdriver.Chrome') as chrome:
            result = main(dict(input_csv=str(self.source), output_csv=str(self.output)))
        chrome.assert_not_called()
        self.assertEqual(result['found'], 2)
        self.assertEqual(len(list(self.output.parent.glob('output.backup_*.csv'))), 1)

    def test_cancellation_preserves_completed_rows(self):
        def progress(current, total):
            if current == 1:
                runtime.cancelled.set()
        runtime.progress = progress
        with self.assertRaises(RunCancelled):
            self.run_lookup(Browser())
        saved = read_csv(self.output)
        self.assertEqual(saved[0]['EpisodeTMSID'], 'EP000000000001')
        self.assertEqual(saved[1]['EpisodeTMSID'], '')

    def test_pagination_failure_stays_blank_and_reports_incomplete_scan(self):
        browser = Browser()
        browser.catalog = lambda season: (_ for _ in ()).throw(PaginationError('incomplete scan'))
        result = self.run_lookup(browser)
        self.assertEqual(result['review'], 2)
        self.assertEqual(read_csv(self.output)[0]['EpisodeTMSID'], '')
        self.assertIn('incomplete', read_csv(self.output)[0]['Notes'])

    def test_same_input_output_rejected(self):
        original = self.source.read_bytes()
        with self.assertRaises(ValueError):
            main(dict(input_csv=str(self.source), output_csv=str(self.source)))
        self.assertEqual(self.source.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
