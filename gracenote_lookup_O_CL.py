#!/usr/bin/env python3
"""
Gracenote TMS ID Lookup Automation Script - Patched Version
Automates the process of looking up TMS IDs for TV episodes
This version implements a strict two-pass workflow with no cross-run persistence.
"""

import csv
import time
import re
import json
import os
from difflib import SequenceMatcher
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager

# Configuration
CONFIG = {
    'input_csv': 'InputEpisodes.csv',
    'output_csv': 'OutputEpisodes_WithTMSIDs.csv',
    'gracenote_url': 'https://gracenoteview.com/',
    'wait_timeout': 10,
    'delay_between_episodes': 2,
    'second_pass_only': False,
    'ignore_season_filter': False  # NEW: When True, searches across all seasons
}


def prompt_mode_choice():
    """
    Ask the user which mode to run: normal two-pass, second pass only, or ignore season filter.
    Sets CONFIG['second_pass_only'] and CONFIG['ignore_season_filter'] based on the answer.
    """
    try:
        print("\nRun mode:")
        print("  1) Normal two-pass workflow")
        print("  2) Second pass only (skip first pass)")
        choice = input("Choose 1 or 2 [1]: ").strip()
        if choice == '2':
            CONFIG['second_pass_only'] = True
            print_success("Second pass only mode enabled")
        else:
            CONFIG['second_pass_only'] = False
            print_success("Normal mode selected")
        
        # NEW: Ask about season filtering
        print("\nSeason filtering:")
        print("  1) Use season filter (default - faster)")
        print("  2) Ignore season filter (search entire series - slower but more thorough)")
        season_choice = input("Choose 1 or 2 [1]: ").strip()
        if season_choice == '2':
            CONFIG['ignore_season_filter'] = True
            print_success("Season filter disabled - will search entire series for each episode")
        else:
            CONFIG['ignore_season_filter'] = False
            print_success("Season filter enabled - will search within specific seasons")
            
    except Exception:
        # Fallback to default if input fails
        print_warning("Could not read input. Using default mode")

# ---------------------------------------------------------------------------
# No persistence across runs
# ---------------------------------------------------------------------------
# The following functions are intentionally no-ops so that series choices are
# not written to disk. Each run starts with an empty cache by design.
def load_series_choice_cache():
    # Always start empty on each run
    return {}

def save_series_choice_cache(cache: dict):
    # No file writes
    return

def clear_series_choice_cache():
    # Nothing to clear on disk
    return

def print_header(text):
    print(f"\n{'='*70}")
    print(f"{text}")
    print(f"{'='*70}")

def print_step(text):
    print(f"   -> {text}")

def print_success(text):
    print(f"   ✓ {text}")

def print_warning(text):
    print(f"   ⚠️  {text}")

def print_error(text):
    print(f"   ❌ {text}")

def read_csv(filename):
    episodes = []
    with open(filename, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            episodes.append(row)
    return episodes

def write_csv(filename, episodes, fieldnames):
    with open(filename, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(episodes)

def extract_tms_id(text, prefix='EP'):
    pattern = f'{prefix}\\d+'
    match = re.search(pattern, text or '', flags=re.IGNORECASE)
    return match.group(0) if match else None

# --- Helper functions for robust text input and normalization ---
def normalize_text(s):
    return re.sub(r'\W+', '', s or '').lower()

# --- Helper to normalize season values ---
def normalize_season_value(s):
    """
    Extract the numeric season value from strings like '14', 'Season 14', 'S14'.
    Returns the digits as a string or '' if none are found.
    """
    if s is None:
        return ''
    m = re.search(r'\d+', str(s))
    return m.group(0) if m else ''

# --- Helper: check if a value is the sentinel '1' (in any common form) ---
def _value_is_one(v) -> bool:
    s = str(v or '').strip()
    return bool(re.fullmatch(r'0*1(\.0+)?', s))

# --- Helper: skip episode if output CSV already has EpisodeTMSID as '1' for this row ---
def should_skip_by_output(output_csv: str, series_title: str, episode_title: str, season: str) -> bool:
    """
    Return True if the existing output CSV has a row for the same Series/Episode/Season
    where EpisodeTMSID is the sentinel '1'. This is a direct check against the output CSV
    and does not depend on prefill/keys.
    """
    try:
        if not os.path.exists(output_csv):
            return False
        with open(output_csv, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                ep_id = (row.get('EpisodeTMSID') or row.get('Episode TMS ID') or row.get('Episode_TMS_ID') or '').strip()
                if not _value_is_one(ep_id):
                    continue
                s_title = (row.get('SeriesTitle') or row.get('Series Title') or row.get('Series') or row.get('Show') or '').strip()
                e_title = (row.get('EpisodeTitle') or row.get('Episode Title') or row.get('Title') or row.get('Episode') or '').strip()
                sea = (row.get('Season') or row.get('season') or '1')
                if (normalize_text(s_title) == normalize_text(series_title)
                    and canonicalize_title_for_match(e_title) == canonicalize_title_for_match(episode_title)
                    and normalize_season_value(sea) == normalize_season_value(season)):
                    return True
    except Exception:
        return False
    return False

# --- Helper: check if a value means the row is already handled ---
def _value_means_done(v: str) -> bool:
    """
    Return True if the value indicates the EP row should be treated as already handled.
    Conditions:
      - A real EP id (e.g., EP050305430001)
      - The sentinel '1' in any common formatting (e.g., '1', '01', '1.0')
    Note: Series SH ids should NOT mark an episode row as done.
    """
    s = (v or "").strip()
    if not s:
        return False
    # Real EP id
    if re.search(r'EP\d+', s, flags=re.IGNORECASE):
        return True
    # Sentinel "1" in various forms
    if re.fullmatch(r'0*1(\.0+)?', s):
        return True
    return False

def _get_first(ep_row: dict, names) -> str:
    """
    Return the first non-empty string among the provided field names from the episode row.
    """
    for n in names:
        val = ep_row.get(n)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return ""

# --- Canonicalize titles for robust episode matching ---

def canonicalize_title_for_match(s):
    """
    Normalizes titles so that 'Spanish Grant, The' and 'The Spanish Grant' match.
    - Moves trailing ', The|, A|, An' to the front
    - Collapses whitespace
    - Removes non-word characters
    - Lowercases
    """
    if not s:
        return ""
    s = s.strip()
    # Handle pattern: "Title, The" -> "The Title"
    m = re.match(r'^(.*),\s*(The|A|An)\s*$', s, flags=re.IGNORECASE)
    if m:
        s = f"{m.group(2)} {m.group(1)}"
    # Normalize: remove non-word chars, collapse spaces, lowercase
    s = re.sub(r'\W+', ' ', s).strip().lower()
    return s

def episode_key(series_title, episode_title, season):
    """
    Build a unique key for an episode based on normalized series, episode title, and numeric season.
    """
    return (normalize_text(series_title), canonicalize_title_for_match(episode_title), normalize_season_value(season))

def load_existing_output_maps(output_csv):
    """
    Build a map of existing episodes in the output CSV by (normalized_series, canonical_episode, season) key,
    as well as a map of series titles to their SH IDs. Also count how many rows are marked with '1' sentinel vs
    real EP/SH IDs for statistics.
    Returns: (existing_ep_map, existing_series_map, stats)
    """
    existing_ep_map = {}
    existing_series_map = {}
    stats = {
        'rows_with_1_in_episode': 0,
        'rows_with_1_in_series': 0,
        'rows_with_real_ep_id': 0,
        'rows_with_real_sh_id': 0,
        'total_skip_rows': 0
    }
    if not os.path.exists(output_csv):
        return existing_ep_map, existing_series_map, stats
    try:
        with open(output_csv, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Read episode identifiers
                series_val = _get_first(row, ['SeriesTitle', 'Series Title', 'Series', 'Show', 'series_title'])
                ep_val = _get_first(row, ['EpisodeTitle', 'Episode Title', 'Title', 'Episode', 'episode_title'])
                season_val = _get_first(row, ['Season', 'season']) or '1'
                # Read TMS IDs
                ep_tms = _get_first(row, ['EpisodeTMSID', 'Episode TMS ID', 'Episode_TMS_ID'])
                series_tms = _get_first(row, ['SeriesTMSID', 'Series TMS ID', 'Series_TMS_ID'])
                # Count statistics
                if _value_is_one(ep_tms):
                    stats['rows_with_1_in_episode'] += 1
                if _value_is_one(series_tms):
                    stats['rows_with_1_in_series'] += 1
                if re.search(r'EP\d+', ep_tms or '', flags=re.IGNORECASE):
                    stats['rows_with_real_ep_id'] += 1
                if re.search(r'SH\d+', series_tms or '', flags=re.IGNORECASE):
                    stats['rows_with_real_sh_id'] += 1
                # Build the episode map
                key = episode_key(series_val, ep_val, season_val)
                if ep_tms or series_tms:
                    existing_ep_map[key] = {
                        'EpisodeTMSID': ep_tms,
                        'SeriesTMSID': series_tms
                    }
                    # Count if this row is marked to skip (either has EP id or is sentinel 1)
                    if _value_means_done(ep_tms) or _value_is_one(series_tms):
                        stats['total_skip_rows'] += 1
                # Build series map (title -> SH id)
                if series_val and series_tms and re.search(r'SH\d+', series_tms or '', flags=re.IGNORECASE):
                    norm_title = normalize_text(series_val)
                    existing_series_map[norm_title] = series_tms
    except Exception:
        pass
    return existing_ep_map, existing_series_map, stats

def wait_for_manual_login(driver):
    print_header("Manual Login Required")
    print("Please log in manually in the browser window.")
    print("Once logged in and you see the main Gracenote page,")
    print("press ENTER here to continue...")
    input()
    print_success("Continuing automation...")

def _scroll_into_view(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.2)

def click_programs_sidebar(driver, wait):
    """
    Attempt to click a 'Programs' link/button in the sidebar.
    This is best-effort as the exact UI structure may vary.
    """
    try:
        sidebar_items = driver.find_elements(By.XPATH, "//*[contains(@class, 'sidebar') or contains(@class, 'nav')]//a | //*[contains(@class, 'sidebar') or contains(@class, 'nav')]//button")
        for item in sidebar_items:
            if 'program' in item.text.lower():
                _scroll_into_view(driver, item)
                item.click()
                print_success("Navigated to Programs section")
                time.sleep(1.0)
                return True
    except Exception as e:
        print_warning(f"Could not navigate to Programs section: {e}")
    return False

def select_series_filter(driver, wait):
    """
    Attempt to find and select the 'SERIES' filter dropdown option.
    This is best-effort as the exact UI structure may vary.
    """
    try:
        # Common patterns: a dropdown labeled "type" or similar
        # Look for native <select> element with options
        selects = driver.find_elements(By.TAG_NAME, "select")
        for sel in selects:
            options = sel.find_elements(By.TAG_NAME, "option")
            for opt in options:
                if 'series' in opt.text.lower():
                    Select(sel).select_by_visible_text(opt.text)
                    print_success("Applied SERIES filter")
                    time.sleep(1.0)
                    return True
        # Try Material UI / custom dropdowns
        # Find a dropdown trigger
        triggers = driver.find_elements(By.XPATH, "//*[@role='button' or @role='combobox' or contains(@class, 'select')]")
        for trigger in triggers:
            if 'type' in trigger.text.lower() or 'filter' in trigger.text.lower():
                _scroll_into_view(driver, trigger)
                trigger.click()
                time.sleep(0.3)
                # Find option "SERIES"
                options = driver.find_elements(By.XPATH, "//*[@role='option' or contains(@class, 'menu-item')]")
                for opt in options:
                    if 'series' in opt.text.lower():
                        opt.click()
                        print_success("Applied SERIES filter")
                        time.sleep(1.0)
                        return True
    except Exception as e:
        print_warning(f"Could not apply SERIES filter: {e}")
    return False

def search_for_series(driver, wait, series_title):
    print_step(f"Searching for series: '{series_title}'")
    try:
        search_box = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], input[type='search']")))
        search_box.clear()
        search_box.send_keys(series_title)
        search_box.send_keys(Keys.RETURN)
        time.sleep(2.0)
        print_success("Search submitted")
        return True
    except Exception as e:
        print_error(f"Search failed: {e}")
        return False

def extract_series_tms_id(driver):
    """
    Extract the SH id from the current page. Could be in the URL or in visible text.
    """
    tms_id = extract_tms_id(driver.current_url, 'SH')
    if not tms_id:
        tms_id = extract_tms_id(driver.page_source, 'SH')
    return tms_id

def click_series_by_sh_id(driver, wait, sh_id):
    """
    After a search, click the series with the given SH id in the results table.
    """
    print_step(f"Selecting series with SH ID: {sh_id}")
    try:
        time.sleep(1.0)
        links = driver.find_elements(By.TAG_NAME, "a")
        for link in links:
            if sh_id.lower() in (link.text or '').lower() or sh_id.lower() in (link.get_attribute('href') or '').lower():
                _scroll_into_view(driver, link)
                link.click()
                time.sleep(2.0)
                print_success(f"Clicked series with SH ID: {sh_id}")
                return True
        print_warning(f"Could not find link for SH ID: {sh_id}")
        return False
    except Exception as e:
        print_error(f"Error clicking series by SH ID: {e}")
        return False

def click_best_series_match(driver, wait, series_title, state):
    """
    After a search, find the best matching series from the results.
    Return status: "ok", "ambiguous", "not_found", "fail"
    If multiple plausible matches exist, return "ambiguous" so we can defer for user selection.
    """
    print_step(f"Looking for best match for series: '{series_title}'")
    try:
        time.sleep(1.5)
        rows = driver.find_elements(By.TAG_NAME, "tr")
        candidates = []
        for row in rows:
            row_text = row.text.lower()
            if 'tms id' in row_text and 'title' in row_text:
                continue
            if 'sh' in row_text or 'series' in row_text.lower():
                candidates.append(row)
        if not candidates:
            print_warning("No series results found")
            return "not_found"
        norm_title = normalize_text(series_title)
        exact_matches = []
        good_matches = []
        for row in candidates:
            row_norm = normalize_text(row.text)
            if norm_title == row_norm or norm_title in row_norm:
                exact_matches.append(row)
            elif SequenceMatcher(None, norm_title, row_norm).ratio() > 0.75:
                good_matches.append(row)
        if len(exact_matches) == 1:
            _scroll_into_view(driver, exact_matches[0])
            links = exact_matches[0].find_elements(By.TAG_NAME, "a")
            if links:
                links[0].click()
                time.sleep(2.0)
                print_success("Selected exact series match")
                return "ok"
            else:
                print_warning("No clickable link in exact match row")
                return "fail"
        elif len(exact_matches) > 1:
            print_warning(f"Found {len(exact_matches)} exact matches - ambiguous")
            return "ambiguous"
        elif len(good_matches) == 1:
            _scroll_into_view(driver, good_matches[0])
            links = good_matches[0].find_elements(By.TAG_NAME, "a")
            if links:
                links[0].click()
                time.sleep(2.0)
                print_success("Selected good series match")
                return "ok"
            else:
                print_warning("No clickable link in good match row")
                return "fail"
        elif len(good_matches) > 1:
            print_warning(f"Found {len(good_matches)} good matches - ambiguous")
            return "ambiguous"
        else:
            print_warning("No good series match found")
            return "not_found"
    except Exception as e:
        print_error(f"Error finding series match: {e}")
        return "fail"

def resolve_series_with_user(driver, wait, series_title, state):
    """
    Prompt the user to manually select the correct series from search results, or skip.
    Return "ok", "skip", or "fail".
    Caches the choice in state['series_choice_cache'] for this run only.
    """
    print_header(f"User Selection Required: '{series_title}'")
    print("The script found multiple or no matching series in the search results.")
    print("Please manually click on the correct series in the browser.")
    print("Then choose:")
    print("  1) I have selected the correct series (press ENTER)")
    print("  2) Skip this series entirely (type 'skip' and press ENTER)")
    choice = input("Your choice [ENTER to continue / 'skip' to skip]: ").strip().lower()
    if choice == 'skip':
        print_warning(f"Skipping series: {series_title}")
        # Mark in cache to skip all episodes for this series this run
        norm_key = normalize_text(series_title)
        state.setdefault('series_choice_cache', {})[norm_key] = None
        return "skip"
    # User claims they have selected; extract the SH id from the page
    series_tms_id = extract_series_tms_id(driver)
    if series_tms_id:
        print_success(f"User selected series with SH ID: {series_tms_id}")
        norm_key = normalize_text(series_title)
        state.setdefault('series_choice_cache', {})[norm_key] = series_tms_id
        return "ok"
    else:
        print_error("Could not extract series TMS ID after user selection")
        return "fail"

def click_seasons_episodes_tab(driver, wait):
    print_step("Opening 'Seasons & Episodes' tab...")
    try:
        # Look for tab or link with text matching "season" and "episode"
        tabs = driver.find_elements(By.XPATH, "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'season') and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'episode')]")
        if tabs:
            _scroll_into_view(driver, tabs[0])
            tabs[0].click()
            time.sleep(2.0)
            print_success("Seasons & Episodes tab opened")
            return True
        print_warning("Could not find 'Seasons & Episodes' tab")
        return False
    except Exception as e:
        print_error(f"Error opening Seasons & Episodes tab: {e}")
        return False

def _ensure_seasons_tab(driver, wait):
    """
    Ensure we are on the Seasons & Episodes tab. If not currently on it, click it.
    """
    try:
        tabs = driver.find_elements(By.XPATH, "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'season') and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'episode')]")
        if tabs:
            _scroll_into_view(driver, tabs[0])
            tabs[0].click()
            time.sleep(1.5)
            return True
    except Exception:
        pass
    return False

def select_season(driver, wait, season):
    """
    Select a given season from the season dropdown.
    """
    print_step(f"Selecting season: {season}")
    desired = normalize_season_value(season)
    if not desired:
        print_warning("Season value could not be normalized")
        return False
    
    # 1) Try native <select> elements first
    try:
        selects = driver.find_elements(By.TAG_NAME, "select")
        for sel in selects:
            options = sel.find_elements(By.TAG_NAME, "option")
            for opt in options:
                if normalize_season_value(opt.text) == desired:
                    Select(sel).select_by_visible_text(opt.text)
                    time.sleep(1.5)
                    print_success(f"Season {desired} selected")
                    return True
    except Exception as e:
        print_warning(f"Native select path failed: {e}")

    # 2) Try custom dropdowns (Material UI or similar)
    try:
        # Find a trigger that looks like a season picker
        trigger = None
        # Common patterns: role=button or combobox with the word Season in it
        triggers = driver.find_elements(By.XPATH,
            "//*[self::div or self::button or self::span][(contains(@role,'button') or @role='combobox' or contains(@class,'select') or contains(@class,'MuiSelect')) and contains(normalize-space(.), 'Season')]")
        if triggers:
            trigger = triggers[0]
        else:
            # Fallback, any button-like element near 'Season and Episode Summary'
            triggers = driver.find_elements(By.XPATH,
                "//*[contains(., 'Season and Episode Summary')]/following::*[(self::div or self::button) and (contains(@role,'button') or contains(@class,'select') or contains(@class,'MuiSelect'))][1]")
            if triggers:
                trigger = triggers[0]
        if trigger:
            trigger.click()
            time.sleep(0.3)
            # Options in MUI live under a listbox
            options = driver.find_elements(By.XPATH, "//*[@role='listbox']//*[@role='option'] | //ul[@role='listbox']//li | //div[@role='listbox']//li | //li[contains(@class,'MuiMenuItem')]")
            if not options:
                # Generic fallback
                options = driver.find_elements(By.XPATH, f"//*[self::li or self::div or self::button][contains(normalize-space(.), 'Season {desired}') or normalize-space(.)='{desired}']")
            best = None
            for el in options:
                txt = el.text.strip()
                if normalize_season_value(txt) == desired:
                    best = el
                    break
            if best is None and options:
                best = options[0]
            if best:
                best.click()
                time.sleep(1.5)
                print_success(f"Season {desired} selected")
                return True
    except Exception as e:
        print_warning(f"Custom dropdown path failed: {e}")

    print_warning(f"Could not select season {desired}")
    return False

def find_episode_tms_id(driver, wait, episode_title, current_season=None):
    """
    Search for an episode TMS ID.
    If CONFIG['ignore_season_filter'] is True, searches across all seasons.
    Otherwise, searches within the current season only.
    """
    print_step(f"Searching for episode: '{episode_title}'...")
    
    if not episode_title or episode_title.strip() == '':
        print_error("Episode title is empty!")
        return "", "Episode title is empty"
    
    target_norm = canonicalize_title_for_match(episode_title)

    def scan_current_page():
        time.sleep(0.8)  # table render settle
        rows = driver.find_elements(By.TAG_NAME, "tr")
        best_row, best_score = None, 0.0
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
            except Exception:
                cells = []
            cell_text = cells[1].text.strip() if len(cells) >= 2 else ''
            row_text = cell_text or row.text
            if not row_text or row_text.lower().startswith("tms id"):
                continue
            row_norm = canonicalize_title_for_match(row_text)
            if row_norm == target_norm:
                return row, 1.0
            score = 0.95 if target_norm and target_norm in row_norm else SequenceMatcher(None, target_norm, row_norm).ratio()
            if score > best_score:
                best_row, best_score = row, score
                if best_score >= 0.99:
                    break
        return (best_row, best_score) if best_row else (None, 0.0)

    def extract_row_tms(row):
        tms_id = extract_tms_id(row.text, 'EP') or extract_tms_id(row.get_attribute('innerHTML'), 'EP')
        if not tms_id:
            try:
                link = row.find_elements(By.TAG_NAME, "a")[0]
                _scroll_into_view(driver, link)
                link.click()
                time.sleep(1)
                tms_id = extract_tms_id(driver.current_url, 'EP')
                driver.back()
                time.sleep(0.8)
            except Exception:
                pass
        return tms_id

    def forward_walk():
        """Scan current page, then walk forward through pages until visited loops or Next is unavailable."""
        visited = set()
        # Always scan the page we start on
        row, score = scan_current_page()
        if row and score >= 0.75:
            tms_id = extract_row_tms(row)
            if tms_id:
                print_success(f"Episode TMS ID found: {tms_id}")
                return tms_id
        marker = _get_range_marker(driver)
        if marker:
            print_step(f"Page range: {marker}")
            visited.add(marker)
        steps = 0
        while steps < 40:
            marker_before = _get_range_marker(driver)
            if not _click_next_page(driver):
                break
            # If the marker did not change, retry up to 2 times
            tries = 0
            while _get_range_marker(driver) == marker_before and tries < 2:
                time.sleep(0.3)
                _click_next_page(driver)
                tries += 1
            time.sleep(1.0)
            new_marker = _get_range_marker(driver)
            if new_marker:
                if new_marker in visited:
                    break
                print_step(f"Page range: {new_marker}")
                visited.add(new_marker)
            row, score = scan_current_page()
            if row and score >= 0.75:
                tms_id = extract_row_tms(row)
                if tms_id:
                    print_success(f"Episode TMS ID found: {tms_id}")
                    return tms_id
            steps += 1
        return None

    try:
        # NEW: If ignoring season filter, we need to search across all seasons
        if CONFIG['ignore_season_filter']:
            print_step("Searching across all seasons (season filter disabled)")
            # First try the current view
            result = forward_walk()
            if result:
                return result, ""
            
            # If not found, try to cycle through available seasons
            # This is best-effort - we'll try to find all season options and search each
            if _ensure_seasons_tab(driver, wait):
                available_seasons = _get_available_seasons(driver)
                if available_seasons:
                    print_step(f"Found {len(available_seasons)} seasons, searching each...")
                    for season_num in available_seasons:
                        print_step(f"Checking Season {season_num}...")
                        if select_season(driver, wait, str(season_num)):
                            time.sleep(1.0)
                            _wait_for_first_page(driver, timeout=4.0)
                            result = forward_walk()
                            if result:
                                return result, f"Found in Season {season_num}"
                    print_warning(f"Episode not found in any of {len(available_seasons)} seasons")
                else:
                    print_warning("Could not detect available seasons")
            
            return "", "Episode not found across all seasons"
        
        # ORIGINAL: Use season-specific search
        # First forward pass
        result = forward_walk()
        if result:
            return result, ""

        # One reset loop only: ensure tab, reselect season to jump back to page 1, then forward again
        if current_season is not None:
            print_step(f"Resetting to page 1 by reselecting Season {current_season} then scanning forward again")
            if _ensure_seasons_tab(driver, wait):
                if select_season(driver, wait, current_season):
                    # Give the table a moment and confirm we are at 1 - ...
                    _wait_for_first_page(driver, timeout=4.0)
                    result = forward_walk()
                    if result:
                        return result, ""
                else:
                    print_warning(f"Could not select season {current_season}")
        
        print_warning(f"Episode not found: {episode_title}")
        return "", "Episode not found"
    except Exception as e:
        print_error(f"Error searching for episode: {e}")
        return "", f"Error: {str(e)}"

def _get_available_seasons(driver):
    """
    NEW: Attempt to detect all available season numbers from the season dropdown.
    Returns a list of season numbers (as integers) or empty list if cannot detect.
    """
    try:
        # Try native select first
        selects = driver.find_elements(By.TAG_NAME, "select")
        for sel in selects:
            options = sel.find_elements(By.TAG_NAME, "option")
            seasons = []
            for opt in options:
                season_num = normalize_season_value(opt.text)
                if season_num:
                    try:
                        seasons.append(int(season_num))
                    except:
                        pass
            if seasons:
                return sorted(seasons)
        
        # Try custom dropdown
        triggers = driver.find_elements(By.XPATH,
            "//*[self::div or self::button or self::span][(contains(@role,'button') or @role='combobox' or contains(@class,'select') or contains(@class,'MuiSelect')) and contains(normalize-space(.), 'Season')]")
        if triggers:
            trigger = triggers[0]
            trigger.click()
            time.sleep(0.3)
            options = driver.find_elements(By.XPATH, "//*[@role='listbox']//*[@role='option'] | //ul[@role='listbox']//li | //div[@role='listbox']//li | //li[contains(@class,'MuiMenuItem')]")
            seasons = []
            for opt in options:
                season_num = normalize_season_value(opt.text)
                if season_num:
                    try:
                        seasons.append(int(season_num))
                    except:
                        pass
            # Close the dropdown
            trigger.click()
            time.sleep(0.2)
            if seasons:
                return sorted(seasons)
    except Exception as e:
        print_warning(f"Could not detect available seasons: {e}")
    return []

def _get_range_marker(driver):
    """
    Extract the current page range indicator text (e.g., "1 - 50 of 250") if visible.
    Returns the text or None if not found.
    """
    try:
        markers = driver.find_elements(By.XPATH, "//*[contains(text(), ' of ') or contains(text(), ' - ')]")
        for m in markers:
            txt = m.text.strip()
            if re.search(r'\d+\s*-\s*\d+\s+of\s+\d+', txt, flags=re.IGNORECASE):
                return txt
    except Exception:
        pass
    return None

def _click_next_page(driver):
    """
    Click the 'Next' pagination button. Returns True if clicked, False if button not found or disabled.
    """
    try:
        # Common patterns: button with 'next' text or aria-label, or icon buttons
        candidates = driver.find_elements(By.XPATH,
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')] | "
            "//button[contains(@aria-label, 'next')] | "
            "//button[contains(@aria-label, 'Next')] | "
            "//*[@role='button' and contains(., 'next')] | "
            "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]")
        for btn in candidates:
            if btn.is_displayed() and btn.is_enabled():
                _scroll_into_view(driver, btn)
                btn.click()
                return True
        # Also try icon-based pagination (arrow right)
        icons = driver.find_elements(By.XPATH, "//button[contains(@aria-label, 'Go to next page') or contains(@title, 'Next')]")
        for icon in icons:
            if icon.is_displayed() and icon.is_enabled():
                _scroll_into_view(driver, icon)
                icon.click()
                return True
    except Exception:
        pass
    return False

def _wait_for_first_page(driver, timeout=4.0):
    """
    Wait for the page range indicator to show '1 - ...' to confirm we're back at page 1.
    """
    end_time = time.time() + timeout
    while time.time() < end_time:
        marker = _get_range_marker(driver)
        if marker and marker.strip().startswith('1 -'):
            return True
        time.sleep(0.5)
    return False

def process_episode(driver, wait, episode, episode_index, total_episodes, state, allow_defer=True):
    print_header(f"Episode {episode_index + 1}/{total_episodes}")
    
    try:
        series_title = episode.get('SeriesTitle') or episode.get('Series') or episode.get('series_title') or episode.get('Show') or ''
        episode_title = episode.get('EpisodeTitle') or episode.get('Title') or episode.get('episode_title') or episode.get('Episode') or ''
        season = episode.get('Season') or episode.get('season') or '1'
        
        print(f"   Series: {series_title}")
        print(f"   Episode: {episode_title}")
        print(f"   Season: {season}")
        
        if not series_title:
            print_error("Series title is missing!")
            episode['Notes'] = "Series title is missing"
            return
        
        if not episode_title:
            print_error("Episode title is missing!")
            episode['Notes'] = "Episode title is missing"
            return
        
        series_key = normalize_text(series_title)
        cached_sh = state.get('series_choice_cache', {}).get(series_key)
        if cached_sh is None:
            # User explicitly skipped this series in a prior interaction this run
            episode['EpisodeTMSID'] = '1'
            episode['Notes'] = 'Skipped series by user'
            print_warning(f"Series '{series_title}' was skipped by user")
            return
        
        # Navigate to the series if not already there
        if state.get('current_series') != series_title:
            print_step(f"Switching to series: {series_title}")

            if not search_for_series(driver, wait, series_title):
                if allow_defer:
                    episode['Notes'] = "Search failed - deferred for user selection"
                    state.setdefault('deferred', []).append(episode)
                    state.setdefault('unresolved_series', set()).add(series_key)
                    print_warning(f"Search failed for series '{series_title}' - deferring")
                    return
                else:
                    if not resolve_series_with_user(driver, wait, series_title, state):
                        episode['Notes'] = "User selection failed"
                        return

            if cached_sh:
                if not click_series_by_sh_id(driver, wait, cached_sh):
                    episode['Notes'] = "Failed to select cached series"
                    return
                series_tms_id = extract_series_tms_id(driver)
                episode['SeriesTMSID'] = series_tms_id
                state['current_series'] = series_title
                # Reset tab or season context when series changes
                state['on_seasons_tab'] = False
                state['current_season'] = None
            else:
                status = click_best_series_match(driver, wait, series_title, state)
                if status == "ambiguous":
                    if allow_defer:
                        episode['Notes'] = "Ambiguous series name - deferred for user selection"
                        state.setdefault('deferred', []).append(episode)
                        state.setdefault('ambiguous_series', set()).add(series_key)
                        print_warning(f"Ambiguous series '{series_title}' - deferring all episodes for second pass")
                        return
                    else:
                        if not resolve_series_with_user(driver, wait, series_title, state):
                            episode['Notes'] = "User selection failed"
                            return
                elif status in ("not_found", "fail"):
                    if allow_defer:
                        episode['Notes'] = "Series not found - deferred for user selection"
                        state.setdefault('deferred', []).append(episode)
                        state.setdefault('unresolved_series', set()).add(series_key)
                        print_warning(f"No exact SERIES match for '{series_title}' - deferring for second pass")
                        return
                    else:
                        if not resolve_series_with_user(driver, wait, series_title, state):
                            episode['Notes'] = "User selection failed"
                            return
                series_tms_id = extract_series_tms_id(driver)
                episode['SeriesTMSID'] = series_tms_id
                # Cache the choice even for unambiguous cases, to speed up later selections (in-run only)
                if series_tms_id:
                    state.setdefault('series_choice_cache', {})[series_key] = series_tms_id
                state['current_series'] = series_title
                # Reset tab or season context when series changes
                state['on_seasons_tab'] = False
                state['current_season'] = None
        else:
            # Already on the correct series page, ensure SeriesTMSID is populated
            if not episode.get('SeriesTMSID'):
                episode['SeriesTMSID'] = extract_series_tms_id(driver)
            # Persist this series choice for this run only
            if episode.get('SeriesTMSID'):
                state.setdefault('series_choice_cache', {})[series_key] = episode['SeriesTMSID']

        # Open Seasons & Episodes tab only if not already open
        if not state.get('on_seasons_tab'):
            if not click_seasons_episodes_tab(driver, wait):
                episode['Notes'] = "Failed to load episodes tab"
                return
            state['on_seasons_tab'] = True
        else:
            print_step("Seasons & Episodes tab already open - skipping")
  
        # NEW: Only select season if we're NOT ignoring season filter
        if not CONFIG['ignore_season_filter']:
            # Select season only if it changed
            if state.get('current_season') != season:
                if not select_season(driver, wait, season):
                    episode['Notes'] = f"Failed to select season {season}"
                    return
                state['current_season'] = season
                time.sleep(1.0)
            else:
                print_step(f"Season {season} already selected - skipping")
        else:
            print_step("Season filter disabled - will search across all seasons")

        episode_tms_id, note = find_episode_tms_id(driver, wait, episode_title, season if not CONFIG['ignore_season_filter'] else None)
        episode['EpisodeTMSID'] = episode_tms_id
        if note:
            episode['Notes'] = note

    except Exception as e:
        print_error(f"Error processing episode: {e}")
        episode['Notes'] = f"Error: {str(e)}"

def main():
    print_header("Starting Gracenote TMS ID Lookup Automation")
    # Ensure no persisted series cache affects this run
    clear_series_choice_cache()

    # Prompt for run mode at start
    prompt_mode_choice()
    
    print(f"\n📂 Reading input CSV file: {CONFIG['input_csv']}...")
    try:
        episodes = read_csv(CONFIG['input_csv'])
        print_success(f"Loaded {len(episodes)} episodes from CSV")
        
        # Show first episode as sample
        if episodes:
            print(f"\n   Sample first row:")
            for key, value in list(episodes[0].items())[:5]:
                print(f"      {key}: {value}")
        
    except Exception as e:
        print_error(f"Error reading CSV: {e}")
        print("\nPlease ensure InputEpisodes.csv exists in the same directory.")
        return
    
    fieldnames = list(episodes[0].keys()) if episodes else []
    for field in ['SeriesTMSID', 'EpisodeTMSID', 'Notes']:
        if field not in fieldnames:
            fieldnames.append(field)

    # Prefill from existing output CSV so we can skip already processed episodes
    existing_ep_map, existing_series_map, output_stats = load_existing_output_maps(CONFIG['output_csv'])
    
    # Show what we found in the output CSV
    print_header("Output CSV Analysis")
    print(f"   Rows with '1' in EpisodeTMSID column: {output_stats['rows_with_1_in_episode']}")
    print(f"   Rows with '1' in SeriesTMSID column: {output_stats['rows_with_1_in_series']}")
    print(f"   Rows with real Episode TMS IDs: {output_stats['rows_with_real_ep_id']}")
    print(f"   Rows with real Series TMS IDs: {output_stats['rows_with_real_sh_id']}")
    print(f"   Total rows marked to skip: {output_stats['total_skip_rows']}")
    prefill_count = 0
    skip_count = 0
    for ep in episodes:
        key = episode_key(
            ep.get('SeriesTitle') or ep.get('Series') or ep.get('series_title') or ep.get('Show') or '',
            ep.get('EpisodeTitle') or ep.get('Title') or ep.get('episode_title') or ep.get('Episode') or '',
            ep.get('Season') or ep.get('season') or '1'
        )
        if key in existing_ep_map:
            cached = existing_ep_map[key]
            # Copy the values from output CSV to the episode
            if cached.get('SeriesTMSID', '') is not None:
                ep['SeriesTMSID'] = cached.get('SeriesTMSID', '')
            if cached.get('EpisodeTMSID', '') is not None:
                ep['EpisodeTMSID'] = cached.get('EpisodeTMSID', '')
            prefill_count += 1
        
        # Check if this row should be skipped based on what's now in the episode
        # (either from input CSV or from output CSV we just loaded)
        ep_tms = ep.get('EpisodeTMSID', '')
        series_tms = ep.get('SeriesTMSID', '')
        if _value_means_done(ep_tms) or _value_is_one(series_tms):
            skip_count += 1

    if prefill_count:
        print_success(f"Prefilled {prefill_count} episodes from existing output")
    if skip_count:
        print_success(f"Found {skip_count} episodes marked to skip (with '1' or TMS IDs)")

    # Build a processing queue up-front (skip rows that already have EP ids or are marked EP=1).
    episodes_to_process = []
    for ep in episodes:
        ep_tms = (ep.get('EpisodeTMSID') or ep.get('Episode TMS ID') or ep.get('Episode_TMS_ID') or '').strip()
        series_mark = (ep.get('SeriesTMSID') or ep.get('Series TMS ID') or ep.get('Series_TMS_ID') or '').strip()
        if _value_means_done(ep_tms) or _value_is_one(series_mark):
            continue
        episodes_to_process.append(ep)
    print_success(f"Episodes to process this run: {len(episodes_to_process)}")

    print("\n🌐 Launching Chrome browser...")
    options = webdriver.ChromeOptions()
    options.add_argument('--start-maximized')
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    wait = WebDriverWait(driver, CONFIG['wait_timeout'])
    
    try:
        print(f"🔗 Navigating to {CONFIG['gracenote_url']}...")
        driver.get(CONFIG['gracenote_url'])

        wait_for_manual_login(driver)

        # Best-effort: land on Programs and apply SERIES filter once up front
        click_programs_sidebar(driver, wait)
        select_series_filter(driver, wait)

        print_header("Processing Episodes")
        state = {'current_series': None, 'current_season': None, 'on_seasons_tab': False,
                 'series_choice_cache': {}, 'deferred': [],
                 'ambiguous_series': set(), 'unresolved_series': set()}
        # Load any persisted series choices (always empty in this patched version)
        state['series_choice_cache'] = load_series_choice_cache()
        # Seed cache with SH ids from existing output so we do not prompt again
        for norm_title, sh_id in existing_series_map.items():
            state['series_choice_cache'][norm_title] = sh_id

        # First pass: process the upfront queue and save progress after each episode
        if not CONFIG.get('second_pass_only'):
            for i, ep in enumerate(episodes_to_process):
                process_episode(driver, wait, ep, i, len(episodes_to_process), state, allow_defer=True)
                write_csv(CONFIG['output_csv'], episodes, fieldnames)
                print_success(f"Progress saved to {CONFIG['output_csv']}")

        if CONFIG.get('second_pass_only'):
            print_header("Second Pass Only Mode")
            # Defer only episodes that don't already have TMS IDs or are marked as done
            state['deferred'] = list(episodes_to_process)
            print_success(f"Deferred {len(state['deferred'])} episodes that need processing")
        # Second pass: resolve any ambiguous series by asking the user once, then process those episodes
        if state['deferred']:
            print_header("Second Pass: Resolve Ambiguous Series")
            # Group deferred episodes by series title (normalized)
            groups = {}
            for ep in state['deferred']:
                key = normalize_text(ep.get('SeriesTitle') or ep.get('Series') or ep.get('series_title') or ep.get('Show') or '')
                groups.setdefault(key, []).append(ep)
            # Process each group
            for key, eps in groups.items():
                if not eps:
                    continue
                series_title = eps[0].get('SeriesTitle') or eps[0].get('Series') or eps[0].get('series_title') or eps[0].get('Show') or ''
                if key not in state['series_choice_cache']:
                    # Prompt user to choose by clicking in the UI
                    status = resolve_series_with_user(driver, wait, series_title, state)
                    if status == "skip":
                        # Mark all episodes in this series as skipped so future runs skip them up front
                        for ep in eps:
                            ep['EpisodeTMSID'] = '1'
                            ep['Notes'] = 'Skipped series by user'
                        write_csv(CONFIG['output_csv'], episodes, fieldnames)
                        print_success(f"Series skipped, progress saved to {CONFIG['output_csv']}")
                        continue
                    if status != "ok":
                        for ep in eps:
                            ep['Notes'] = ep.get('Notes', '') or 'Could not resolve ambiguous series'
                        continue
                # With an in-run cache set, process each deferred episode without allowing further deferral
                for j, ep in enumerate(eps):
                    process_episode(driver, wait, ep, j, len(eps), state, allow_defer=False)
                    write_csv(CONFIG['output_csv'], episodes, fieldnames)
                    print_success(f"Progress saved to {CONFIG['output_csv']}")
            # Clear deferred list
            state['deferred'].clear()

        print_header("Processing Complete")
        successful = sum(1 for e in episodes if e.get('EpisodeTMSID'))
        needs_review = sum(1 for e in episodes if not e.get('EpisodeTMSID') or e.get('Notes'))

        print(f"\n📊 Summary:")
        print(f"   Total episodes processed: {len(episodes)}")
        print(f"   Successfully found: {successful}")
        print(f"   Need manual review: {needs_review}")
        print(f"\n📄 Output saved to: {CONFIG['output_csv']}\n")

    except Exception as e:
        print_error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n🔒 Closing browser...")
        driver.quit()
        print_success("Done!\n")

if __name__ == "__main__":
    main()
