# Changelog

## 2.0.0 — 2026-10-02

- Add a desktop UI with XLSX/CSV pickers, progress, results, review filtering, and Continue/Skip buttons.
- Fill blank Movie RetailerID1 and TV RetailerSeriesID/RetailerEpisodeID1 fields in avails workbooks.
- Preserve existing workbook values and native formatting by patching only target cells; add a separate review CSV.
- Match movies by title and year, reuse unambiguous existing series IDs, and verify workbook content before resuming.
- Preserve the two-pass workflow, manual mode, and search across available seasons.
- Replace guessed pagination clicks with table-scoped arrow identification and verified row transitions.
- Scan and cache complete season catalogs, returning to page 1 before collection.
- Flag fuzzy and duplicate title matches for review; support episode and part numbers.
- Validate CSV headers and preserve BOMs, extra columns, input order, and supplied IDs.
- Save checkpoints atomically, back up previous output, and preserve progress on cancellation.
- Separate found IDs, skipped rows, and rows needing review in the summary.
- Add versioned macOS app bundles, release ZIPs, checksums, manifests, and GitHub release automation.
- Add a public download page that lists releases and their original versioned assets.

## Earlier scripts

The original terminal scripts were not distributed as numbered GitHub Releases. They remain available in Git history and the historical script copies.
