#!/usr/bin/env python3
"""
Publishes the Quran content from SQLite to a CDN-served JSON release.

Produces, inside the output folder:

    quran_config.json           Small. Exactly matches the app's QuranConfig.
                                The app reads this on startup.
    quran.json                  The full content, always the latest version.
                                Stable URL for anyone reusing the translation.
    releases/quran_v<N>.json    Byte-identical immutable copy of the above.
                                quran_json_link_url points here, so it can be
                                cached forever and can never change mid-download.

Git is content-addressed, so quran.json and the release copy share one blob.
The duplicate costs no extra repository space.

Uses only the Python standard library - nothing to install.

Exit codes:
  0  Success, a new release is ready to publish
  2  Success, but the content is unchanged (nothing to publish)
  1  Something went wrong (a plain-English reason is printed)
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows consoles default to a legacy codepage that cannot print Arabic or
# Yoruba diacritics. Force UTF-8 so output never crashes the script.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def find_root():
    """Locate the project folder by searching upward for settings.json,
    instead of assuming a fixed folder depth. This makes the script work
    whether it lives directly in the project folder or inside a scripts/
    subfolder."""
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "settings.json").exists():
            return candidate
    # Not found anywhere above - fall back to the folder the script is in,
    # so the "missing settings file" error below can show a sensible path.
    return here


ROOT = find_root()
SETTINGS_PATH = ROOT / "settings.json"
STATE_PATH = ROOT / ".publish_state.json"
LOG_DIR = ROOT / "logs"

CONFIG_FILE = "quran_config.json"
CONTENT_FILE = "quran.json"
RELEASES_DIR = "releases"

LOG_LINES = []


# ---------------------------------------------------------------- output ----

def say(message=""):
    print(message)
    LOG_LINES.append(message)


def fail(problem, fix=None):
    """Stop with an explanation a non-technical person can act on."""
    say()
    say("=" * 66)
    say("  STOPPED - nothing was published.")
    say("=" * 66)
    say()
    say(f"  Problem: {problem}")
    if fix:
        say()
        say(f"  How to fix: {fix}")
    say()
    write_log()
    sys.exit(1)


def write_log():
    try:
        LOG_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        (LOG_DIR / f"publish_{stamp}.log").write_text(
            "\n".join(LOG_LINES), encoding="utf-8"
        )
    except Exception:
        pass


# -------------------------------------------------------------- settings ----

def load_settings():
    if not SETTINGS_PATH.exists():
        fail(
            f"Cannot find settings.json.",
            f"It needs to be in the same folder as quran.db.\n"
            f"              Looked for it at: {SETTINGS_PATH}",
        )
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        fail(
            f"settings.json is not valid JSON (line {error.lineno}).",
            "A comma or quote was probably removed by accident. "
            "Ask your developer to check it.",
        )

    for key in ("database_file", "output_dir", "github_user",
                "github_repo", "github_branch", "table"):
        if not settings.get(key):
            fail(f'The setting "{key}" is empty in settings.json.',
                 "Ask your developer to complete the one-time setup.")

    if "YOUR-GITHUB-USERNAME" in settings["github_user"]:
        fail(
            "settings.json still contains the placeholder GitHub username.",
            'Replace "YOUR-GITHUB-USERNAME" with the real GitHub account name.',
        )

    validate_app_config(settings)
    return settings


def validate_app_config(settings):
    """These values are copied into the published config, so a mistake here
    reaches every user. Check them carefully."""
    app_config = settings.get("app_config")
    if not isinstance(app_config, dict):
        fail('settings.json is missing the "app_config" section.',
             "Ask your developer to restore it. See GUIDE.md.")

    audio_url = app_config.get("quran_audio_link_url")
    if not isinstance(audio_url, str) or not audio_url.startswith("http"):
        fail('"quran_audio_link_url" in settings.json is not a valid web address.',
             "It must start with https://")

    for platform in ("androidUpdateInfo", "iosUpdateInfo"):
        info = app_config.get(platform)
        if not isinstance(info, dict):
            fail(f'settings.json is missing "{platform}" inside app_config.',
                 "Ask your developer to restore it. See GUIDE.md.")

        if not isinstance(info.get("version"), int) or isinstance(info.get("version"), bool):
            fail(f'"{platform}.version" must be a whole number, with no quotes '
                 f'around it (found: {info.get("version")!r}).',
                 "Example:  \"version\": 4")

        if not isinstance(info.get("versionName"), str) or not info["versionName"].strip():
            fail(f'"{platform}.versionName" must be text in quotes '
                 f'(found: {info.get("versionName")!r}).',
                 'Example:  "versionName": "1.4.0"')

        if not isinstance(info.get("forceUpdate"), bool):
            fail(f'"{platform}.forceUpdate" must be true or false, with no '
                 f'quotes (found: {info.get("forceUpdate")!r}).',
                 'Use  "forceUpdate": false  - not "false"')

        update_info = info.get("updateInfo", None)
        if update_info is not None and not isinstance(update_info, str):
            fail(f'"{platform}.updateInfo" must be text in quotes, or null.',
                 'Example:  "updateInfo": "Adds bookmarks."')


# -------------------------------------------------------------- database ----

def open_database(settings):
    db_path = ROOT / settings["database_file"]
    if not db_path.exists():
        fail(
            f"Cannot find the database file: {db_path.name}",
            f'Put "{db_path.name}" in this folder:\n              {ROOT}',
        )

    if db_path.stat().st_size == 0:
        fail(f"The database file {db_path.name} is empty (0 bytes).",
             "It is probably corrupted. Restore it from a backup.")

    # A -wal or -journal file means DB Browser still has the database open,
    # and the admin may not have clicked "Write Changes" yet.
    leftovers = [db_path.with_name(db_path.name + suffix)
                 for suffix in ("-wal", "-journal")]
    if any(path.exists() and path.stat().st_size > 0 for path in leftovers):
        say()
        say("  NOTE: The database looks like it is still open in DB Browser.")
        say('        If you made edits, click "Write Changes" in DB Browser')
        say("        first, otherwise those edits will NOT be published.")
        say()

    try:
        # Read-only, so this script can never damage the admin's database.
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as error:
        fail(f"Could not open {db_path.name}: {error}",
             "Close DB Browser for SQLite and run this again.")

    return connection


def check_schema(connection, settings):
    table = settings["table"]
    tables = [row["name"] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")]

    if table not in tables:
        fail(
            f'The database has no table named "{table}".',
            f'Tables found in the file: {", ".join(tables) or "(none)"}\n'
            f'              Update "table" in settings.json to the correct name.',
        )

    present = {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    missing = [column for column in settings["columns"].values() if column not in present]
    if missing:
        fail(
            f'The table "{table}" is missing these columns: {", ".join(missing)}',
            "The database structure changed. Ask your developer to update settings.json.",
        )


def read_verses(connection, settings):
    columns = settings["columns"]
    keys = ("id", "surah_id", "verse_id", "verse_arabic",
            "verse_translation", "verse_footnote")
    select = ", ".join(f'"{columns[key]}" AS "{key}"' for key in keys)
    query = (f'SELECT {select} FROM "{settings["table"]}" '
             f'ORDER BY "{columns["surah_id"]}", "{columns["verse_id"]}"')
    try:
        return [dict(row) for row in connection.execute(query)]
    except sqlite3.DatabaseError as error:
        fail(f"Could not read the verses: {error}",
             "The database file may be corrupted. Restore it from a backup.")


# ------------------------------------------------------------ validation ----

def clean_text(value):
    """Normalise a text field. Returns None for genuinely empty values."""
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or None


def validate(rows, settings):
    """Catch content mistakes BEFORE anything reaches users."""
    if not rows:
        fail("The verses table is empty - there is nothing to publish.",
             "Check that you opened the right database file.")

    errors, warnings = [], []
    seen = {}
    max_surah = int(settings.get("expected_surah_count") or 114)

    for row in rows:
        surah_id, verse_id = row["surah_id"], row["verse_id"]

        if surah_id is None or verse_id is None:
            errors.append(f"A row is missing its surah number or verse number "
                          f"(row id {row['id']}).")
            continue
        try:
            surah_id, verse_id = int(surah_id), int(verse_id)
        except (TypeError, ValueError):
            errors.append(f"Surah/verse number is not a whole number (row id {row['id']}).")
            continue

        row["surah_id"], row["verse_id"] = surah_id, verse_id
        where = f"surah {surah_id}, verse {verse_id}"

        if not 1 <= surah_id <= max_surah:
            errors.append(f"Surah number {surah_id} is outside 1-{max_surah} "
                          f"(row id {row['id']}).")
        if verse_id < 1:
            errors.append(f"Verse number {verse_id} is not valid ({where}).")

        key = (surah_id, verse_id)
        if key in seen:
            errors.append(f"Duplicate verse: {where} appears twice "
                          f"(row ids {seen[key]} and {row['id']}).")
        else:
            seen[key] = row["id"]

        # Arabic and translation are required; the footnote is optional.
        if not clean_text(row["verse_arabic"]):
            errors.append(f"Missing Arabic text for {where}.")
        if not clean_text(row["verse_translation"]):
            errors.append(f"Missing Yoruba translation for {where}.")

    # Gaps in verse numbering are suspicious, but not always wrong.
    by_surah = {}
    for row in rows:
        if isinstance(row.get("surah_id"), int) and isinstance(row.get("verse_id"), int):
            by_surah.setdefault(row["surah_id"], []).append(row["verse_id"])
    for surah_id, verse_ids in sorted(by_surah.items()):
        gaps = sorted(set(range(1, max(verse_ids) + 1)) - set(verse_ids))
        if gaps:
            shown = ", ".join(str(gap) for gap in gaps[:8])
            more = f" (+{len(gaps) - 8} more)" if len(gaps) > 8 else ""
            warnings.append(f"Surah {surah_id}: verse numbers {shown}{more} are missing.")

    if warnings:
        say()
        say(f"  {len(warnings)} warning(s) - publishing will continue:")
        for warning in warnings[:12]:
            say(f"    - {warning}")
        if len(warnings) > 12:
            say(f"    - ...and {len(warnings) - 12} more (see the log file).")
            LOG_LINES.extend(f"    - {warning}" for warning in warnings[12:])

    if errors:
        say()
        say("=" * 66)
        say(f"  STOPPED - found {len(errors)} problem(s) in the database.")
        say("=" * 66)
        say()
        say("  Nothing was published. Fix these in DB Browser for SQLite,")
        say('  click "Write Changes", then run this again.')
        say()
        for error in errors[:25]:
            say(f"    - {error}")
        if len(errors) > 25:
            say(f"    - ...and {len(errors) - 25} more (see the log file).")
            LOG_LINES.extend(f"    - {error}" for error in errors[25:])
        say()
        write_log()
        sys.exit(1)

    return by_surah


# ---------------------------------------------------------------- export ----

def build_payload(rows):
    """Build the exact JSON shape the app and third parties expect."""
    ordered = sorted(rows, key=lambda item: (item["surah_id"], item["verse_id"]))
    return [
        {
            "id": row["id"],
            "surah_id": row["surah_id"],
            "verse_arabic": clean_text(row["verse_arabic"]),
            "verse_footnote": clean_text(row["verse_footnote"]),
            "verse_id": row["verse_id"],
            "verse_translation": clean_text(row["verse_translation"]),
        }
        for row in ordered
    ]


def serialise(payload):
    """Deterministic bytes, so identical content always hashes identically."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def read_published_config(settings):
    path = ROOT / settings["output_dir"] / CONFIG_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def export(settings, payload, previous):
    output_dir = ROOT / settings["output_dir"]
    releases_dir = output_dir / RELEASES_DIR
    releases_dir.mkdir(parents=True, exist_ok=True)

    data = serialise(payload)
    digest = hashlib.sha256(data).hexdigest()

    # Compare against the content that is actually published right now.
    current = output_dir / CONTENT_FILE
    content_changed = not (previous is not None and current.exists()
                           and current.read_bytes() == data)

    previous_version = int((previous or {}).get("surah_updated_version", 0))

    # surah_updated_version is what makes every app re-download the content,
    # so it moves ONLY when the verses themselves changed. Publishing new
    # store-update details must never cost users a download.
    version = previous_version + 1 if content_changed else previous_version

    release_name = f"quran_v{version}.json"

    base_url = (f'https://cdn.jsdelivr.net/gh/{settings["github_user"]}/'
                f'{settings["github_repo"]}@{settings["github_branch"]}/'
                f'{settings["output_dir"]}/')

    app_config = settings["app_config"]
    config = {
        "iosUpdateInfo": app_config["iosUpdateInfo"],
        "androidUpdateInfo": app_config["androidUpdateInfo"],
        "surah_updated_version": version,
        "quran_audio_link_url": app_config["quran_audio_link_url"],
        "quran_json_link_url": base_url + f"{RELEASES_DIR}/{release_name}",
    }
    if settings.get("include_integrity_fields"):
        config["quran_json_sha256"] = digest
        config["quran_json_bytes"] = len(data)

    config_changed = (previous is None or config != previous)

    if not content_changed and not config_changed:
        return None

    if content_changed:
        # Stable "always latest" file, for third-party reuse.
        current.write_bytes(data)
        # Immutable versioned copy - what the app downloads. Identical content
        # means Git stores one blob for both paths, so this duplicate is free.
        (releases_dir / release_name).write_bytes(data)

    (output_dir / CONFIG_FILE).write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "version": version,
        "previous_version": previous_version,
        "content_changed": content_changed,
        "bytes": len(data),
        "verse_count": len(payload),
        "surah_count": len({row["surah_id"] for row in payload}),
        "first_release": previous is None,
    }


# ------------------------------------------------------------- CDN purge ----

def save_state(settings, version):
    """Only the mutable files need a purge. Release files are immutable."""
    STATE_PATH.write_text(json.dumps({
        "version": version,
        "purge_paths": [CONFIG_FILE, CONTENT_FILE],
        "settings": {key: settings[key] for key in
                     ("github_user", "github_repo", "github_branch", "output_dir")},
    }, indent=2), encoding="utf-8")


def purge_cdn():
    if not STATE_PATH.exists():
        say("  Nothing to refresh.")
        return

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    settings = state["settings"]
    paths = state.get("purge_paths", [])

    say(f"  Refreshing the download server for {len(paths)} file(s)...")
    failures = 0
    for relative in paths:
        url = (f'https://purge.jsdelivr.net/gh/{settings["github_user"]}/'
               f'{settings["github_repo"]}@{settings["github_branch"]}/'
               f'{settings["output_dir"]}/{relative}')
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    response.read()
                break
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
                if attempt == 2:
                    failures += 1
                else:
                    time.sleep(2 * (attempt + 1))

    if failures:
        say()
        say(f"  NOTE: {failures} file(s) could not be refreshed right now.")
        say("        Your update is already published and safe. Users will")
        say("        receive it within about 12 hours even without this step.")
    else:
        say("  Done - users will see the update immediately.")

    STATE_PATH.unlink(missing_ok=True)


# ------------------------------------------------------------------ main ----

def human(num_bytes):
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / 1024 / 1024:.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def main():
    parser = argparse.ArgumentParser(description="Publish the Quran content.")
    parser.add_argument("--purge", action="store_true",
                        help="Refresh the CDN cache after a successful push.")
    parser.add_argument("--check", action="store_true",
                        help="Validate everything without writing any files.")
    arguments = parser.parse_args()

    if arguments.purge:
        purge_cdn()
        return 0

    settings = load_settings()

    say("  Reading the database...")
    connection = open_database(settings)
    check_schema(connection, settings)
    rows = read_verses(connection, settings)
    connection.close()
    say(f"  Found {len(rows)} verses.")

    say("  Checking the content for mistakes...")
    by_surah = validate(rows, settings)
    expected = int(settings.get("expected_surah_count") or 114)
    if len(by_surah) < expected:
        say(f"  NOTE: {len(by_surah)} of {expected} surahs are in the database.")

    if arguments.check:
        say()
        say("  Everything looks good. (Check mode - nothing was written.)")
        write_log()
        return 0

    say("  Preparing the release...")
    payload = build_payload(rows)
    previous = read_published_config(settings)
    result = export(settings, payload, previous)

    if result is None:
        say()
        say("=" * 66)
        say("  Nothing has changed since the last update.")
        say("=" * 66)
        say()
        say("  The content is identical to what users already have, so there")
        say("  is nothing to publish. This is not an error.")
        say()
        say("  If you expected changes, open DB Browser for SQLite and make")
        say('  sure you clicked "Write Changes" after editing.')
        say()
        write_log()
        return 2

    save_state(settings, result["version"])

    say()
    say("  " + "-" * 62)
    if result["first_release"]:
        say(f"  Version {result['version']} is ready (first release).")
    elif result["content_changed"]:
        say(f"  Version {result['version']} is ready "
            f"(replacing version {result['previous_version']}).")
    else:
        say("  App settings updated. The Quran content did not change.")
    say("  " + "-" * 62)
    if result["content_changed"]:
        say(f"    Content        : {result['verse_count']} verses "
            f"across {result['surah_count']} surahs")
        say(f"    File size      : {human(result['bytes'])}")
    else:
        say(f"    Content version stays at {result['version']}, so users will")
        say("    not re-download the Quran text.")
    say()
    write_log()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        say("  Cancelled. Nothing was published.")
        write_log()
        sys.exit(1)
    except Exception as error:  # last resort, so the admin never sees a traceback
        fail(f"An unexpected error occurred: {error}",
             "Send the newest file in the 'logs' folder to your developer.")
