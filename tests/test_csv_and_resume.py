import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gracenote_lookup.csv_io import read_csv, write_csv
from gracenote_lookup.matching import (_value_is_one, handled, merge_existing,
                                      normalize_season_value, valid_id)


def row(**changes):
    result = dict(SeriesTitle='Example', EpisodeTitle='The Pilot', Season='1',
                  SeriesTMSID='', EpisodeTMSID='', EpisodeNumber='', Part='', Notes='')
    result.update(changes)
    return result


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'episodes.csv'

    def test_bom_aliases_and_extra_columns_round_trip(self):
        self.path.write_text('\ufeffSeries Title,Episode Title,Season,CustomerID\r\nExample,"Pilot, The",01,abc\r\n', encoding='utf-8')
        rows = read_csv(self.path)
        self.assertEqual(rows[0]['SeriesTitle'], 'Example')
        write_csv(self.path, rows, list(rows[0]))
        self.assertEqual(read_csv(self.path)[0]['CustomerID'], 'abc')
        self.assertEqual(read_csv(self.path)[0]['Episode Title'], 'Pilot, The')

    def test_invalid_and_empty_csvs(self):
        for value in ('SeriesTitle,SeriesTitle,EpisodeTitle\nx,x,y\n',
                      'SeriesTitle,EpisodeTitle\nx,y,z\n', 'Wrong,EpisodeTitle\nx,y\n',
                      'SeriesTitle,EpisodeTitle\n'):
            with self.subTest(value=value):
                self.path.write_text(value)
                with self.assertRaises(ValueError):
                    read_csv(self.path)

    def test_failed_checkpoint_preserves_previous_output(self):
        self.path.write_text('previous result')
        with patch('gracenote_lookup.csv_io.os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                write_csv(self.path, [row()], list(row()))
        self.assertEqual(self.path.read_text(), 'previous result')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_real_series_id_does_not_skip_episode(self):
        self.assertFalse(handled(row(SeriesTMSID='SH000000000001')))
        self.assertTrue(handled(row(EpisodeTMSID='EP000000000001')))
        self.assertTrue(handled(row(SeriesTMSID='1.0')))
        self.assertFalse(valid_id('EP123', 'EP'))
        self.assertTrue(_value_is_one('01.00'))
        self.assertEqual(normalize_season_value('No Season'), '0')
        self.assertEqual(normalize_season_value('Season 01'), '1')

    def test_resume_preserves_input_id_and_only_merges_unique_results(self):
        rows = [row(EpisodeTMSID='EP000000000009')]
        previous = [row(SeriesTMSID='SH000000000001', EpisodeTMSID='EP000000000001')]
        merge_existing(rows, previous)
        self.assertEqual(rows[0]['EpisodeTMSID'], 'EP000000000009')
        rows = [row(Season='01', EpisodeTitle='Pilot, The')]
        merge_existing(rows, previous)
        self.assertEqual(rows[0]['EpisodeTMSID'], 'EP000000000001')
        rows = [row()]
        previous.append(row(SeriesTMSID='SH000000000002', EpisodeTMSID='EP000000000002'))
        self.assertEqual(merge_existing(rows, previous), {})
        self.assertEqual(rows[0]['EpisodeTMSID'], '')

    def test_resume_respects_supplied_series_and_episode_numbers(self):
        rows = [row(SeriesTMSID='SH000000000002', EpisodeNumber='2')]
        previous = [row(SeriesTMSID='SH000000000001', EpisodeNumber='2', EpisodeTMSID='EP000000000001')]
        merge_existing(rows, previous)
        self.assertEqual(rows[0]['EpisodeTMSID'], '')


if __name__ == '__main__':
    unittest.main()
