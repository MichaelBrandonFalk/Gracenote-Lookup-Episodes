import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event
from xml.sax.saxutils import escape

from gracenote_lookup.automation import main
from gracenote_lookup.avails import AvailsWorkbook, CELL, attribute, patch_cells
from gracenote_lookup.pagination import Episode, PaginationError
from gracenote_lookup.runtime import runtime, RunCancelled

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def sheet(headers, rows):
    def cell(col, row, value):
        ref = f'{col}{row}'
        if value == 'FORMULA':
            return f'<c r="{ref}" s="2"><f>IF(1=1,&quot;&quot;,&quot;x&quot;)</f><v/></c>'
        if not value:
            return f'<c r="{ref}" s="2"/>'
        return f'<c r="{ref}" s="2" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
    all_rows = [headers] + rows
    content = ''.join(f'<row r="{i}" hidden="{int(i == 4)}">' + ''.join(cell(chr(65+j), i, v) for j, v in enumerate(values)) + '</row>'
                      for i, values in enumerate(all_rows, 2))
    return (f'<worksheet xmlns="{NS}"><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetData>{content}</sheetData>'
            '<autoFilter ref="A2:F8"/><pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>').encode()


def fixture(filename, movies=None, tv=None):
    movies = movies if movies is not None else [['Example Movie', '2021', ''], ['Already Filled', '2020', 'MV000000000099']]
    tv = tv if tv is not None else [['Example Series', 'The Pilot', '1', '1', '', ''],
                                  ['Example Series', 'Finale', '1', '2', 'EP000000000099', ''],
                                  ['Example Series', 'Missing', '1', '3', '', 'SH000000000001']]
    members = {
        'xl/workbook.xml': f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                           '<sheet name="Movie" sheetId="1" r:id="rId1"/><sheet name="TV" sheetId="2" r:id="rId2"/></sheets></workbook>'.encode(),
        'xl/_rels/workbook.xml.rels': b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': sheet(['TitleDisplayUnlimited', 'ReleaseYear', 'RetailerID1'], movies),
        'xl/worksheets/sheet2.xml': sheet(['SeriesTitleDisplayUnlimited', 'EpisodeTitleDisplayUnlimited', 'SeasonNumber', 'EpisodeNumber', 'RetailerEpisodeID1', 'RetailerSeriesID'], tv),
        'xl/styles.xml': b'<styles>original formatting data</styles>',
        'xl/drawings/image.bin': b'original image bytes',
    }
    with zipfile.ZipFile(filename, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return members


class Browser:
    def __init__(self):
        self.calls = []

    def open_movie(self, title, year, manual):
        self.calls.append(('Movie', title, year, manual))
        return 'MV000000000001'

    def open_series(self, title, identifier, manual):
        self.calls.append(('TV', title, identifier, manual))
        return 'SH000000000001'

    def catalog(self, season):
        return (Episode('The Pilot', 'EP000000000001', '1'), Episode('Finale', 'EP000000000002', '2'))

    def all_episodes(self):
        return self.catalog('1')


class AvailsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'avails.xlsx'
        self.output = Path(self.temp.name) / 'results.xlsx'
        self.original = fixture(self.source)
        self.before = self.source.read_bytes()
        self.saved_runtime = vars(runtime).copy()
        runtime.cancelled = Event()
        runtime.log = lambda text: None
        runtime.rows = lambda rows: None
        runtime.progress = lambda current, total: None
        self.addCleanup(lambda: vars(runtime).update(self.saved_runtime))

    def lookup(self, browser=None, **options):
        return main(dict(input_csv=str(self.source), output_csv=str(self.output), **options), lambda: browser or Browser())

    def test_fills_only_blank_ids_preserves_existing_and_every_other_xml_byte(self):
        browser = Browser()
        result = self.lookup(browser)
        self.assertEqual((result['filled'], result['remaining'], result['review']), (4, 1, 1))
        self.assertEqual(self.source.read_bytes(), self.before)
        saved = AvailsWorkbook(self.output)
        self.assertEqual(saved.rows[1]['MovieTMSID'], 'MV000000000099')
        self.assertEqual(saved.rows[3]['EpisodeTMSID'], 'EP000000000099')
        self.assertEqual(saved.rows[4]['EpisodeTMSID'], '')
        book = AvailsWorkbook(self.source)
        for name, original in self.original.items():
            references = {ref for (sheet_name, ref) in book.targets if book.sheet_paths[sheet_name] == name}
            remove = lambda xml: CELL.sub(lambda m: b'' if attribute(m['attrs'], 'r') in references else m[0], xml)
            self.assertEqual(remove(original), remove(saved.members[name]), name)
        self.assertEqual(len(browser.calls), 2)  # Filled rows do not trigger searches.
        self.assertEqual(browser.calls[1][2], 'SH000000000001')  # Existing series ID reused.
        import csv
        with open(result['report'], encoding='utf-8-sig', newline='') as stream:
            report = list(csv.DictReader(stream))
        self.assertEqual(report[-1]['Status'], 'Needs review')

    def test_self_closing_cells_do_not_consume_the_following_cell(self):
        xml = b'<worksheet><sheetData><row r="4"><c r="A4" s="7"/><c r="B4"><v>3</v></c></row></sheetData></worksheet>'
        result = patch_cells(xml, {'A4': 'MV000000000001'})
        self.assertIn(b'<c r="B4"><v>3</v></c>', result)
        self.assertIn(b'r="A4" s="7"', result)

    def test_missing_target_cell_inserted_in_column_order(self):
        xml = b'<worksheet><sheetData><row r="4"><c r="A4"><v>1</v></c><c r="C4"><v>3</v></c></row></sheetData></worksheet>'
        result = patch_cells(xml, {'B4': 'EP000000000001'})
        self.assertLess(result.index(b'r="A4"'), result.index(b'r="B4"'))
        self.assertLess(result.index(b'r="B4"'), result.index(b'r="C4"'))

    def test_formula_with_blank_result_is_never_overwritten(self):
        fixture(self.source, movies=[['Formula Movie', '2021', 'FORMULA']], tv=[])
        book = AvailsWorkbook(self.source)
        self.assertEqual(book.targets, {})
        self.lookup()
        self.assertEqual(book.members, AvailsWorkbook(self.output).members)

    def test_skip_preserves_blank_cells_without_legacy_sentinel(self):
        browser = Browser()
        browser.open_movie = lambda *args, **kwargs: 'skip'
        browser.open_series = lambda *args, **kwargs: 'skip'
        result = self.lookup(browser, second_pass_only=True)
        self.assertEqual((result['filled'], result['skipped']), (0, 4))
        self.assertEqual(AvailsWorkbook(self.source).members, AvailsWorkbook(self.output).members)

    def test_ambiguous_movie_goes_to_manual_pass(self):
        browser = Browser()
        browser.open_movie = lambda title, year, manual: 'MV000000000001' if manual else ''
        result = self.lookup(browser)
        self.assertEqual(result['filled'], 4)

    def test_resume_reuses_ids_and_rejects_unrelated_workbook_edits(self):
        self.lookup()
        browser = Browser()
        self.lookup(browser)
        self.assertEqual([call[0] for call in browser.calls], ['TV'])  # Only unresolved episode remains.
        self.assertEqual(len(list(self.output.parent.glob('results.backup_*.xlsx'))), 1)
        book = AvailsWorkbook(self.output)
        book.members['xl/styles.xml'] = b'changed styles'
        with zipfile.ZipFile(self.output, 'w') as archive:
            for name, value in book.members.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(ValueError, 'unrelated workbook changes'):
            self.lookup()

    def test_stop_checkpoints_completed_ids(self):
        runtime.progress = lambda n, total: runtime.cancelled.set() if n == 1 else None
        with self.assertRaises(RunCancelled):
            self.lookup()
        book = AvailsWorkbook(self.output)
        self.assertEqual(book.rows[0]['MovieTMSID'], 'MV000000000001')
        self.assertEqual(book.rows[2]['SeriesTMSID'], '')

    def test_incomplete_pagination_keeps_episode_blank_but_saves_series(self):
        browser = Browser()
        browser.catalog = lambda season: (_ for _ in ()).throw(PaginationError('incomplete scan'))
        self.lookup(browser)
        saved = AvailsWorkbook(self.output)
        self.assertEqual(saved.rows[2]['EpisodeTMSID'], '')
        self.assertEqual(saved.rows[2]['SeriesTMSID'], 'SH000000000001')

    def test_input_output_same_path_cannot_overwrite(self):
        with self.assertRaises(ValueError):
            main(dict(input_csv=str(self.source), output_csv=str(self.source)))
        with self.assertRaises(ValueError):
            AvailsWorkbook(self.source).write(self.source)
        self.assertEqual(self.source.read_bytes(), self.before)


if __name__ == '__main__':
    unittest.main()
