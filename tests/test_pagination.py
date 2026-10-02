import unittest
from unittest.mock import patch

from gracenote_lookup.matching import canonicalize_title_for_match
from gracenote_lookup.pagination import (Episode, PaginationError, Snapshot,
                                        SnapshotUnavailable, collect_pages, match_episode, move, parse_range)


class Clock:
    now = 0

    def sleep(self, seconds):
        self.now += seconds


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.clock.now = 0
        self.sleep = patch('gracenote_lookup.pagination.time.sleep', self.clock.sleep)
        self.monotonic = patch('gracenote_lookup.pagination.time.monotonic', lambda: self.clock.now)
        self.sleep.start()
        self.monotonic.start()
        self.addCleanup(self.sleep.stop)
        self.addCleanup(self.monotonic.stop)

    def page(self, low, high, total, previous=None, next=None):
        return Snapshot(f'{low} - {high} of {total}',
                        tuple(Episode(f'Episode {n}', f'EP{n:012d}') for n in range(low, high + 1)), previous, next)

    def test_walk_resets_from_middle_and_collects_every_page(self):
        pages = [self.page(1, 2, 5, next=1), self.page(3, 4, 5, previous=-1, next=1),
                 self.page(5, 5, 5, previous=-1)]
        index = 1
        clicks = []
        def click(direction):
            nonlocal index
            clicks.append(direction)
            index += direction
        episodes = collect_pages(lambda: pages[index], click)
        self.assertEqual([ep.title for ep in episodes], [f'Episode {n}' for n in range(1, 6)])
        self.assertEqual(clicks, [-1, 1, 1])

    def test_range_changes_before_rows_and_table_disappears(self):
        before = self.page(1, 2, 4, next='next')
        after = self.page(3, 4, 4, previous='previous')
        mixed = Snapshot(after.marker, before.episodes)
        values = [SnapshotUnavailable('loading'), mixed, after, after]
        def read():
            value = values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        clicks = []
        self.assertEqual(move(before, 'next', read, clicks.append), after)
        self.assertEqual(clicks, ['next'])

    def test_stalled_page_raises_and_does_not_click_again(self):
        before = self.page(1, 2, 4, next='next')
        clicks = []
        with self.assertRaisesRegex(PaginationError, 'incomplete'):
            move(before, 'next', lambda: before, clicks.append, timeout=1)
        self.assertEqual(clicks, ['next'])

    def test_missing_enabled_next_is_not_end_of_scan(self):
        with self.assertRaisesRegex(PaginationError, 'No enabled next'):
            collect_pages(lambda: self.page(1, 2, 4), lambda control: None)

    def test_no_old_forty_page_limit(self):
        index = 0
        pages = [self.page(n, n, 45, -1 if n > 1 else None, 1 if n < 45 else None) for n in range(1, 46)]
        def click(direction):
            nonlocal index
            index += direction
        self.assertEqual(len(collect_pages(lambda: pages[index], click)), 45)

    def test_padded_last_page_unicode_and_thousands(self):
        self.assertEqual(parse_range('21 - 40 of 28'), (21, 28, 28))
        self.assertEqual(parse_range('1,001 – 1,020 of 1,015'), (1001, 1015, 1015))
        self.assertEqual(parse_range('0 - 0 of 0'), (0, 0, 0))
        with self.assertRaises(PaginationError):
            parse_range('Help: 1 - 20 of 28')
        with self.assertRaises(PaginationError):
            parse_range('41 - 60 of 28')

    def test_incomplete_rows_are_not_accepted(self):
        page = Snapshot('1 - 2 of 2', (Episode('Only one', 'EP000000000001'),))
        with self.assertRaisesRegex(PaginationError, 'incomplete'):
            collect_pages(lambda: page, lambda control: None, timeout=1)

    def test_exact_match_beats_earlier_fuzzy_title(self):
        episodes = [Episode('The Return', 'EP000000000001'), Episode('Return, The', 'EP000000000002')]
        self.assertEqual(match_episode('The Return', episodes, canonicalize_title_for_match)[0], '')
        episodes[0] = Episode('The Return Home', 'EP000000000001')
        self.assertEqual(match_episode('The Return', episodes, canonicalize_title_for_match)[0], 'EP000000000002')

    def test_duplicate_titles_require_number_or_part(self):
        episodes = [Episode('Pilot', 'EP000000000001', '1', '1 of 2'), Episode('Pilot', 'EP000000000002', '2', '2 of 2')]
        self.assertEqual(match_episode('Pilot', episodes, canonicalize_title_for_match)[0], '')
        self.assertEqual(match_episode('Pilot', episodes, canonicalize_title_for_match, '02')[0], 'EP000000000002')
        self.assertEqual(match_episode('Pilot', episodes, canonicalize_title_for_match, part='1')[0], 'EP000000000001')

    def test_fuzzy_suggestion_never_assigns_id(self):
        identifier, note = match_episode('Pilot episode', [Episode('Pilot Episode!', 'EP000000000001')], canonicalize_title_for_match)
        self.assertEqual(identifier, 'EP000000000001')
        identifier, note = match_episode('Pilot episo', [Episode('Pilot episode', 'EP000000000001')], canonicalize_title_for_match)
        self.assertEqual(identifier, '')
        self.assertIn('suggestion', note)


if __name__ == '__main__':
    unittest.main()
