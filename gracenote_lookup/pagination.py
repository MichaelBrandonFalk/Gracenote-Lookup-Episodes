"""Read one table snapshot at a time, with verified, single-click pagination."""
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher

from .runtime import runtime


class PaginationError(Exception):
    pass


class SnapshotUnavailable(PaginationError):
    """The table is temporarily absent while Gracenote loads it."""


RANGE = re.compile(r"^(\d+)\s*[-–—]\s*(\d+)\s+of\s+(\d+)$", re.I)


def parse_range(text):
    match = RANGE.fullmatch(text.strip().replace(",", ""))
    if not match:
        raise PaginationError(f"Cannot read episode page range: {text!r}")
    lo, hi, total = map(int, match.groups())
    # The live Gracenote server table shows e.g. "21 - 40 of 28" on
    # its last page. Only eight rows are expected, despite the padded end.
    hi = min(hi, total)
    if (total == 0 and (lo, hi) != (0, 0)) or (total > 0 and not 1 <= lo <= hi <= total):
        raise PaginationError(f"Invalid episode page range: {text!r}")
    return lo, hi, total


@dataclass(frozen=True)
class Episode:
    title: str
    tms_id: str
    episode_number: str = ""
    part: str = ""


@dataclass
class Snapshot:
    marker: str
    episodes: tuple
    previous: object = None
    next: object = None
    marker_element: object = None

    @property
    def bounds(self):
        return parse_range(self.marker)

    @property
    def signature(self):
        return tuple((ep.title, ep.tms_id, ep.episode_number, ep.part) for ep in self.episodes)


# All row strings and controls come from the same DOM read. Never click by screen
# position or guess among unrelated page buttons. First/last controls are excluded.
SNAPSHOT_SCRIPT = r"""
const visible = el => el.getClientRects().length > 0;
const idPattern = /\bEP\d{12}\b/i;
const tables = [...document.querySelectorAll('table')].filter(visible).filter(t => {
  const headers = [...t.querySelectorAll('th')].map(h => h.innerText.trim().toLowerCase());
  return headers.some(h => /title|episode name/.test(h)) &&
    (headers.some(h => /tms|episode/.test(h)) || idPattern.test(t.innerText) ||
     [...t.querySelectorAll('a[href]')].some(a => idPattern.test(a.getAttribute('href'))));
});
if (tables.length !== 1) throw Error('Expected one visible episode table; found ' + tables.length);
const table = tables[0];
let scope = table.parentElement, markers = [];
while (scope && scope !== document.body) {
  markers = [...scope.querySelectorAll('.pagination-container .page-count,[class*="TablePagination-displayedRows"]')].filter(visible);
  if (!markers.length) markers = [...scope.querySelectorAll('p,span,div')].filter(el =>
    visible(el) && /^\d[\d,]*\s*[-–—]\s*\d[\d,]*\s+of\s+\d[\d,]*$/i.test(el.innerText.trim()));
  if (markers.length) break;
  scope = scope.parentElement;
}
if (markers.length !== 1) throw Error('Cannot identify the episode table pagination range');
const marker = markers[0];
let root = marker.closest('.pagination-container,[class*="TablePagination-root"],nav,[aria-label*="pagination" i]');
if (!root) {
  root = marker.parentElement;
  while (root && root !== scope && !root.querySelector('button,[role="button"]')) root = root.parentElement;
}
if (!root) throw Error('Cannot identify episode pagination controls');
const headers = [...table.querySelectorAll('thead th')].map(h => h.innerText.trim().toLowerCase());
let titleIndex = headers.findIndex(h => /episode title|episode name|^title$/.test(h));
if (titleIndex < 0) titleIndex = 1;
const numberIndex = headers.findIndex(h => h === 'episode number');
const partIndex = headers.findIndex(h => /^part/.test(h));
const episodes = [...table.querySelectorAll('tbody tr')].filter(visible).map(row => {
  const cells = [...row.querySelectorAll('td')];
  const hrefs = [...row.querySelectorAll('a[href]')].map(a => a.getAttribute('href')).join(' ');
  const match = hrefs.match(idPattern) || row.innerText.match(idPattern);
  return {title: cells[titleIndex]?.innerText.trim() || '', tms_id: match ? match[0].toUpperCase() : '',
          episode_number: cells[numberIndex]?.innerText.trim() || '', part: cells[partIndex]?.innerText.trim() || ''};
}).filter(ep => ep.title);
const buttons = [...root.querySelectorAll('button,[role="button"]')].filter(visible);
function direction(button) {
  const label = (button.getAttribute('aria-label') || button.getAttribute('title') || button.innerText).trim().toLowerCase();
  if (/first|last|help|feedback/.test(label)) return '';
  if (/next/.test(label) || ['›','>','⟩'].includes(label)) return 'next';
  if (/previous|prev/.test(label) || ['‹','<','⟨'].includes(label)) return 'previous';
  const svg = button.querySelector('svg');
  const icon = (svg?.getAttribute('data-icon') || svg?.getAttribute('data-testid') || '').toLowerCase();
  if (/last|first/.test(icon)) return '';
  if (/^angle-right$|keyboardarrowright|chevronright|navigatenext/.test(icon)) return 'next';
  if (/^angle-left$|keyboardarrowleft|chevronleft|navigatebefore/.test(icon)) return 'previous';
  return '';
}
function pick(dir) {
  const matches = buttons.filter(b => direction(b) === dir);
  if (matches.length > 1) throw Error('Ambiguous ' + dir + ' pagination controls');
  const button = matches[0];
  return button && !button.disabled && button.getAttribute('aria-disabled') !== 'true' &&
    !button.classList.contains('Mui-disabled') ? button : null;
}
return {marker: marker.innerText.trim(), marker_element: marker, episodes,
        previous: pick('previous'), next: pick('next')};
"""


def snapshot(driver):
    runtime.check()
    try:
        raw = driver.execute_script(SNAPSHOT_SCRIPT)
        return Snapshot(raw["marker"], tuple(Episode(**ep) for ep in raw["episodes"]),
                        raw["previous"], raw["next"], raw["marker_element"])
    except Exception as exc:
        raise SnapshotUnavailable(f"Cannot read episode table: {exc}") from exc


def ready(page):
    lo, hi, total = page.bounds
    return len(page.episodes) == (hi - lo + 1 if total else 0)


def wait_ready(read, timeout=15):
    end = time.monotonic() + timeout
    last_signature = None
    while time.monotonic() < end:
        runtime.check()
        try:
            page = read()
        except SnapshotUnavailable:
            last_signature = None
            time.sleep(0.25)
            continue
        key = (page.marker, page.signature)
        if ready(page) and key == last_signature:
            return page
        last_signature = key if ready(page) else None
        time.sleep(0.25)
    raise PaginationError("Episode rows did not finish loading. Scan is incomplete.")


def move(before, direction, read, click, timeout=15):
    control = before.next if direction == "next" else before.previous
    if control is None:
        raise PaginationError(f"No enabled {direction} control. Scan is incomplete.")
    runtime.check()
    click(control)  # Exactly one click: retrying can skip a late-loading page.
    end = time.monotonic() + timeout
    stable = None
    old_lo, old_hi, old_total = before.bounds
    while time.monotonic() < end:
        runtime.check()
        try:
            page = read()
        except SnapshotUnavailable:
            stable = None
            time.sleep(0.25)
            continue
        lo, hi, total = page.bounds
        if total != old_total:
            raise PaginationError("Episode count changed while paging. Please rerun.")
        advanced = lo == old_hi + 1 if direction == "next" else hi == old_lo - 1
        key = (page.marker, page.signature)
        if advanced and page.signature != before.signature and ready(page):
            if stable == key:
                return page
            stable = key
        else:
            stable = None
        time.sleep(0.25)
    raise PaginationError(f"{direction.title()} page did not load after one click. Scan is incomplete.")


def collect_pages(read, click, log=lambda text: None, timeout=15):
    page = wait_ready(read, timeout)
    visited = set()
    while page.bounds[0] > 1:
        if page.marker in visited:
            raise PaginationError("Pagination repeated while returning to page 1.")
        visited.add(page.marker)
        page = move(page, "previous", read, click, timeout)
    collected = []
    visited.clear()
    while True:
        runtime.check()
        if page.marker in visited:
            raise PaginationError("Pagination repeated a page. Scan is incomplete.")
        visited.add(page.marker)
        log(f"Reading episodes {page.marker}")
        collected.extend(page.episodes)
        _, hi, total = page.bounds
        if hi >= total:
            break
        page = move(page, "next", read, click, timeout)
    if len(collected) != page.bounds[2]:
        raise PaginationError("Scanned row count differs from the site's total.")
    return tuple(collected)


def match_episode(title, episodes, normalize, episode_number="", part=""):
    target = normalize(title)
    if not target:
        return "", "Episode title is empty"
    exact = [ep for ep in episodes if normalize(ep.title) == target]
    def number(value):
        m = re.search(r"\d+", str(value))
        return str(int(m.group())) if m else ""
    if episode_number:
        exact = [ep for ep in exact if number(ep.episode_number) == number(episode_number)]
    if part:
        exact = [ep for ep in exact if number(ep.part) == number(part)]
    ids = {ep.tms_id for ep in exact if ep.tms_id}
    if len(ids) == 1 and all(ep.tms_id for ep in exact):
        return ids.pop(), ""
    if exact:
        return "", "Exact title has multiple IDs or a missing ID; manual verification needed"
    ranked = sorted(episodes, key=lambda ep: SequenceMatcher(None, target, normalize(ep.title)).ratio(), reverse=True)
    if ranked and SequenceMatcher(None, target, normalize(ranked[0].title)).ratio() >= 0.65:
        best = ranked[0]
        return "", f"No exact title match. Review suggestion: {best.title} ({best.tms_id})"
    return "", "Episode not found after scanning all pages; manual verification needed"
