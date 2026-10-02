"""Callbacks shared by the CLI and desktop worker; no Qt dependency."""
import builtins
from dataclasses import dataclass, field
from threading import Event


class RunCancelled(BaseException):
    """Bypass legacy broad Exception handlers when the user stops a run."""


@dataclass
class Runtime:
    log: object = builtins.print
    ask: object = builtins.input
    progress: object = lambda current, total: None
    rows: object = lambda rows: None
    await_login: object = None
    cancelled: Event = field(default_factory=Event)

    def check(self):
        if self.cancelled.is_set():
            raise RunCancelled()


runtime = Runtime()


def log(*args, sep=" ", end="\n", **kwargs):
    runtime.check()
    runtime.log(sep.join(str(a) for a in args) + (end if end != "\n" else ""))


def ask(prompt):
    runtime.check()
    answer = runtime.ask(prompt)
    runtime.check()
    return answer
