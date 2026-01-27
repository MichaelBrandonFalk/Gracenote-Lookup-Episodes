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
    'ignore_season_on_second_pass': False  # <--- Add this line
}


def prompt_mode_choice():
    try:
        print("\nRun mode:")
        print("  1) Normal two-pass workflow")
        print("  2) Second pass only (skip first pass)")
        choice = input("Choose 1 or 2 [1]: ").strip()
        CONFIG['second_pass_only'] = (choice == '2')
        
        # Add the new toggle prompt here
        print("\nSeason handling for second pass:")
        print("  1) Search specific season from CSV")
        print("  2) Ignore season number (search entire series)")
        s_choice = input("Choose 1 or 2 [1]: ").strip()
        if s_choice == '2':
            CONFIG['ignore_season_on_second_pass'] = True
            print_success("Ignore Season enabled for second pass")
        else:
            CONFIG['ignore_season_on_second_pass'] = False
            print_success("Specific season matching enabled")
            
    except Exception:
        print_warning("Could not read input. Using default settings.")

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
    - Lowercases and removes punctuation via normalize_text
    """
    if not s:
        return ''
    t = str(s).strip()
    m = re.match(r'^(.*?),(?:\s*)(the|a|an)$', t, flags=re.IGNORECASE)
    if m:
        t = f"{m.group(2)} {m.group(1)}"
    # Collapse multiple spaces
    t = re.sub(r'\s+', ' ', t)
    # Use existing normalize_text to strip punctuation and lowercase
    return normalize_text(t)

# --- Keys and existing-output merge helpers ---

def episode_key(series_title, episode_title, season):
    """
    Build a stable key for an episode using normalized series title,
    canonicalized episode title, and normalized season.
    """
    s = normalize_text(series_title or '')
    e = canonicalize_title_for_match(episode_title or '')
    n = normalize_season_value(season) or '1'
    return f"{s}|{e}|{n}"


def load_existing_output_maps(output_csv):
    """
    Load the existing output CSV, if present, and return two maps:
      1) episodes_map: key -> {EpisodeTMSID, SeriesTMSID} for rows that should be skipped
      2) series_map: normalized series title -> SH id for rows that have an SH id
    
    Returns (episodes_map, series_map, stats) where stats contains skip counts
    """
    episodes_map = {}
    series_map = {}
    stats = {
        'rows_with_1_in_episode': 0,
        'rows_with_1_in_series': 0,
        'rows_with_real_ep_id': 0,
        'rows_with_real_sh_id': 0,
        'total_skip_rows': 0
    }
    
    try:
        if not os.path.exists(output_csv):
            return episodes_map, series_map, stats
        with open(output_csv, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                ep_id = (row.get('EpisodeTMSID') or row.get('Episode TMS ID') or row.get('Episode_TMS_ID') or '').strip()
                sh_id = (row.get('SeriesTMSID') or row.get('Series TMS ID') or row.get('Series_TMS_ID') or '').strip()
                s_title = (row.get('SeriesTitle') or row.get('Series') or row.get('series_title') or row.get('Show') or '').strip()
                e_title = (row.get('EpisodeTitle') or row.get('Title') or row.get('episode_title') or row.get('Episode') or '').strip()
                season = (row.get('Season') or row.get('season') or '1')
                key = episode_key(s_title, e_title, season)
                
                # Track what we find
                has_real_ep = bool(re.search(r'EP\d+', ep_id, flags=re.IGNORECASE))
                has_real_sh = bool(re.search(r'SH\d+', sh_id, flags=re.IGNORECASE))
                has_1_in_ep = bool(ep_id and re.fullmatch(r'0*1(\.0+)?', ep_id))
                has_1_in_sh = bool(sh_id and re.fullmatch(r'0*1(\.0+)?', sh_id))
                
                if has_real_ep:
                    stats['rows_with_real_ep_id'] += 1
                if has_real_sh:
                    stats['rows_with_real_sh_id'] += 1
                if has_1_in_ep:
                    stats['rows_with_1_in_episode'] += 1
                if has_1_in_sh:
                    stats['rows_with_1_in_series'] += 1
                
                # Add to map if it has real EP ID or sentinel "1" in EP, or sentinel "1" in SH
                if (_value_means_done(ep_id) or _value_is_one(sh_id)):
                    episodes_map[key] = {'EpisodeTMSID': ep_id, 'SeriesTMSID': sh_id}
                    stats['total_skip_rows'] += 1
                
                # Only seed series cache when we have a real SH id
                if has_real_sh:
                    series_map[normalize_text(s_title)] = sh_id
    except Exception as e:
        print_warning(f"Error loading output CSV: {e}")
    
    return episodes_map, series_map, stats

# --- Title extraction and series label helpers ---
def extract_clean_title(text):
    """
    Pull the visible title line and drop a trailing year like '(2016)'.
    Used to compare titles in autocomplete and search results.
    """
    if not text:
        return ''
    t = str(text).strip().splitlines()[0]
    # Remove a trailing year in parentheses
    t = re.sub(r'\(\d{4}\)$', '', t).strip()
    return t

def has_series_label(text):
    """
    Best-effort check that an option or result is a SERIES (not a film or special).
    """
    return 'series' in (text or '').lower()

# --- Pagination helpers ---
def _scroll_into_view(driver, el):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass

def _find_pager_buttons(driver):
    """
    Try to locate Previous and Next pager controls in or near the Season & Episode table.
    Returns a tuple (prev_button, next_button) which may contain None if not found.
    """
    prev_btn, next_btn = None, None

    # 1) Prefer controls located near the "x - y of z" label
    container = None
    marker_el = _get_range_marker_el(driver)
    if marker_el:
        try:
            container = marker_el.find_element(By.XPATH, "./ancestor::*[self::div or self::nav or self::footer or self::section][1]")
        except Exception:
            container = None

    def pick_buttons(scope):
        _prev, _next = None, None
        if scope is None:
            scope = driver
        # Broad query: include buttons, elements with role=button, and common MUI icon buttons
        candidates = scope.find_elements(By.XPATH,
            ".//button | .//*[@role='button'] | .//div[contains(@class,'MuiIconButton')] | .//span[contains(@class,'MuiIconButton')]")
        for b in candidates:
            label = (b.get_attribute('aria-label') or b.get_attribute('title') or b.text or '').strip().lower()
            classes = (b.get_attribute('class') or '').lower()

            # Expanded token sets for better robustness
            prev_label_tokens = ['previous', 'prev', 'go to previous page', 'first page', 'back']
            next_label_tokens = ['next', 'go to next page', 'last page', 'forward']
            prev_class_tokens = ['chevron_left', 'keyboard_arrow_left', 'keyboardarrowleft', 'navigate_before', 'arrow_back', 'first_page']
            next_class_tokens = ['chevron_right', 'keyboard_arrow_right', 'keyboardarrowright', 'navigate_next', 'arrow_forward', 'last_page']
            prev_symbols = {'‹', '<', '«', '⟨'}
            next_symbols = {'›', '>', '»', '⟩'}

            if any(tok in label for tok in prev_label_tokens) or any(tok in classes for tok in prev_class_tokens) or label in prev_symbols:
                _prev = _prev or b
            if any(tok in label for tok in next_label_tokens) or any(tok in classes for tok in next_class_tokens) or label in next_symbols:
                _next = _next or b
        return _prev, _next

    prev_btn, next_btn = pick_buttons(container)
    if not prev_btn and not next_btn:
        prev_btn, next_btn = pick_buttons(None)

    # Last-chance fallback: look for two icon buttons immediately following the marker
    if (not prev_btn or not next_btn) and marker_el:
        try:
            sibs = marker_el.find_elements(By.XPATH, "following::*[self::button or @role='button'][position()<=5]")
            if sibs:
                if len(sibs) >= 2:
                    prev_btn = prev_btn or sibs[0]
                    next_btn = next_btn or sibs[1]
                else:
                    next_btn = next_btn or sibs[0]
        except Exception:
            pass

    return prev_btn, next_btn

def _is_enabled(btn):
    if btn is None:
        return False
    try:
        disabled_attr = btn.get_attribute('disabled')
        aria_disabled = btn.get_attribute('aria-disabled')
        classes = (btn.get_attribute('class') or '').lower()
        return not (disabled_attr is not None or (aria_disabled and aria_disabled.lower() == 'true') or ('disabled' in classes) or ('mui-disabled' in classes))
    except Exception:
        return True

def _get_range_marker(driver):
    """
    Return the exact pager label like '1 - 20 of 30' if present, else ''.
    Clamps the upper bound to the reported total to avoid accidental cross-node matches.
    """
    try:
        el = _get_range_marker_el(driver)
        if not el:
            return ''
        txt = (el.text or '').strip()
        m = re.search(r"(\d+)\s*-\s*(\d+)\s*of\s*(\d+)", txt)
        if not m:
            return ''
        lo = int(m.group(1))
        hi = int(m.group(2))
        total = int(m.group(3))
        if hi > total:
            hi = total
        if lo > hi:
            lo, hi = hi, lo
        return f"{lo} - {hi} of {total}"
    except Exception:
        return ''

# --- Helper: return the DOM element for the range marker ---
def _get_range_marker_el(driver):
    """
    Return the DOM element that contains a label like '1 - 20 of 35', else None.
    """
    try:
        candidates = driver.find_elements(By.XPATH, "//*[self::div or self::span or self::p][contains(normalize-space(.),' of ') and contains(normalize-space(.),'-')]")
        for el in candidates:
            txt = (el.text or '').strip()
            if re.search(r'\d+\s*-\s*\d+\s*of\s*\d+', txt):
                return el
    except Exception:
        pass
    return None

# --- Helpers to anchor to the TablePagination widget ---

def _get_pagination_root(driver):
    """Return the nearest TablePagination root element using the marker as an anchor."""
    marker = _get_range_marker_el(driver)
    if not marker:
        return None
    node = marker
    for _ in range(6):
        try:
            if node.find_elements(By.CSS_SELECTOR, ".MuiTablePagination-actions, [class*='TablePagination-actions']"):
                return node
        except Exception:
            pass
        try:
            node = node.find_element(By.XPATH, "..")
        except Exception:
            break
    return marker


def _get_actions_container(driver):
    """Return the TablePagination actions container if available, else the pagination root."""
    root = _get_pagination_root(driver)
    if not root:
        return None
    try:
        return root.find_element(By.CSS_SELECTOR, ".MuiTablePagination-actions, [class*='TablePagination-actions']")
    except Exception:
        return root

# --- Generic click helpers and pager fallbacks ---

# --- Helpers: wait for pager state change and history reflow ---

def _wait_for_marker_change(driver, before_text, timeout=3.0):
    """Wait until the pager label changes or the marker element is re-rendered."""
    try:
        before_el = _get_range_marker_el(driver)
    except Exception:
        before_el = None
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: (
                (before_el is not None and EC.staleness_of(before_el)(d)) or
                (_get_range_marker(d) and _get_range_marker(d) != before_text)
            )
        )
        return True
    except Exception:
        return False

def _reflow_via_history(driver):
    """For single page apps, a quick back/forward can force a repaint of widgets."""
    try:
        driver.back()
        time.sleep(1.0)
    except Exception:
        pass
    try:
        driver.forward()
        time.sleep(1.2)
    except Exception:
        pass

def _click_el(driver, el):
    try:
        _scroll_into_view(driver, el)
        el.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            try:
                el.send_keys(Keys.SPACE)
                return True
            except Exception:
                return False

def _find_clickables_near_marker(driver):
    """
    Return likely pager controls that appear near the range marker 'x - y of z'.
    Searches only inside the nearest pager container and the marker itself
    (avoids picking global footer/help widgets).
    """
    marker_el = _get_range_marker_el(driver)
    candidates = []
    scopes = []

    if marker_el:
        # Nearest visual container that likely holds the pager
        try:
            container = marker_el.find_element(By.XPATH, "./ancestor::*[self::div or self::nav or self::footer or self::section][1]")
            scopes.append(container)
        except Exception:
            pass
        # The marker itself for neighbor-based queries **inside** this local scope only
        scopes.append(marker_el)
    else:
        # Fall back to the whole page (rare)
        scopes.append(driver)

    for scope in scopes:
        try:
            part = scope.find_elements(By.XPATH,
                ".//button | .//*[@role='button'] | .//*[contains(@class,'IconButton')] | .//*[contains(@class,'Pagination')]")
            candidates.extend(part)
        except Exception:
            pass

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for el in candidates:
        try:
            key = el.id
        except Exception:
            key = id(el)
        if key not in seen:
            seen.add(key)
            unique.append(el)

    # Only visible items
    filtered = []
    for el in unique:
        try:
            if el.is_displayed():
                filtered.append(el)
        except Exception:
            continue

    return filtered

def _pick_rightmost_clickable(cands):
    best, best_x = None, -1
    for el in cands:
        try:
            if not el.is_displayed():
                continue
            x = el.location.get('x', 0)
            if x > best_x:
                best = el
                best_x = x
        except Exception:
            continue
    return best

def _pick_leftmost_clickable(cands):
    best, best_x = None, 10**9
    for el in cands:
        try:
            if not el.is_displayed():
                continue
            x = el.location.get('x', 0)
            if x < best_x:
                best = el
                best_x = x
        except Exception:
            continue
    return best


# --- Pager helpers for direction-aware button selection ---
def _label_and_classes(el):
    label = (el.get_attribute('aria-label') or el.get_attribute('title') or el.text or '').strip().lower()
    classes = (el.get_attribute('class') or '').lower()
    return label, classes

# --- Helper: filter out help/feedback widgets ---
def _is_help_like(el):
    """Heuristic: identify help/feedback widgets so we never click them."""
    try:
        label, classes = _label_and_classes(el)
        text = (el.text or '').strip().lower()
        hay = f"{label} {text} {classes}"
        bad = ['help', 'get help', 'user guide', "what's new", 'zendesk', 'issue status', 'send feedback', 'get started']
        return any(tok in hay for tok in bad)
    except Exception:
        return False

def _choose_dir_button(cands, want_next=True):
    # First pass: look for explicit labels or arrow classes
    for el in cands:
        try:
            label, classes = _label_and_classes(el)
            if want_next:
                if (any(tok in label for tok in ['next', 'go to next page', 'last page', 'forward']) or
                    any(tok in classes for tok in ['chevron_right', 'keyboard_arrow_right', 'keyboardarrowright', 'navigate_next', 'arrow_forward', 'last_page']) or
                    label in {'›', '>', '»', '⟩'}):
                    return el
            else:
                if (any(tok in label for tok in ['previous', 'prev', 'go to previous page', 'first page', 'back']) or
                    any(tok in classes for tok in ['chevron_left', 'keyboard_arrow_left', 'keyboardarrowleft', 'navigate_before', 'arrow_back', 'first_page']) or
                    label in {'‹', '<', '«', '⟨'}):
                    return el
        except Exception:
            continue
    # Second pass: geometry fallback
    return _pick_rightmost_clickable(cands) if want_next else _pick_leftmost_clickable(cands)

def _click_next_page(driver):
    prev_btn, next_btn = _find_pager_buttons(driver)
    marker_before = _get_range_marker(driver)
    # Try explicit next button first
    if next_btn and _is_enabled(next_btn):
        if _click_el(driver, next_btn):
            if _wait_for_marker_change(driver, marker_before, timeout=2.5):
                return True
    # Fallback using candidates near the marker
    cands = _find_clickables_near_marker(driver)
    btn = _choose_dir_button(cands, want_next=True)
    if btn and _click_el(driver, btn):
        if _wait_for_marker_change(driver, marker_before, timeout=2.5):
            return True
    # Recovery: force a UI reflow, then retry once with explicit aria-label in the pager scope
    _reflow_via_history(driver)
    marker_before = _get_range_marker(driver)
    # Try again in local scope
    prev_btn2, next_btn2 = _find_pager_buttons(driver)
    if next_btn2 and _is_enabled(next_btn2) and _click_el(driver, next_btn2):
        if _wait_for_marker_change(driver, marker_before, timeout=2.5):
            return True
    # Last resort
    return False

def _click_prev_page(driver):
    prev_btn, next_btn = _find_pager_buttons(driver)
    marker_before = _get_range_marker(driver)

    # Try explicit prev button first
    if prev_btn and _is_enabled(prev_btn):
        if _click_el(driver, prev_btn):
            if _wait_for_marker_change(driver, marker_before, timeout=2.5):
                return True

    # Fallback using candidates near the marker
    cands = _find_clickables_near_marker(driver)

    # If we can see a Next button, choose the clickable immediately to its left, same row, not help-like
    btn = None
    try:
        if next_btn and cands:
            next_x = next_btn.location.get('x', 0)
            try:
                nr = next_btn.rect
                next_mid_y = (nr.get('y', 0) + nr.get('height', 0) / 2.0)
            except Exception:
                next_mid_y = None
            left_of_next = []
            for el in cands:
                try:
                    if _is_help_like(el):
                        continue
                    x = el.location.get('x', 0)
                    if x < next_x:
                        if next_mid_y is None:
                            left_of_next.append(el)
                        else:
                            r = el.rect
                            mid_y = r.get('y', 0) + r.get('height', 0) / 2.0
                            if abs(mid_y - next_mid_y) <= 30:
                                left_of_next.append(el)
                except Exception:
                    continue
            if left_of_next:
                chosen = _choose_dir_button(left_of_next, want_next=False)
                if chosen is None or chosen not in left_of_next:
                    chosen = _pick_rightmost_clickable(left_of_next)
                btn = chosen
    except Exception:
        btn = None

    if not btn:
        btn = _choose_dir_button(cands, want_next=False)

    if btn and _click_el(driver, btn):
        if _wait_for_marker_change(driver, marker_before, timeout=2.5):
            return True

    # Try a targeted fallback: click the "first page" control if present
    actions = _get_actions_container(driver)
    if actions:
        try:
            first_btn = None
            try:
                first_btn = actions.find_element(By.XPATH, ".//button[@aria-label='Go to first page']")
            except Exception:
                # Class-name based fallback
                cands = actions.find_elements(By.XPATH, ".//button | .//*[@role='button']")
                for b in cands:
                    cls = (b.get_attribute('class') or '').lower()
                    title = (b.get_attribute('title') or '').strip().lower()
                    if 'first_page' in cls or title == 'first page':
                        first_btn = b
                        break
            if first_btn and _is_enabled(first_btn) and _click_el(driver, first_btn):
                if _wait_for_marker_change(driver, marker_before, timeout=2.5):
                    return True
        except Exception:
            pass

    # Recovery: force a UI reflow, then retry once with explicit aria-label in the pager scope
    _reflow_via_history(driver)
    marker_before = _get_range_marker(driver)
    prev_btn2, next_btn2 = _find_pager_buttons(driver)
    if prev_btn2 and _is_enabled(prev_btn2) and _click_el(driver, prev_btn2):
        if _wait_for_marker_change(driver, marker_before, timeout=2.5):
            return True

    return False

def robust_clear_and_type(element, text, driver):
    """
    Aggressively clear a text field and type `text`.
    Works across Mac and Windows keyboard shortcuts and falls back to JS.
    """
    try:
        element.click()
    except Exception:
        pass
    # Try JS clear first
    try:
        driver.execute_script("arguments[0].value = '';", element)
    except Exception:
        pass
    time.sleep(0.05)
    # Try common select-all and delete sequences
    try:
        element.send_keys(Keys.COMMAND + "a")
        element.send_keys(Keys.DELETE)
    except Exception:
        pass
    try:
        element.send_keys(Keys.CONTROL + "a")
        element.send_keys(Keys.DELETE)
    except Exception:
        pass
    try:
        element.clear()
    except Exception:
        pass
    time.sleep(0.05)
    element.send_keys(text)

# --- New helper: try_select_autocomplete_series
def try_select_autocomplete_series(driver, wait, series_title):
    """
    Try to select a series from the autocomplete dropdown suggestions.
    If there are multiple exact-title matches, only auto-click when exactly one looks SERIES-like.
    SERIES-like is detected by either:
      - explicit 'series' label in option text, OR
      - presence of an SH id in option text/HTML.
    """
    try:
        # Wait for the popper or listbox to appear
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "ul[role='listbox'], div[role='listbox'], .MuiAutocomplete-popper [role='listbox']")) > 0)
        containers = driver.find_elements(By.CSS_SELECTOR, "ul[role='listbox'], div[role='listbox'], .MuiAutocomplete-popper [role='listbox']")
        if not containers:
            return False
        container = containers[0]

        def collect_options():
            opts = driver.find_elements(By.CSS_SELECTOR, "[role='option'], ul[role='listbox'] li, div[role='listbox'] li, [data-option-index]")
            if not opts:
                opts = driver.find_elements(By.XPATH, "//div[contains(@class,'Autocomplete') or contains(@class,'popper') or @role='listbox']//li")
            out = []
            for o in opts:
                try:
                    if o.is_displayed():
                        out.append(o)
                except Exception:
                    pass
            return out

        # Scroll to top first
        try:
            driver.execute_script("arguments[0].scrollTop = 0;", container)
        except Exception:
            pass

        # Walk the scrollable list to force lazy-load of all options
        seen_count = -1
        stable_iters = 0
        while True:
            options = collect_options()
            if len(options) == seen_count:
                stable_iters += 1
            else:
                stable_iters = 0
            seen_count = len(options)

            # Scroll down
            try:
                driver.execute_script("arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight * 0.9;", container)
            except Exception:
                try:
                    container.send_keys(Keys.END)
                except Exception:
                    pass

            if stable_iters >= 2:
                break
            time.sleep(0.15)

        options = collect_options()
        target_norm = normalize_text(extract_clean_title(series_title))

        exact = []
        for el in options:
            txt = (el.text or '').strip()
            title_clean = extract_clean_title(txt)
            if normalize_text(title_clean) != target_norm:
                continue
            inner = ''
            try:
                inner = el.get_attribute('innerHTML') or ''
            except Exception:
                inner = ''
            series_like = has_series_label(txt) or bool(extract_tms_id(txt, 'SH')) or bool(extract_tms_id(inner, 'SH'))
            exact.append((el, series_like))

        if not exact:
            return False

        # If there is exactly one exact-title option, click it.
        if len(exact) == 1:
            el = exact[0][0]
            try:
                _scroll_into_view(driver, el)
            except Exception:
                pass
            el.click()
            time.sleep(1.5)
            return True

        # Multiple exact matches: only click if exactly one is series-like
        series_only = [t for t in exact if t[1]]
        if len(series_only) == 1:
            el = series_only[0][0]
            try:
                _scroll_into_view(driver, el)
            except Exception:
                pass
            el.click()
            time.sleep(1.5)
            return True

        print_warning(f"Multiple autocomplete matches for '{series_title}' - letting grid/user resolve")
        return False
    except Exception:
        return False

def wait_for_manual_login(driver):
    print_header("Manual login required")
    print("\nPlease log in to Gracenote in the browser window.")
    print("Once logged in and you see the main page,")
    input("press ENTER here to continue...\n")
    print_success("Continuing with automation...")

def click_programs_sidebar(driver, wait):
    print_step("Clicking 'Programs' in sidebar...")
    try:
        # Wait a moment for page to be ready
        time.sleep(1)
        programs = wait.until(EC.element_to_be_clickable((By.LINK_TEXT, "Programs")))
        programs.click()
        time.sleep(2)
        print_success("Programs page loaded")
        return True
    except Exception as e:
        print_error(f"Error clicking Programs: {e}")
        return False

def select_series_filter(driver, wait):
    print_step("Selecting 'SERIES' filter...")
    timeout = float(CONFIG.get('wait_timeout', 10) or 10)
    end = time.time() + timeout

    def _looks_selected(el):
        try:
            ap = (el.get_attribute('aria-pressed') or '').strip().lower()
            if ap == 'true':
                return True
        except Exception:
            pass
        try:
            a = (el.get_attribute('aria-selected') or '').strip().lower()
            if a == 'true':
                return True
        except Exception:
            pass
        try:
            cls = (el.get_attribute('class') or '').lower()
            if 'selected' in cls or 'active' in cls or 'mui-selected' in cls:
                return True
        except Exception:
            pass
        return False

    # Anchor to the "Filter by Program Type" label if present
    anchor = None
    try:
        anchor = driver.find_element(By.XPATH, "//*[contains(normalize-space(.), 'Filter by Program Type')]")
    except Exception:
        anchor = None

    xpaths = [
        ".//following::*[(self::button or self::div or self::span) and normalize-space(.)='SERIES'][1]",
        ".//following::*[(self::button or self::div or self::span) and normalize-space(.)='Series'][1]",
        "//*[self::button or self::div or self::span][normalize-space(.)='SERIES']",
        "//*[@role='button' and normalize-space(.)='SERIES']",
        "//*[self::button or self::div or self::span][normalize-space(.)='Series']",
        "//*[@role='button' and normalize-space(.)='Series']",
    ]

    while time.time() < end:
        try:
            scope = anchor if anchor is not None else driver
            for xp in xpaths:
                try:
                    els = scope.find_elements(By.XPATH, xp)
                except Exception:
                    els = []
                for el in els:
                    try:
                        if not el.is_displayed():
                            continue
                        if _looks_selected(el):
                            print_success('SERIES filter already selected')
                            return True
                        if _click_el(driver, el):
                            time.sleep(0.6)
                            print_success('SERIES filter applied')
                            return True
                    except Exception:
                        continue
        except Exception:
            pass
        time.sleep(0.2)

    print_warning('SERIES filter not clicked (may already be selected). Continuing anyway.')
    return True

def search_for_series(driver, wait, series_title):
    print_step(f"Searching for series: '{series_title}'...")
    
    if not series_title or series_title.strip() == '':
        print_error("Series title is empty!")
        return False
    
    try:
        # Find the search input box
        search_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[placeholder*='Program' i]")))
        robust_clear_and_type(search_input, series_title, driver)
        time.sleep(0.4)

        # First try to click from autocomplete suggestions
        if try_select_autocomplete_series(driver, wait, series_title):
            print_success("Selected series from autocomplete")
            return True

        # Fallback: press Enter to show grid results, selection will happen in click_best_series_match
        search_input.send_keys(Keys.RETURN)
        time.sleep(2)
        print_success("Search submitted")
        return True
    except Exception as e:
        print_error(f"Error searching: {e}")
        return False

def click_best_series_match(driver, wait, series_title, state):
    print_step('Analyzing search results for best match...')
    try:
        if is_on_series_page(driver):
            print_success('Already on a series page')
            return 'ok'
    except Exception:
        pass

    # Wait briefly for results links
    try:
        wait.until(lambda d: len(d.find_elements(By.XPATH, "//a[contains(@href, '/program-details')]")) > 0)
    except Exception:
        print_error('No results found after search')
        return 'fail'

    results = driver.find_elements(By.XPATH, "//a[contains(@href, '/program-details')]")
    if not results:
        print_error('No series results found')
        return 'fail'

    norm_target = normalize_text(extract_clean_title(series_title))

    def _get_text(el):
        try:
            t = (el.text or '').strip()
            if t:
                return t
        except Exception:
            pass
        for attr in ('aria-label', 'title'):
            try:
                t = (el.get_attribute(attr) or '').strip()
                if t:
                    return t
            except Exception:
                pass
        try:
            t = (el.get_attribute('textContent') or '').strip()
            if t:
                return t
        except Exception:
            pass
        return ''

    exact = []
    for el in results:
        txt = _get_text(el)
        href = el.get_attribute('href') or ''
        title_clean = extract_clean_title(txt)
        is_series = bool(extract_tms_id(href, 'SH')) or has_series_label(txt) or bool(extract_tms_id(txt, 'SH'))
        if normalize_text(title_clean) == norm_target:
            exact.append((el, is_series))

    # One exact-title result: click it even if text lacks 'Series'
    if len(exact) == 1:
        if _click_el(driver, exact[0][0]):
            time.sleep(2)
            print_success('Series page loaded')
            return 'ok'
        return 'fail'

    # Multiple exact-title results: only auto-click if exactly one is series-like
    if len(exact) > 1:
        series_only = [t for t in exact if t[1]]
        if len(series_only) == 1:
            if _click_el(driver, series_only[0][0]):
                time.sleep(2)
                print_success('Series page loaded')
                return 'ok'
        print_warning(f"Multiple series named '{series_title}' found - deferring for user choice")
        return 'ambiguous'

    # Fallback: click the only result with an SH id in href
    sh_results = []
    for el in results:
        try:
            href = el.get_attribute('href') or ''
            if extract_tms_id(href, 'SH'):
                sh_results.append(el)
        except Exception:
            pass

    if len(sh_results) == 1:
        if _click_el(driver, sh_results[0]):
            time.sleep(2)
            print_success('Series page loaded (SH href fallback)')
            return 'ok'

    if len(sh_results) > 1:
        print_warning(f"Multiple SH series candidates for '{series_title}' - deferring for user choice")
        return 'ambiguous'

    print_warning('No exact SERIES match for title in results')
    return 'not_found'

def extract_series_tms_id(driver):
    """
    Return the Series SH TMS ID from the current page (URL or page source).
    """
    try:
        current_url = driver.current_url
        tms_id = extract_tms_id(current_url, 'SH')
        if tms_id:
            print_success(f"Series TMS ID found: {tms_id}")
            return tms_id

        # Try page content if not present in URL
        page_source = driver.page_source
        tms_id = extract_tms_id(page_source, 'SH')
        if tms_id:
            print_success(f"Series TMS ID found: {tms_id}")
            return tms_id

        print_warning("Series TMS ID not found")
        return ""
    except Exception as e:
        print_error(f"Error extracting series TMS ID: {e}")
        return ""

# --- Helper: best-effort check for series page context ---
def is_on_series_page(driver):
    """
    Best-effort check that the current page is a Series details page.
    """
    try:
        url = driver.current_url or ''
        if 'program-details' in url or extract_tms_id(url, 'SH'):
            return True
    except Exception:
        pass
    # Check for the tab label present on series pages
    try:
        driver.find_element(By.XPATH, "//*[contains(text(), 'Seasons & Episodes')]")
        return True
    except Exception:
        pass
    # Fallback: look for an SH id in the page source
    try:
        if extract_tms_id(driver.page_source or '', 'SH'):
            return True
    except Exception:
        pass
    return False

def click_series_by_sh_id(driver, wait, sh_id):
    """
    From a search results list, click the series whose link contains the given SH id.
    """
    try:
        # Prioritize anchors with href including the SH id
        link = wait.until(EC.element_to_be_clickable((By.XPATH, f"//a[contains(@href, '{sh_id}')]")))
        link.click()
        time.sleep(2)
        print_success(f"Selected series by SH id {sh_id}")
        return True
    except Exception:
        # Fallback: any anchor whose text includes the SH id
        try:
            link = driver.find_element(By.XPATH, f"//a[contains(., '{sh_id}')]")
            link.click()
            time.sleep(2)
            print_success(f"Selected series by SH id {sh_id} (fallback)")
            return True
        except Exception as e:
            print_warning(f"Could not select by SH id: {e}")
            return False


# --- New helpers for collecting series options for user selection (kept for internal use) ---
def _collect_series_from_autocomplete(driver, target_norm):
    """
    Scan the autocomplete listbox, scrolling to load all options.
    Return a list of candidates [{'el': element, 'text': text, 'sh': 'SH...'}]
    filtered to SERIES whose cleaned title matches target_norm.
    """
    try:
        containers = driver.find_elements(By.CSS_SELECTOR, "ul[role='listbox'], div[role='listbox'], .MuiAutocomplete-popper [role='listbox']")
        if not containers:
            return []
        container = containers[0]
        # Scroll to top
        try:
            driver.execute_script("arguments[0].scrollTop = 0;", container)
        except Exception:
            pass

        def collect():
            opts = driver.find_elements(By.CSS_SELECTOR, "[role='option'], ul[role='listbox'] li, div[role='listbox'] li, [data-option-index]")
            if not opts:
                opts = driver.find_elements(By.XPATH, "//div[contains(@class,'Autocomplete') or contains(@class,'popper') or @role='listbox']//li")
            # keep visible
            return [o for o in opts if o.is_displayed()]

        seen = -1
        stable = 0
        while True:
            options = collect()
            if len(options) == seen:
                stable += 1
            else:
                stable = 0
            seen = len(options)
            # Scroll down a page
            try:
                driver.execute_script("arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight * 0.95;", container)
            except Exception:
                try:
                    container.send_keys(Keys.END)
                except Exception:
                    pass
            if stable >= 2:
                break
            time.sleep(0.1)

        options = collect()
        out = []
        for el in options:
            txt = (el.text or '').strip()
            title_clean = extract_clean_title(txt)
            if normalize_text(title_clean) == target_norm and (has_series_label(txt) or bool(extract_tms_id(txt, 'SH')) or bool(extract_tms_id(el.get_attribute('innerHTML') or '', 'SH'))):
                sh = extract_tms_id(txt, 'SH') or extract_tms_id(el.get_attribute('innerHTML') or '', 'SH')
                out.append({'el': el, 'text': txt, 'sh': sh})
        return out
    except Exception:
        return []

def _collect_series_from_grid(driver, target_norm):
    """
    Collect SERIES candidates from the results grid.
    Return a list of candidates [{'el': element, 'text': text, 'sh': 'SH...'}]
    filtered to exact cleaned title matches.
    SERIES detection is broadened to include SH ids in href.
    """
    try:
        results = driver.find_elements(By.XPATH, "//a[contains(@href, '/program-details')]")
        out = []
        for el in results:
            txt = (el.text or '').strip()
            href = el.get_attribute('href') or ''
            title_clean = extract_clean_title(txt)
            is_series = ('SH' in href) or has_series_label(txt)
            if normalize_text(title_clean) == target_norm and is_series:
                sh = extract_tms_id(href, 'SH') or extract_tms_id(txt, 'SH') or extract_tms_id(el.get_attribute('outerHTML') or '', 'SH')
                out.append({'el': el, 'text': txt, 'sh': sh})
        return out
    except Exception:
        return []

# ---------------------------------------------------------------------------
# Pass 2 user selection is handled by clicking in the site UI
# ---------------------------------------------------------------------------
def resolve_series_with_user(driver, wait, series_title, state):
    """
    Pass 2: let the user click the correct SERIES in the browser UI.
    - Opens Programs, applies SERIES filter, types the title to show the drop-down.
    - You click the correct series option (or submit to grid and click the tile).
    - The function waits until a series details page is detected, then continues.
    """
    print_header(f"User selection required for '{series_title}'")
    series_key = normalize_text(series_title)

    # Go to Programs and apply SERIES filter
    if not click_programs_sidebar(driver, wait):
        return "fail"
    select_series_filter(driver, wait)

    # Focus search box and show the autocomplete
    try:
        search_input = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "input[placeholder*='Program' i]")
        ))
        robust_clear_and_type(search_input, series_title, driver)
        time.sleep(0.6)
    except Exception as e:
        print_error(f"Could not focus search input: {e}")
        return "fail"

    print("\nMultiple SERIES share this exact title.")
    print("Please do this in the browser window now:")
    print("  1) Use the Program search drop-down and CLICK the correct SERIES")
    print("     - or press Enter in the search box to open the grid, then CLICK the correct tile")
    resp = input("After you have clicked and the series page opens, press ENTER here to continue (or type 'skip' to skip this entire series)... ").strip().lower()
    if resp in ('skip', 's'):
        state.setdefault('skipped_series', set()).add(series_key)
        print_warning(f"User chose to skip series '{series_title}'")
        return "skip"

    # Wait briefly for the details page to be visible
    try:
        WebDriverWait(driver, 20).until(lambda d: is_on_series_page(d))
        print_success("Series page detected")
    except Exception:
        print_warning("Could not automatically confirm series page. Continuing")

    # Record the current series context for this run only
    sh_id = extract_series_tms_id(driver) or ''
    if sh_id:
        state.setdefault('series_choice_cache', {})[series_key] = sh_id
        print_success(f"Using SH id for this run: {sh_id}")
    else:
        print_warning("No SH id found on page. Proceeding without it")

    state['current_series'] = series_title
    state['on_seasons_tab'] = False
    state['current_season'] = None
    return "ok"


def click_seasons_episodes_tab(driver, wait):
    print_step("Clicking 'Seasons & Episodes' tab...")
    try:
        # Look for the tab - it might be a button or link
        tab = wait.until(EC.element_to_be_clickable((By.XPATH, 
            "//button[contains(text(), 'Seasons & Episodes')] | //a[contains(text(), 'Seasons & Episodes')] | //div[contains(text(), 'Seasons & Episodes')]")))
        tab.click()
        time.sleep(2)
        print_success("Seasons & Episodes tab loaded")
        return True
    except Exception as e:
        print_error(f"Error clicking Seasons & Episodes tab: {e}")
        # Try alternative
        try:
            print_step("Trying alternative selector...")
            tab = driver.find_element(By.XPATH, "//*[contains(text(), 'Seasons')]")
            tab.click()
            time.sleep(2)
            print_success("Seasons tab loaded")
            return True
        except:
            return False

# --- Helpers to ensure Seasons & Episodes tab context and first page ---

def _is_on_seasons_tab(driver):
    """Best-effort check that the Seasons & Episodes tab content is visible."""
    try:
        # Presence of the MUI displayed rows label strongly indicates the episodes table
        if _get_range_marker_el(driver):
            return True
    except Exception:
        pass
    try:
        # Common anchors within the tab content
        if driver.find_elements(By.XPATH, "//*[contains(text(), 'Season and Episode Summary') or contains(text(),'Export as .csv')]"):
            return True
    except Exception:
        pass
    return False


def _ensure_seasons_tab(driver, wait, timeout=6):
    """Open the Seasons & Episodes tab if not already open and wait for its content."""
    if _is_on_seasons_tab(driver):
        return True
    if not click_seasons_episodes_tab(driver, wait):
        return False
    # Wait for content of the tab to appear
    end = time.time() + timeout
    while time.time() < end:
        if _is_on_seasons_tab(driver):
            return True
        time.sleep(0.2)
    return _is_on_seasons_tab(driver)


def _wait_for_first_page(driver, timeout=4.0):
    """Wait until the pager shows it is on the first page (starts with 1 - ...)."""
    end = time.time() + timeout
    while time.time() < end:
        label = _get_range_marker(driver)
        if label:
            m = re.search(r"^(\d+)\s*-\s*(\d+)\s*of\s*(\d+)$", label)
            if m and int(m.group(1)) == 1:
                return True
        time.sleep(0.15)
    return False

def _read_current_season_value(driver):
    """Best-effort: read the currently selected season number from the UI."""
    # Native <select>
    try:
        dropdowns = driver.find_elements(By.XPATH, "//section//*[self::select] | //*[contains(., 'Season and Episode Summary')]/following::select[1] | //select")
        if dropdowns:
            sel = Select(dropdowns[0])
            try:
                return normalize_season_value(sel.first_selected_option.text)
            except Exception:
                return ''
    except Exception:
        pass

    # MUI / custom select
    try:
        cands = driver.find_elements(By.CSS_SELECTOR, "[role='combobox'], [aria-haspopup='listbox'], .MuiSelect-select")
        for c in cands:
            try:
                if c.is_displayed():
                    val = normalize_season_value(c.text)
                    if val:
                        return val
            except Exception:
                continue
    except Exception:
        pass

    return ''


# --- Helper: get the Season and Episode Summary container for dropdown scoping ---
def _get_season_summary_container(driver):
    """Return the nearest container around the 'Season and Episode Summary' header."""
    header = None
    try:
        # Prefer a displayed header element
        cands = driver.find_elements(By.XPATH, "//*[contains(normalize-space(.), 'Season and Episode Summary')]")
        for el in cands:
            try:
                if el.is_displayed():
                    header = el
                    break
            except Exception:
                continue
    except Exception:
        header = None

    if not header:
        return None

    node = header
    for _ in range(8):
        try:
            # If this ancestor contains the select display, use it as the scope
            if node.find_elements(By.CSS_SELECTOR, ".MuiSelect-select, [aria-haspopup='listbox'], [role='combobox']"):
                return node
        except Exception:
            pass
        try:
            node = node.find_element(By.XPATH, "..")
        except Exception:
            break
    return header


# --- Helper: open the season dropdown (Material UI or similar) ---
def _open_season_dropdown(driver, wait, timeout=4.0):
    """Open the Season dropdown on the Seasons & Episodes tab and return True if the menu appears."""
    def _visible_menu_items():
        # MUI menu is often rendered in a portal; look for visible items globally but require a menu-like ancestor
        items = []
        xps = [
            "//*[@role='listbox']//*[self::li or self::div][starts-with(normalize-space(.), 'Season ') or normalize-space(.)='No Season']",
            "//*[contains(@class,'MuiMenu') or contains(@class,'MuiPopover') or contains(@class,'MuiPaper')]//*[self::li or self::div][starts-with(normalize-space(.), 'Season ') or normalize-space(.)='No Season']",
            "//li[contains(@class,'MuiMenuItem')][starts-with(normalize-space(.), 'Season ') or normalize-space(.)='No Season']",
        ]
        for xp in xps:
            try:
                els = driver.find_elements(By.XPATH, xp)
            except Exception:
                els = []
            for el in els:
                try:
                    if el.is_displayed():
                        items.append(el)
                except Exception:
                    continue
        return items

    # If the menu is already open, do not click again (clicking would close it)
    if _visible_menu_items():
        return True

    # Prefer to find the trigger inside the Season and Episode Summary section
    scope = _get_season_summary_container(driver) or driver

    trigger = None
    try:
        # The visible box is usually the .MuiSelect-select element
        cands = scope.find_elements(By.CSS_SELECTOR, ".MuiSelect-select")
        for el in cands:
            try:
                if el.is_displayed():
                    trigger = el
                    break
            except Exception:
                continue
    except Exception:
        trigger = None

    # 2) Fallback: any listbox trigger
    if trigger is None:
        try:
            cands = scope.find_elements(By.CSS_SELECTOR, "[aria-haspopup='listbox'], [role='combobox']")
            for el in cands:
                try:
                    if el.is_displayed():
                        trigger = el
                        break
                except Exception:
                    continue
        except Exception:
            trigger = None

    # 2b) Fallback: button-like element that contains the word 'Season' (older working approach)
    if trigger is None:
        try:
            triggers = scope.find_elements(
                By.XPATH,
                ".//*[self::div or self::button or self::span][(contains(@role,'button') or @role='combobox' or contains(@class,'select') or contains(@class,'MuiSelect') or @aria-haspopup='listbox') and contains(normalize-space(.), 'Season')]"
            )
            for el in triggers:
                try:
                    if el.is_displayed():
                        trigger = el
                        break
                except Exception:
                    continue
        except Exception:
            trigger = None

    # 2c) Fallback: first select-like control near the Season and Episode Summary section
    if trigger is None:
        try:
            triggers = scope.find_elements(
                By.XPATH,
                ".//*[self::div or self::button][@role='button' or @role='combobox' or contains(@class,'select') or contains(@class,'MuiSelect') or @aria-haspopup='listbox']"
            )
            for el in triggers:
                try:
                    if el.is_displayed():
                        trigger = el
                        break
                except Exception:
                    continue
        except Exception:
            trigger = None

    # Last resort: global search
    if trigger is None:
        try:
            cands = driver.find_elements(By.CSS_SELECTOR, "[aria-haspopup='listbox'], [role='combobox'], .MuiSelect-select")
            for el in cands:
                try:
                    if el.is_displayed():
                        trigger = el
                        break
                except Exception:
                    continue
        except Exception:
            trigger = None

    if not trigger:
        return False

    if not _click_el(driver, trigger):
        return False

    end = time.time() + timeout
    while time.time() < end:
        if _visible_menu_items():
            return True
        time.sleep(0.1)

    return False

def select_season(driver, wait, season_number):
    raw = str(season_number or '').strip()
    desired_num = normalize_season_value(raw)
    want_no_season = (not desired_num) and (raw == '' or 'no season' in raw.lower() or raw == '0')
    desired = desired_num or ('0' if want_no_season else '1')
    print_step(f"Selecting Season {desired}...")
    # 1) Try native <select>
    try:
        # Prefer a select near the Season and Episode Summary section
        dropdowns = driver.find_elements(By.XPATH, "//section//*[self::select] | //*[contains(., 'Season and Episode Summary')]/following::select[1] | //select")
        if dropdowns:
            dropdown = dropdowns[0]
            sel = Select(dropdown)
            # Try visible text variations first
            tried = False
            for text in (("No Season",) if desired == '0' else (f"Season {desired}", f"S{desired}", desired)):
                try:
                    sel.select_by_visible_text(text)
                    tried = True
                    break
                except Exception:
                    pass
            if not tried:
                try:
                    sel.select_by_value(desired)
                    tried = True
                except Exception:
                    pass
            if not tried:
                # Last resort, iterate options and click the one whose number matches
                for opt in sel.options:
                    if normalize_season_value(opt.text) == desired or normalize_season_value(opt.get_attribute("value")) == desired:
                        opt.click()
                        tried = True
                        break
            if tried:
                time.sleep(1.5)
                print_success(f"Season {desired} selected")
                return True
    except Exception as e:
        print_warning(f"Native select path failed: {e}")

    # 2) Try custom dropdowns (Material UI or similar)
    try:
        if not _open_season_dropdown(driver, wait, timeout=4.0):
            raise Exception("Season dropdown did not open")

        options = driver.find_elements(
            By.XPATH,
            "//*[@role='listbox']//*[@role='option'] | //*[@role='listbox']//li | //ul[contains(@class,'MuiMenu-list')]//li | //li[contains(@class,'MuiMenuItem')]"
        )

        visible = []
        for el in options:
            try:
                if el.is_displayed():
                    visible.append(el)
            except Exception:
                continue

        best = None
        want = ("No Season" if desired == '0' else f"Season {desired}")
        for el in visible:
            try:
                txt = (el.text or '').strip()
                if txt == want:
                    best = el
                    break
            except Exception:
                continue

        # Secondary fallback: allow exact numeric-only option text
        if best is None:
            for el in visible:
                try:
                    txt = (el.text or '').strip()
                    if txt == desired:
                        best = el
                        break
                except Exception:
                    continue

        if best and _click_el(driver, best):
            time.sleep(1.2)
            cur = _read_current_season_value(driver)
            if cur and cur != desired:
                print_warning(f"Season picker readback '{cur}' did not match desired '{desired}'")
            print_success(f"Season {desired} selected")
            return True

        try:
            driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
        except Exception:
            pass

    except Exception as e:
        print_warning(f"Custom dropdown path failed: {e}")

    print_warning(f"Could not select season {desired}")
    return False


# --- New helpers: get_available_seasons and find_episode_across_all_seasons ---

def get_available_seasons(driver):
    """Best-effort: return a sorted list of season numbers (as strings) available in the season dropdown."""
    seasons = set()

    # 1) Native <select> options
    try:
        dropdowns = driver.find_elements(By.XPATH, "//section//*[self::select] | //*[contains(., 'Season and Episode Summary')]/following::select[1] | //select")
        if dropdowns:
            sel = Select(dropdowns[0])
            for opt in sel.options:
                txt = (opt.text or '').strip()
                if txt.lower() == 'no season':
                    seasons.add('0')
                    continue
                v = normalize_season_value(txt) or normalize_season_value(opt.get_attribute('value'))
                if v:
                    seasons.add(v)
    except Exception:
        pass

    # 2) Material UI style dropdown options (open and read visible menu items)
    if not seasons:
        try:
            opened = _open_season_dropdown(driver, WebDriverWait(driver, 4), timeout=4.0)
            if not opened:
                print_warning("Season dropdown did not open for enumeration")
            else:
                # Read visible menu items like 'Season 5' / 'No Season'
                candidates = driver.find_elements(By.XPATH,
                    "//*[@role='listbox']//*[self::li or self::div][starts-with(normalize-space(.), 'Season ') or normalize-space(.)='No Season'] | "
                    "//*[contains(@class,'MuiMenu') or contains(@class,'MuiPopover') or contains(@class,'MuiPaper')]//*[self::li or self::div][starts-with(normalize-space(.), 'Season ') or normalize-space(.)='No Season'] | "
                    "//li[contains(@class,'MuiMenuItem')][starts-with(normalize-space(.), 'Season ') or normalize-space(.)='No Season']"
                )
                for el in candidates:
                    try:
                        if not el.is_displayed():
                            continue
                        txt = (el.text or '').strip()
                        if txt.lower() == 'no season':
                            seasons.add('0')
                            continue
                        m = re.match(r'^Season\s+(\d+)$', txt)
                        if m:
                            seasons.add(m.group(1))
                    except Exception:
                        continue

                # Close menu (ESC)
                try:
                    driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
                except Exception:
                    pass

                if seasons:
                    try:
                        ordered = [str(x) for x in sorted({int(s) for s in seasons})]
                    except Exception:
                        ordered = sorted(list(seasons))
                    print_step(f"Available seasons detected: {', '.join(ordered)}")
                    print_step(f"Season count: {len(ordered)}")
        except Exception:
            pass

    # Sort numerically, but keep '0' ("No Season") first if present
    try:
        return [str(x) for x in sorted({int(s) for s in seasons})]
    except Exception:
        # Fallback: keep '0' first if present
        out = sorted([s for s in seasons if s != '0'])
        if '0' in seasons:
            out = ['0'] + out
        return out


def find_episode_across_all_seasons(driver, wait, episode_title):
    """Loop seasons and search within each season until the episode is found."""
    if not _ensure_seasons_tab(driver, wait):
        return "", "Failed to load Seasons & Episodes tab"

    seasons = get_available_seasons(driver)
    if not seasons:
        # Fallback: we can only scan the currently visible season
        print_warning("Could not enumerate seasons. Scanning current season only.")
        return find_episode_tms_id(driver, wait, episode_title, current_season=None)

    for s in seasons:
        print_step(f"Search All Seasons: selecting Season {s}")
        if not select_season(driver, wait, s):
            continue
        _wait_for_first_page(driver, timeout=4.0)
        ep_id, note = find_episode_tms_id(driver, wait, episode_title, current_season=s)
        if ep_id:
            return ep_id, ""

    return "", "Episode not found in any season - manual verification needed"

def find_episode_tms_id(driver, wait, episode_title, current_season=None):
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
            else:
                print_warning("Could not open Seasons & Episodes tab for reset")

        print_warning("All pages have been looked at. Episode not found")
        return "", "Episode not found - manual verification needed"

    except Exception as e:
        print_error(f"Error finding episode: {e}")
        return "", f"Error: {e}"

def process_episode(driver, wait, episode, index, total, state, allow_defer=True):
    print(f"\n\n📺 Episode {index + 1}/{total}")
    print("─" * 70)
    
    # Try multiple column name variations
    series_title = (episode.get('SeriesTitle') or 
                   episode.get('Series') or 
                   episode.get('series_title') or 
                   episode.get('Show') or '').strip()
    series_key = normalize_text(series_title)
    # If this series was already marked for deferred resolution, skip immediately on first pass
    if allow_defer and (series_key in state.get('ambiguous_series', set()) or series_key in state.get('unresolved_series', set())):
        episode['Notes'] = "Deferred entire series awaiting user selection"
        state.setdefault('deferred', []).append(episode)
        print_warning(f"Skipping episode for deferred series '{series_title}'")
        return
    
    episode_title = (episode.get('EpisodeTitle') or 
                    episode.get('Title') or 
                    episode.get('episode_title') or 
                    episode.get('Episode') or '').strip()
    
    raw_season = (episode.get('Season') or episode.get('season') or '1')
    season = normalize_season_value(raw_season) or '1'
    
    print(f"   Series: \"{series_title}\"")
    print(f"   Episode: \"{episode_title}\"")
    print(f"   Season: {season}")


    # Consider both canonical and alternate column names
    prefilled_ep_tms = _get_first(episode, ['EpisodeTMSID', 'Episode TMS ID', 'Episode_TMS_ID'])
    prefilled_series_tms = _get_first(episode, ['SeriesTMSID', 'Series TMS ID', 'Series_TMS_ID'])

    if _value_means_done(prefilled_ep_tms) or _value_is_one(prefilled_series_tms):
        reason_bits = []
        if prefilled_ep_tms:
            reason_bits.append(f"EpisodeTMSID={prefilled_ep_tms}")
        if prefilled_series_tms:
            reason_bits.append(f"SeriesTMSID={prefilled_series_tms}")
        reason = "; ".join(reason_bits) if reason_bits else "pre-filled markers"
        print_step(f"TMS already present or marked as 1 ({reason}) - skipping row")
        return
    
    # Check for empty values
    if not series_title:
        print_error("Series title is empty - skipping")
        episode['SeriesTMSID'] = ''
        episode['EpisodeTMSID'] = ''
        episode['Notes'] = 'Series title is empty'
        return
    
    if not episode_title:
        print_error("Episode title is empty - skipping")
        episode['SeriesTMSID'] = ''
        episode['EpisodeTMSID'] = ''
        episode['Notes'] = 'Episode title is empty'
        return
    
    episode['SeriesTMSID'] = episode.get('SeriesTMSID', '')
    episode['EpisodeTMSID'] = episode.get('EpisodeTMSID', '')
    episode['Notes'] = episode.get('Notes', '')
    
    try:
        # Use cached choice if available (in-run only)
        cached_sh = state.get('series_choice_cache', {}).get(series_key)
        # Sanity check: if state says we are on this series, verify page context and SH id
        if state.get('current_series') == series_title:
            if not is_on_series_page(driver):
                print_warning("Not currently on a series page - forcing reselect of series")
                state['current_series'] = None
                state['on_seasons_tab'] = False
                state['current_season'] = None
            else:
                try:
                    page_sh = extract_series_tms_id(driver)
                except Exception:
                    page_sh = ''
                if cached_sh and page_sh and cached_sh != page_sh:
                    print_warning(f"Series SH mismatch (page {page_sh} vs cached {cached_sh}) - forcing reselect of series")
                    state['current_series'] = None
                    state['on_seasons_tab'] = False
                    state['current_season'] = None
        if state.get('current_series') != series_title:
            if not click_programs_sidebar(driver, wait):
                episode['Notes'] = "Failed to click Programs"
                return

            select_series_filter(driver, wait)

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
  



  
        # --- START OF MODIFICATION ---
        # Determine if we should skip season selection

        skip_season_selection = (not allow_defer) and CONFIG.get('ignore_season_on_second_pass', False)
        if skip_season_selection:
            print_step("Search All Seasons active: will scan every season in the series...")
        else:
            # Ensure correct season is selected (original logic)
            if state.get('current_season') != season:
                print_step(f"Selecting season: {season}")
                if select_season(driver, wait, season):
                    state['current_season'] = season
                    time.sleep(1)
        # --- END OF MODIFICATION ---
        
        print_step(f"Looking for episode: {episode_title}")
        if skip_season_selection:
            episode_tms_id, note = find_episode_across_all_seasons(driver, wait, episode_title)
        else:
            episode_tms_id, note = find_episode_tms_id(driver, wait, episode_title, season)
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
