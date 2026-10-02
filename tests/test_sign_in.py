import unittest
from threading import Event

from selenium.webdriver.common.by import By

from gracenote_lookup.browser import BASE_URL, LOGIN_CLIENT, LOGIN_HOST, GracenoteBrowser, SignInError
from gracenote_lookup.runtime import runtime
from gracenote_lookup.sign_in import finish_sign_in


class Element:
    def __init__(self, callback=lambda: None, visible=True):
        self.callback = callback
        self.visible = visible
        self.values = []

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return True

    def click(self):
        self.callback()

    def clear(self):
        self.values.clear()

    def send_keys(self, value):
        self.values.append(value)


class Driver:
    def __init__(self):
        self.current_url = BASE_URL + '/schedules'
        self.mode = 'id'
        self.clicks = []
        self.username, self.password = Element(), Element()

    def find_elements(self, by, selector):
        if selector.startswith('a[href="/programs"]'):
            def programs():
                self.clicks.append('Programs')
                self.current_url = BASE_URL + '/programs'
            return [Element(programs)]
        if by == By.XPATH and 'Program Title' in selector and self.current_url.endswith('/programs'):
            def title_mode():
                self.clicks.append('Program Title')
                self.mode = 'title'
            return [Element(title_mode)]
        if selector.startswith('input[placeholder='):
            return [Element()] if self.current_url.endswith('/programs') and self.mode == 'title' else []
        if selector == 'input[name="username"]':
            return [Element(visible=False), self.username]
        if selector.startswith('input[name="password"]'):
            return [Element(visible=False), self.password]
        return []

    def find_element(self, by, selector):
        return self.find_elements(by, selector)[0]


class SignInTests(unittest.TestCase):
    def setUp(self):
        previous = vars(runtime).copy()
        self.addCleanup(lambda: vars(runtime).update(previous))
        runtime.cancelled = Event()
        runtime.log = lambda text: None

    def test_schedules_landing_clicks_programs_and_selects_title_mode(self):
        driver = Driver()
        browser = GracenoteBrowser(driver)
        self.assertTrue(browser.signed_in())
        browser.open_programs()
        self.assertEqual(driver.clicks, ['Programs', 'Program Title'])
        self.assertEqual(driver.current_url, BASE_URL + '/programs')

    def test_automatic_login_does_not_wait_for_continue(self):
        calls = []
        class Browser:
            def start_sign_in(self): calls.append('start')
            def open_programs(self): calls.append('Programs')
        runtime.await_login = lambda browser: calls.append('detected sign-in')
        runtime.ask = lambda text: self.fail('Desktop login must not wait for Continue')
        finish_sign_in(Browser())
        self.assertEqual(calls, ['start', 'detected sign-in', 'Programs'])

    def test_invalidated_session_is_reported_instead_of_waiting_forever(self):
        driver = Driver()
        driver.current_url = BASE_URL + '/auth-forbidden'
        with self.assertRaisesRegex(SignInError, 'rejected this session'):
            GracenoteBrowser(driver).signed_in()

    def test_credentials_fill_only_visible_fields_on_verified_login_origin(self):
        driver = Driver()
        driver.current_url = (f'https://{LOGIN_HOST}/login?client_id={LOGIN_CLIENT}'
                              '&redirect_uri=https%3A%2F%2Fgracenoteview.com%2Flogin')
        GracenoteBrowser(driver).fill_saved_sign_in('example@example.com', 'synthetic-password')
        self.assertEqual(driver.username.values, ['example@example.com'])
        self.assertEqual(driver.password.values, ['synthetic-password'])
        self.assertEqual(driver.clicks, [])  # No automatic submission.

    def test_untrusted_origin_client_or_redirect_never_receives_credentials(self):
        driver = Driver()
        urls = ['https://example.com/login',
                f'https://{LOGIN_HOST}/login?client_id=wrong&redirect_uri=https%3A%2F%2Fgracenoteview.com%2Flogin',
                f'https://{LOGIN_HOST}/login?client_id={LOGIN_CLIENT}&redirect_uri=https%3A%2F%2Fexample.com',
                f'http://{LOGIN_HOST}/login?client_id={LOGIN_CLIENT}&redirect_uri=https%3A%2F%2Fgracenoteview.com%2Flogin']
        for url in urls:
            driver.current_url = url
            with self.assertRaises(SignInError):
                GracenoteBrowser(driver).fill_saved_sign_in('example', 'synthetic-password')
            self.assertEqual(driver.username.values, [])
            self.assertEqual(driver.password.values, [])

    def test_movie_search_clears_all_type_filters_including_series(self):
        driver = Driver()
        clicked = []
        controls = [{'name': name, 'selected': selected, 'element': Element(lambda n=name: clicked.append(n))}
                    for name, selected in [('SERIES', True), ('FILM', True), ('TV MOVIE', False)]]
        driver.execute_script = lambda script: controls
        GracenoteBrowser(driver).configure_type_filters('MV')
        self.assertEqual(clicked, ['SERIES', 'FILM'])
        clicked.clear()
        controls[0]['selected'] = False
        controls[1]['selected'] = False
        GracenoteBrowser(driver).configure_type_filters('SH')
        self.assertEqual(clicked, ['SERIES'])
