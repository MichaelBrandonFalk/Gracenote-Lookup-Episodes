"""Validate inputs and checkpoint output without truncating the previous save."""
import csv
import os
import tempfile
from pathlib import Path

ALIASES = {
    "SeriesTitle": ("SeriesTitle", "Series Title", "Series", "series_title", "Show"),
    "EpisodeTitle": ("EpisodeTitle", "Episode Title", "Title", "episode_title", "Episode"),
    "Season": ("Season", "season"),
    "SeriesTMSID": ("SeriesTMSID", "Series TMS ID", "Series_TMS_ID"),
    "EpisodeTMSID": ("EpisodeTMSID", "Episode TMS ID", "Episode_TMS_ID"),
    "EpisodeNumber": ("EpisodeNumber", "Episode Number", "episode_number"),
    "Part": ("Part", "Part #", "PartNumber"),
}


def read_csv(filename):
    with open(filename, encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        if not headers or any(not h.strip() for h in headers):
            raise ValueError("CSV needs a header row with named columns.")
        if len({h.strip() for h in headers}) != len(headers):
            raise ValueError("CSV contains duplicate column names.")
        reader.fieldnames = [h.strip() for h in headers]
        for required in ("SeriesTitle", "EpisodeTitle"):
            if not any(a in reader.fieldnames for a in ALIASES[required]):
                raise ValueError(f"CSV is missing {required} (or a supported alias).")
        rows = []
        for row in reader:
            if None in row:
                raise ValueError(f"CSV row {reader.line_num} has more values than headers.")
            row = {k: (v or "").strip() for k, v in row.items()}
            if not any(row.values()):
                continue
            for canonical, aliases in ALIASES.items():
                row[canonical] = next((row[a] for a in aliases if row.get(a)), "")
            rows.append(row)
    if not rows:
        raise ValueError("CSV contains no episode rows.")
    return rows


def write_csv(filename, rows, fieldnames):
    target = Path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                         dir=target.parent, delete=False) as stream:
            temp_name = stream.name
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
