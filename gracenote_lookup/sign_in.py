"""Shared login handoff for the desktop app and CLI."""
from .runtime import runtime


def finish_sign_in(browser):
    browser.start_sign_in()
    if runtime.await_login is not None:
        runtime.await_login(browser)
    else:
        runtime.ask('Sign in to Gracenote in Chrome, then press ENTER here.')
    runtime.check()
    runtime.log('Signed in. Opening Programs…')
    browser.open_programs()
