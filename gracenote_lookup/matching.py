"""Conservative ID validation and consistent keys for CSV resume."""
import re
import unicodedata


def normalize_text(value):
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def canonicalize_title_for_match(value):
    text = str(value or "").strip()
    match = re.fullmatch(r"(.*?),\s*(the|an|a)", text, re.I)
    if match:
        text = f"{match[2]} {match[1]}"
    return normalize_text(text)


def normalize_season_value(value):
    text = str(value or "").strip().lower()
    if "no season" in text or text in ("no", "none", "noseason"):
        return "0"
    match = re.search(r"\d+", text)
    return str(int(match[0])) if match else ""


def extract_tms_id(value, prefix="EP"):
    match = re.search(rf"\b{re.escape(prefix)}\d{{12}}\b", str(value or ""), re.I)
    return match[0].upper() if match else None


def valid_id(value, prefix):
    return bool(re.fullmatch(rf"{re.escape(prefix)}\d{{12}}", str(value or "").strip(), re.I))


def _value_is_one(value):
    return bool(re.fullmatch(r"0*1(?:\.0+)?", str(value or "").strip()))


def _value_means_done(value):
    return valid_id(value, "EP") or _value_is_one(value)


def handled(row):
    return _value_means_done(row.get("EpisodeTMSID")) or _value_is_one(row.get("SeriesTMSID"))


def episode_key(row):
    return (normalize_text(row["SeriesTitle"]), canonicalize_title_for_match(row["EpisodeTitle"]),
            normalize_season_value(row.get("Season")) or "1",
            normalize_season_value(row.get("EpisodeNumber")),
            normalize_season_value(row.get("Part")))


def group_key(row):
    # Identical titles can refer to different series. Preserve an explicit choice.
    supplied = row.get("SeriesTMSID", "").upper()
    return normalize_text(row["SeriesTitle"]), supplied if valid_id(supplied, "SH") else ""


def merge_existing(rows, previous):
    by_episode, by_series = {}, {}
    for old in previous:
        if handled(old):
            by_episode.setdefault(episode_key(old), []).append(old)
        if valid_id(old.get("SeriesTMSID"), "SH"):
            by_series.setdefault(normalize_text(old["SeriesTitle"]), set()).add(old["SeriesTMSID"].upper())
    for row in rows:
        if handled(row):
            continue
        candidates = by_episode.get(episode_key(row), [])
        if valid_id(row.get("SeriesTMSID"), "SH"):
            candidates = [old for old in candidates if old.get("SeriesTMSID", "").upper() == row["SeriesTMSID"].upper()]
        pairs = {(old.get("SeriesTMSID", "").upper(), old.get("EpisodeTMSID", "").upper()) for old in candidates}
        if len(pairs) == 1:
            series_id, episode_id = pairs.pop()
            if not row.get("SeriesTMSID"):
                row["SeriesTMSID"] = series_id
            row["EpisodeTMSID"] = episode_id
            row["Notes"] = candidates[0].get("Notes", "")
    return {title: next(iter(ids)) for title, ids in by_series.items() if len(ids) == 1}
