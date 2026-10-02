"""Site controls verified against Gracenote View on 2026-10-02."""
import re
from urllib.parse import urlparse

from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .matching import canonicalize_title_for_match, extract_tms_id, normalize_season_value
from .pagination import collect_pages, snapshot, wait_ready, PaginationError
from .runtime import runtime

BASE_URL = "https://gracenoteview.com"


class GracenoteBrowser:
    def __init__(self, driver, timeout=15):
        self.driver = driver
        self.timeout = timeout
        self.catalogs = {}
        self.seasons_cache = {}

    def wait(self, predicate, message="Gracenote did not finish loading"):
        def check(driver):
            runtime.check()
            try:
                return predicate(driver)
            except (NoSuchElementException, StaleElementReferenceException):
                return False
        return WebDriverWait(self.driver, self.timeout, poll_frequency=0.25).until(check, message)

    def selected_id(self, prefix='SH'):
        parsed = urlparse(self.driver.current_url)
        if parsed.hostname != "gracenoteview.com":
            return ""
        return extract_tms_id(parsed.path, prefix) or ""

    def verify_series(self, title, expected_id=None):
        def confirmed(driver):
            identifier = self.selected_id()
            if not identifier or (expected_id and identifier != expected_id):
                return False
            titles = [el.text for el in driver.find_elements(By.CSS_SELECTOR, ".program-title") if el.is_displayed()]
            tabs = driver.find_elements(By.CSS_SELECTOR, "input[value='Seasons & Episodes']")
            return bool(tabs) and any(canonicalize_title_for_match(t) == canonicalize_title_for_match(title) for t in titles)
        self.wait(confirmed, "Open the correct series detail page, then Continue.")
        return self.selected_id()

    def search(self, title, prefix='SH', year=''):
        self.driver.get(BASE_URL + "/programs")
        search = self.wait(lambda d: EC.element_to_be_clickable((By.CSS_SELECTOR, "input[placeholder='Enter Program by Name']"))(d))
        search.click()
        search.send_keys(Keys.COMMAND if __import__('sys').platform == 'darwin' else Keys.CONTROL, "a")
        search.send_keys(Keys.BACKSPACE)
        search.send_keys(title)

        def results(driver):
            options = driver.find_elements(By.CSS_SELECTOR, "[role='listbox'] [role='option']")
            # The initial listbox contains a 'Type to search...' placeholder.
            return options if any(o.find_elements(By.CSS_SELECTOR, ".program-tmsid") for o in options) else False
        try:
            options = self.wait(results, "No program search results appeared")
        except Exception:
            return []  # Second pass gives the user a chance to select manually.
        exact = []
        for option in options:
            titles = option.find_elements(By.CSS_SELECTOR, ".program-title")
            badges = option.find_elements(By.CSS_SELECTOR, ".badge-text")
            ids = option.find_elements(By.CSS_SELECTOR, ".program-tmsid")
            types = ('series', 'miniseries') if prefix == 'SH' else ('tv movie', 'feature film', 'film', 'movie')
            years = option.find_elements(By.CSS_SELECTOR, '.dd-sub-title')
            requested_year = re.search(r'\b\d{4}\b', str(year))
            if (titles and ids and any(b.text.strip().lower() in types for b in badges)
                    and (not requested_year or any(requested_year[0] in re.findall(r'\b\d{4}\b', y.text) for y in years))
                    and canonicalize_title_for_match(titles[0].text) == canonicalize_title_for_match(title)):
                identifier = extract_tms_id(ids[0].text, prefix)
                if identifier:
                    exact.append((identifier, option))
        return exact

    def open_series(self, title, identifier="", manual=False):
        if identifier:
            if self.selected_id() != identifier:
                self.driver.get(BASE_URL + "/programs/" + identifier)
            return self.verify_series(title, identifier)
        candidates = self.search(title)
        if not manual:
            if len(candidates) != 1:
                return ""
            identifier, option = candidates[0]
            option.click()
            return self.verify_series(title, identifier)
        answer = runtime.ask(f'Select the correct SERIES for “{title}” in Chrome, then click Continue. '
                             'Click Skip this series to skip its episodes.')
        runtime.check()
        if answer.strip().lower() == 'skip':
            return 'skip'
        return self.verify_series(title)

    def verify_movie(self, title, expected_id=None):
        def confirmed(driver):
            identifier = self.selected_id('MV')
            titles = [el.text for el in driver.find_elements(By.CSS_SELECTOR, '.program-title') if el.is_displayed()]
            return (identifier and (not expected_id or identifier == expected_id) and
                    any(canonicalize_title_for_match(t) == canonicalize_title_for_match(title) for t in titles))
        self.wait(confirmed, 'Open the correct movie detail page, then Continue.')
        return self.selected_id('MV')

    def open_movie(self, title, year='', manual=False):
        candidates = self.search(title, 'MV', year)
        if not manual:
            if len(candidates) != 1:
                return ''
            identifier, option = candidates[0]
            option.click()
            return self.verify_movie(title, identifier)
        answer = runtime.ask(f'Select the correct MOVIE for “{title}” ({year or "year unknown"}) in Chrome, '
                             'then click Continue. Click Skip to leave its ID blank.')
        runtime.check()
        return 'skip' if answer.strip().lower() == 'skip' else self.verify_movie(title)

    def open_episodes(self):
        tab = self.wait(lambda d: d.find_element(By.CSS_SELECTOR, "input[value='Seasons & Episodes']"))
        label = tab.find_element(By.XPATH, "..")
        if 'active' not in (label.get_attribute('class') or '').split():
            label.click()
        self.wait(lambda d: d.find_elements(By.CSS_SELECTOR, ".select-search__input"))

    def season_options(self):
        self.open_episodes()
        identifier = self.selected_id()
        if identifier in self.seasons_cache:
            return self.seasons_cache[identifier]
        picker = self.wait(lambda d: EC.element_to_be_clickable((By.CSS_SELECTOR, ".select-search__input"))(d))
        picker.click()
        try:
            options = self.wait(lambda d: d.find_elements(By.CSS_SELECTOR, ".select-search [role='menuitem'] button"))
            seasons = [(normalize_season_value(o.text), o.text) for o in options if normalize_season_value(o.text)]
            if not seasons:
                raise PaginationError('Cannot read available seasons')
            self.seasons_cache[identifier] = seasons
            return seasons
        finally:
            picker.send_keys(Keys.ESCAPE)
            # Some react-select-search versions close on blur rather than Escape.
            self.driver.find_element(By.CSS_SELECTOR, ".program-title").click()

    def select_season(self, season):
        self.open_episodes()
        options = self.season_options()
        desired = normalize_season_value(season) or '1'
        label = next((label for number, label in options if number == desired), None)
        if label is None:
            raise PaginationError(f'Season {season} is not offered for this series')
        picker = self.driver.find_element(By.CSS_SELECTOR, ".select-search__input")
        if (picker.get_attribute('value') or '').strip() != label:
            old_signature = wait_ready(lambda: snapshot(self.driver), self.timeout).signature
            picker.click()
            menu = self.wait(lambda d: d.find_elements(By.CSS_SELECTOR, ".select-search [role='menuitem'] button"))
            matches = [el for el in menu if el.text.strip() == label]
            if len(matches) != 1:
                raise PaginationError(f'Cannot uniquely select {label}')
            matches[0].click()
            self.wait(lambda d: (d.find_element(By.CSS_SELECTOR, '.select-search__input').get_attribute('value') or '').strip() == label)
            def refreshed(driver):
                try:
                    return snapshot(driver).signature != old_signature
                except PaginationError:
                    return False
            self.wait(refreshed, 'Episode rows did not refresh for the selected season')
        return wait_ready(lambda: snapshot(self.driver), self.timeout)

    def catalog(self, season):
        identifier = self.selected_id()
        key = identifier, normalize_season_value(season) or '1'
        if key not in self.catalogs:
            self.select_season(season)
            self.catalogs[key] = collect_pages(lambda: snapshot(self.driver), lambda button: button.click(),
                                               runtime.log, self.timeout)
        return self.catalogs[key]

    def all_episodes(self):
        return tuple(ep for number, label in self.season_options() for ep in self.catalog(number))
