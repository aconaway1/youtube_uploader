#!/usr/bin/env python3
"""Tag uploaded video files in Finder.

Reads upload_log.json (written by uploader.py) and, for every file the
log says was uploaded, adds the "Published" tag and removes the "Approved for
Publishing" tag. Files that are already tagged are left alone, so it's safe
to run as often as you like (e.g. from launchd every few hours).

Tag names, the Published color, and whether to remove the approved tag come
from settings.yml next to this script (see examples/settings.example.yml).
Without that file, the defaults below are used.

This script never talks to YouTube. It only reads the local upload log and
changes Finder tags on your own files. macOS only.

    python3 mark_published.py              # tag everything in the log
    python3 mark_published.py --dry-run    # show what would change
    python3 mark_published.py --keep-approved
"""
import argparse
import json
import os
import plistlib
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_LOG = os.path.join(HERE, "upload_log.json")
SETTINGS_FILE = os.path.join(HERE, "settings.yml")

# Used for anything settings.yml doesn't set.
DEFAULT_TAGS = {
    "published": "Published",
    "published_color": "green",
    "approved": "Approved for Publishing",
    "remove_approved": True,
}

TAG_ATTR = "com.apple.metadata:_kMDItemUserTags"
XATTR = "/usr/bin/xattr"
# Finder's label numbers.
COLORS = {"none": 0, "gray": 1, "green": 2, "purple": 3, "blue": 4,
          "yellow": 5, "red": 6, "orange": 7}


# ---- Finder tags ------------------------------------------------------------
# Tags live in an extended attribute holding a binary plist: a list of
# strings, each "Name" or "Name\n<color number>".

def decode_tags(raw):
    if not raw:
        return []
    return list(plistlib.loads(raw))


def encode_tags(tags):
    return plistlib.dumps(list(tags), fmt=plistlib.FMT_BINARY)


def tag_name(entry):
    return entry.split("\n", 1)[0]


def read_tags(path):
    """Tag entries on a file, e.g. ['Approved for Publishing\\n5']."""
    r = subprocess.run([XATTR, "-px", TAG_ATTR, path], capture_output=True, text=True)
    if r.returncode != 0:
        if "No such xattr" in r.stderr:
            return []
        raise OSError(r.stderr.strip() or f"xattr failed on {path}")
    return decode_tags(bytes.fromhex("".join(r.stdout.split())))


def write_tags(path, tags):
    subprocess.run([XATTR, "-wx", TAG_ATTR, encode_tags(tags).hex(), path],
                   check=True, capture_output=True, text=True)


def retag(tags, add, add_color, remove):
    """New tag list: add `add` (with color) unless present, drop `remove`."""
    names = [tag_name(t) for t in tags]
    out = [t for t in tags if remove is None or tag_name(t) != remove]
    if add not in names:
        out.append(f"{add}\n{COLORS[add_color]}" if COLORS[add_color] else add)
    return out


# ---- settings ---------------------------------------------------------------

def load_settings(path):
    """finder_tags from settings.yml merged over DEFAULT_TAGS. Returns (tags, problems)."""
    tags = dict(DEFAULT_TAGS)
    if not os.path.exists(path):
        return tags, []
    try:
        import yaml  # PyYAML, installed with requirements.txt
    except ImportError:
        return tags, [f"{path} exists but PyYAML isn't installed; run this with the repo's "
                      ".venv python or `pip install PyYAML`"]
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return tags, [f"{path}: expected settings like  finder_tags: {{published: Published}}"]
    section = data.get("finder_tags") or {}
    if not isinstance(section, dict):
        return tags, [f"{path}: finder_tags must be a block of settings"]
    problems = []
    unknown = sorted(set(section) - set(DEFAULT_TAGS))
    if unknown:
        problems.append(f"{path}: unknown finder_tags setting(s) {', '.join(unknown)} "
                        f"(expected {', '.join(DEFAULT_TAGS)})")
    for key in ("published", "approved"):
        if key in section:
            if not isinstance(section[key], str) or not section[key].strip() or "\n" in section[key]:
                problems.append(f"{path}: finder_tags.{key} must be a tag name")
            else:
                tags[key] = section[key].strip()
    if "published_color" in section:
        color = str(section["published_color"]).strip().lower()
        if color not in COLORS:
            problems.append(f"{path}: finder_tags.published_color must be one of {', '.join(COLORS)}")
        else:
            tags["published_color"] = color
    if "remove_approved" in section:
        if not isinstance(section["remove_approved"], bool):
            problems.append(f"{path}: finder_tags.remove_approved must be true or false")
        else:
            tags["remove_approved"] = section["remove_approved"]
    if tags["published"] == tags["approved"]:
        problems.append(f"{path}: published and approved tags can't be the same")
    return tags, problems


# ---- main -------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", default=UPLOAD_LOG, help="upload log from uploader.py")
    parser.add_argument("--settings", default=SETTINGS_FILE,
                        help="settings file with tag names (default: settings.yml here)")
    parser.add_argument("--dry-run", action="store_true", help="show changes without making them")
    parser.add_argument("--keep-approved", action="store_true",
                        help="don't remove the approved tag, whatever settings.yml says")
    parser.add_argument("--quiet", action="store_true", help="only print changes and problems")
    args = parser.parse_args(argv)

    if sys.platform != "darwin" and not os.environ.get("MARK_PUBLISHED_ANY_OS"):
        print("Finder tags only exist on macOS.")
        return 2
    settings, problems = load_settings(args.settings)
    for p in problems:
        print(f"ERROR {p}")
    if problems:
        return 2
    if not os.path.exists(args.log):
        if not args.quiet:
            print(f"No upload log at {args.log}; nothing to tag.")
        return 0
    with open(args.log) as f:
        log = json.load(f)

    remove = None if args.keep_approved or not settings["remove_approved"] else settings["approved"]
    changed = already = missing = errors = 0
    for path, entry in sorted(log.items()):
        if not entry.get("video_id"):
            continue
        name = os.path.basename(path)
        if not os.path.exists(path):
            missing += 1
            if not args.quiet:
                print(f"MISSING {name}: not at {path} (moved or renamed?)")
            continue
        try:
            tags = read_tags(path)
            new = retag(tags, settings["published"], settings["published_color"], remove)
            if new == tags:
                already += 1
                continue
            if not args.dry_run:
                write_tags(path, new)
            changed += 1
            print(f"{'WOULD TAG' if args.dry_run else 'TAGGED'} {name}: "
                  f"{[tag_name(t) for t in tags]} -> {[tag_name(t) for t in new]}")
        except (OSError, subprocess.CalledProcessError, plistlib.InvalidFileException) as e:
            errors += 1
            print(f"ERROR {name}: {e}")

    if not args.quiet or changed or errors:
        print(f"{changed} {'to tag' if args.dry_run else 'tagged'}, {already} already tagged, "
              f"{missing} missing, {errors} errors.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
