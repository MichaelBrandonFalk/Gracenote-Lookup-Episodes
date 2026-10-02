"""Two-pass lookup orchestration, independent of the desktop UI."""
from datetime import datetime
from pathlib import Path
import shutil

from selenium import webdriver

from .browser import BASE_URL, GracenoteBrowser
from .csv_io import read_csv, write_csv
from .matching import (canonicalize_title_for_match, extract_tms_id, group_key,
                       handled, merge_existing, normalize_season_value, valid_id,
                       _value_is_one, _value_means_done)
from .pagination import match_episode, PaginationError
from .runtime import runtime, RunCancelled
from .sign_in import finish_sign_in

DEFAULTS = {
    'input_csv': 'InputEpisodes.csv',
    'output_csv': 'OutputEpisodes_WithTMSIDs.csv',
    'wait_timeout': 15,
    'second_pass_only': False,
    'ignore_season_search': False,
    'resume_existing': True,
}


def main(config=None, browser_factory=None):
    settings = dict(DEFAULTS)
    settings.update(config or {})
    if config is None:
        settings['second_pass_only'] = runtime.ask('Manual series selection only? (y/N): ').strip().lower() == 'y'
        settings['ignore_season_search'] = runtime.ask('Search every season? (y/N): ').strip().lower() == 'y'
    runtime.check()
    source = Path(settings['input_csv']).expanduser().resolve()
    output = Path(settings['output_csv']).expanduser().resolve()
    if source.suffix.lower() == '.xlsx':
        from .avails_workflow import main as lookup_workbook
        return lookup_workbook(settings, browser_factory)
    if source.suffix.lower() != '.csv' or output.suffix.lower() != '.csv':
        raise ValueError('Choose an input CSV or XLSX workbook and a matching output file type.')
    if source == output:
        raise ValueError('Choose a different output file to preserve the input CSV.')
    episodes = read_csv(source)
    for episode in episodes:
        episode.setdefault('Notes', '')
    fields = list(episodes[0])
    previous = read_csv(output) if output.exists() and settings['resume_existing'] else []
    choices = merge_existing(episodes, previous)
    if output.exists():
        backup = output.with_name(output.stem + '.backup_' +
                                  datetime.now().strftime('%Y%m%d_%H%M%S_%f') + output.suffix)
        shutil.copy2(output, backup)
        runtime.log(f'Previous output backed up: {backup}')
    pending = [episode for episode in episodes if not handled(episode)]
    groups = {}
    for episode in pending:
        groups.setdefault(group_key(episode), []).append(episode)
    driver = None
    deferred = []
    processed = 0

    def checkpoint():
        write_csv(output, episodes, fields)
        runtime.rows(episodes)

    def process_group(browser, key, rows, manual):
        nonlocal processed
        runtime.check()
        title = rows[0]['SeriesTitle']
        if not title:
            for row in rows:
                row['Notes'] = 'Series title is empty'
            checkpoint()
            return True
        identifier = key[1] or choices.get(key[0], '')
        runtime.log(f'Looking up {title} · {len(rows)} episode rows')
        try:
            identifier = browser.open_series(title, identifier, manual=manual)
            if identifier == 'skip':
                for row in rows:
                    row['EpisodeTMSID'] = '1'
                    row['Notes'] = 'Skipped series by user'
                checkpoint()
                return True
            if not identifier:
                for row in rows:
                    row['Notes'] = 'Series selection needed in second pass'
                checkpoint()
                return False
            # Cache choices by group, not title alone: two versions of a series
            # can legitimately share a title in the same input file.
            for row in rows:
                row['SeriesTMSID'] = identifier
            for row in rows:
                runtime.check()
                try:
                    if not row['EpisodeTitle']:
                        row['Notes'] = 'Episode title is empty'
                    else:
                        catalog = (browser.all_episodes() if settings['ignore_season_search'] else
                                   browser.catalog(normalize_season_value(row['Season']) or '1'))
                        row['EpisodeTMSID'], row['Notes'] = match_episode(
                            row['EpisodeTitle'], catalog, canonicalize_title_for_match,
                            row.get('EpisodeNumber', ''), row.get('Part', ''))
                except PaginationError as exc:
                    row['EpisodeTMSID'] = ''
                    row['Notes'] = f'Pagination needs review: {exc}'
                checkpoint()
                processed += 1
                runtime.progress(processed, len(pending))
            return True
        except Exception as exc:
            runtime.log(f'Series needs attention: {exc}')
            for row in rows:
                if not handled(row):
                    row['Notes'] = f'Series needs review: {exc}'
            checkpoint()
            return manual  # Automatic failures also get a manual second pass.

    try:
        checkpoint()
        runtime.progress(0, len(pending))
        if pending:
            if browser_factory:
                browser = browser_factory()
            else:
                runtime.log('Launching Chrome. Sign in directly in the browser.')
                options = webdriver.ChromeOptions()
                options.add_argument('--start-maximized')
                driver = webdriver.Chrome(options=options)
                driver.set_page_load_timeout(60)
                browser = GracenoteBrowser(driver, settings['wait_timeout'])
                driver.get(BASE_URL)
                finish_sign_in(browser)
            for key, rows in groups.items():
                if settings['second_pass_only'] or not process_group(browser, key, rows, False):
                    deferred.append((key, rows))
            if deferred:
                runtime.log('Second pass: choose the correct series in Chrome when prompted.')
                for key, rows in deferred:
                    process_group(browser, key, rows, True)
        found = sum(valid_id(row.get('EpisodeTMSID'), 'EP') for row in episodes)
        skipped = sum(_value_is_one(row.get('EpisodeTMSID')) or _value_is_one(row.get('SeriesTMSID'))
                      for row in episodes if not valid_id(row.get('EpisodeTMSID'), 'EP'))
        review = len(episodes) - found - skipped
        runtime.log(f'Finished: {found} found, {skipped} skipped, {review} need review. Saved: {output}')
        return {'found': found, 'skipped': skipped, 'review': review, 'output': str(output)}
    except RunCancelled:
        checkpoint()
        raise
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
