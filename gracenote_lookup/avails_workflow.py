"""Fill blank avails retailer IDs; retain every original populated cell."""
from datetime import datetime
from pathlib import Path
import shutil

from selenium import webdriver

from .avails import AvailsWorkbook
from .browser import BASE_URL, GracenoteBrowser
from .matching import canonicalize_title_for_match, normalize_season_value, valid_id
from .pagination import match_episode, PaginationError
from .runtime import runtime, RunCancelled


def main(settings, browser_factory=None):
    source = Path(settings['input_csv']).expanduser().resolve()
    output = Path(settings['output_csv']).expanduser().resolve()
    if source == output:
        raise ValueError('Choose a different output file to preserve the input workbook.')
    if output.suffix.lower() != '.xlsx':
        raise ValueError('An avails workbook needs an .xlsx output file.')
    book = AvailsWorkbook(source)
    if output.exists():
        if settings['resume_existing']:
            book.resume(output)
        backup = output.with_name(output.stem + '.backup_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.xlsx')
        shutil.copy2(output, backup)
        runtime.log(f'Previous output backed up: {backup}')
    pending = [row for row in book.rows if book.pending(row)]
    # Existing series IDs are reliable hints only when the title has one ID.
    hints = {}
    for row in book.rows:
        if valid_id(row['SeriesTMSID'], 'SH'):
            hints.setdefault(canonicalize_title_for_match(row['SeriesTitle']), set()).add(row['SeriesTMSID'].upper())
    groups = {}
    for row in pending:
        title = canonicalize_title_for_match(row['MovieTitle'] or row['SeriesTitle'])
        choices = hints.get(title, set())
        series_id = row['SeriesTMSID'].upper() if valid_id(row['SeriesTMSID'], 'SH') else (next(iter(choices)) if len(choices) == 1 else '')
        key = (row['Kind'], title, row['Year'] if row['Kind'] == 'Movie' else series_id)
        groups.setdefault(key, []).append(row)
    driver = None
    processed = set()

    def checkpoint():
        book.write(output)
        runtime.rows(book.rows)
        runtime.progress(len(processed), len(pending))

    def process(browser, key, rows, manual):
        runtime.check()
        kind = key[0]
        title = rows[0]['MovieTitle'] or rows[0]['SeriesTitle']
        runtime.log(f'Looking up {kind}: {title} · {len(rows)} rows')
        try:
            identifier = (browser.open_movie(title, rows[0]['Year'], manual=manual) if kind == 'Movie'
                          else browser.open_series(title, key[2], manual=manual))
            if identifier == 'skip':
                for row in rows:
                    row['_skip'] = True
                    row['Notes'] = 'Skipped by user; blank cells preserved'
                    processed.add((row['_sheet'], row['_row']))
                checkpoint()
                return True
            if not valid_id(identifier, 'MV' if kind == 'Movie' else 'SH'):
                for row in rows:
                    row['Notes'] = f'{kind} selection needed'
                checkpoint()
                return False
            for row in rows:
                runtime.check()
                if kind == 'Movie':
                    if book.needs(row, 'MovieTMSID'):
                        row['MovieTMSID'] = identifier
                    row['Notes'] = 'Matched movie'
                else:
                    if book.needs(row, 'SeriesTMSID'):
                        row['SeriesTMSID'] = identifier
                    if book.needs(row, 'EpisodeTMSID'):
                        if not row['EpisodeTitle']:
                            row['Notes'] = 'Episode title is empty'
                        else:
                            try:
                                catalog = (browser.all_episodes() if settings['ignore_season_search'] or not row['Season'] else
                                           browser.catalog(normalize_season_value(row['Season']) or '1'))
                                row['EpisodeTMSID'], row['Notes'] = match_episode(
                                    row['EpisodeTitle'], catalog, canonicalize_title_for_match,
                                    row['EpisodeNumber'], row['Part'])
                            except PaginationError as exc:
                                row['Notes'] = f'Pagination needs review: {exc}'
                    else:
                        row['Notes'] = 'Matched series; existing episode ID preserved'
                processed.add((row['_sheet'], row['_row']))
                checkpoint()
            return True
        except Exception as exc:
            runtime.log(f'{kind} needs attention: {exc}')
            for row in rows:
                if book.pending(row):
                    row['Notes'] = f'{kind} needs review: {exc}'
            checkpoint()
            return manual

    try:
        checkpoint()
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
                runtime.ask('Sign in to Gracenote in Chrome, then click Continue in this app.')
                runtime.check()
            deferred = []
            for key, rows in groups.items():
                if settings['second_pass_only'] or not process(browser, key, rows, False):
                    deferred.append((key, rows))
            for key, rows in deferred:
                process(browser, key, rows, True)
        result = book.summary()
        result.update(output=str(output), report=str(output.with_name(output.stem + '_lookup_report.csv')))
        runtime.log(f'Finished: {result["filled"]} blank ID cells filled; {result["remaining"]} remain blank. Saved: {output}')
        return result
    except RunCancelled:
        checkpoint()
        raise
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
