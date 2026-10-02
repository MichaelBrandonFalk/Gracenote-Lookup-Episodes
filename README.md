# Gracenote Episode Lookup

A macOS desktop app for filling blank Gracenote movie, series and episode IDs in avails Excel workbooks, or adding series and episode IDs to a CSV. Choose your files in the app, sign in to Gracenote in Chrome, and resolve ambiguous programs with **Continue** or **Skip this program**.

- [Download the app](https://MichaelBrandonFalk.github.io/Gracenote-Lookup-Episodes/)
- [All versions and release notes](https://github.com/MichaelBrandonFalk/Gracenote-Lookup-Episodes/releases)

## Requirements

Apple Silicon Mac, macOS 13 or newer, Google Chrome, internet access, and a Gracenote View account. Python is bundled in the app. Selenium Manager downloads a compatible Chrome driver on first use.

The current app is ad-hoc signed, not Apple notarized. macOS may ask you to allow it in System Settings → Privacy & Security on first launch.

## Use the app

1. Download the ZIP, unzip it, and open **Gracenote Episode Lookup v2.0.1.app**.
2. Choose an avails `.xlsx` workbook or episode `.csv` and a different output location of the same file type.
3. Click **Start lookup** and sign in in Chrome. The app detects successful sign-in and clicks **Programs** automatically, even if Gracenote lands on Schedules.
4. When multiple programs share a title, select the correct movie or series in Chrome and continue, or skip it.
5. Review the results table and open the output. XLSX runs also create a separate review CSV. **Stop and save** retains completed results.
6. Click **Clear** to remove the selected files, results and activity and start over. If a lookup is running, it stops and saves before clearing. Saved files are retained.

The normal mode makes an automatic pass, then asks for series choices in a second pass. Manual mode asks for every series that has no supplied ID. **Search every season** scans all available seasons, including No Season when offered.

## Local sign-in settings

Open **Settings** to save your Gracenote username and password in this Mac’s Keychain, or remove saved credentials. They are not written to application files, logs, workbooks, source control or GitHub releases. **Clear** retains saved credentials.

During sign-in, **Fill saved sign-in** fills the verified Gracenote email/password form. You click **Sign in** in Chrome and complete any MFA. Copy buttons are also available in Settings. Corporate and Google sign-in remain manual. The app waits for the signed-in Programs navigation before continuing, and reports an invalidated Gracenote session rather than waiting indefinitely.

Movie searches leave program-type filters off so both Film and TV Movie results remain available. Result IDs and program badges are verified before any workbook cell is filled.

## Avails Excel workbooks

Select the original workbook directly; no CSV conversion or worksheet upload is needed. The app detects the field-name header row beneath metadata group headings and handles both Movie and TV worksheets.

| Worksheet | Blank field | ID written |
| --- | --- | --- |
| Movie | `RetailerID1` | Movie ID, `MV` followed by 12 digits |
| TV | `RetailerSeriesID` | Series ID, `SH` followed by 12 digits |
| TV | `RetailerEpisodeID1` | Episode ID, `EP` followed by 12 digits |

Every populated ID cell, including formulas, is preserved. Movie matching uses the title and release year when supplied. TV matching uses series title, season, episode title and episode number. Blank TV seasons trigger a scan of all available seasons. An existing series ID for the same title is reused only when it is unambiguous.

The app saves a new workbook by changing only originally blank target ID cells. All other worksheet XML, styles, formulas, hidden rows, validations, images and ZIP members are retained. Digitally signed workbooks are rejected because editing would invalidate their signatures. The original file is never overwritten.

The accompanying `OUTPUT_lookup_report.csv` records sheet name, physical row number, IDs, status and review notes. Skipped or uncertain matches remain blank in Excel; the CSV-only `1` skip marker is never inserted into an avails workbook. Resume copies valid IDs from an existing output only after checking that its other workbook content matches the original. The workbook remains local; individual program titles are searched on Gracenote.

## CSV columns

| Column | Purpose |
| --- | --- |
| `SeriesTitle` | Required series name. Aliases include `Series Title`, `Series`, and `Show`. |
| `EpisodeTitle` | Required episode name. Aliases include `Episode Title`, `Title`, and `Episode`. |
| `Season` | Defaults to 1 when blank. Supports `Season 01`, `S1`, and `No Season`. |
| `EpisodeNumber` | Optional number within the season; helps distinguish repeated titles. |
| `Part` | Optional part number, such as `1` or `1 of 2`. |
| `SeriesTMSID` | Optional known `SH` ID. A series ID alone does not skip an unfinished episode. |
| `EpisodeTMSID` | Existing `EP` IDs are preserved. `1` marks a row as intentionally skipped. |
| `Notes` | Match suggestions, missing IDs, skipped series, or scan failures. |

Other columns and input row order are preserved. The app reads UTF-8 CSVs with or without a BOM and writes output compatible with Excel. Use **Download CSV template** for a blank template.

## Matching, pagination, and resume

The app scans a season once per run and reuses its complete catalog for later episode rows. It returns to page 1 before collecting every page, clicks only the arrow associated with the episode table, and waits for both the page range and rows to refresh. A stalled or incomplete scan is recorded in Notes rather than reported as a completed search.

Only unique exact normalized title matches receive an ID automatically. Case, punctuation, accents, and trailing articles such as `Pilot, The` are normalized. Fuzzy matches become review suggestions. Repeated titles with different IDs require an episode number, part number, or manual verification. Title and number matches that remain ambiguous are left blank.

**Resume from existing output** preserves completed rows and explicit `1` markers. Input IDs take precedence. Conflicting previous IDs are not reused. Each run backs up an existing output before writing, and each checkpoint uses an atomic replacement. Series choices and catalogs are held in memory for the current run; login credentials are entered directly into Chrome.

## Development

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install '.[build]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m gracenote_lookup
.venv/bin/python scripts/build_app.py
```

The original launcher `gracenote_lookup_O.py` remains available for the command-line workflow. Historical script copies in the repository are retained as references.

## Releases

The version in `gracenote_lookup/__init__.py` controls the window title, package metadata, macOS bundle version, ZIP filename, release manifest, and GitHub tag. See [release process](docs/release_process.md). The download page reads published GitHub Releases, so new versions appear there automatically and older releases remain accessible.

## Validation

Regression tests cover delayed rows, missing controls, stalled pagination, more than 40 pages, duplicate titles, resume conflicts, CSV validation, workbook cell preservation, formulas, two-pass selection, and cancellation. The frozen app has an offscreen self-test for Qt startup, worker signals, CSV output and XLSX output.

On October 2, 2026, the site controls and snapshot extraction were checked in an authenticated Codex browser using County Rescue, Highway to Heaven and The Great Christmas Switch. Highway to Heaven Season 1 paged from 20 rows to the final 8 rows and back. A full lookup in the packaged app still requires a user login in its separate Chrome window.
