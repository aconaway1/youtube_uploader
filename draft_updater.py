"""Update the details of videos that are already on YouTube.

Reads each video's current details first and changes only the fields given
in the YAML, so anything you leave out (tags, language, translations, other
settings) stays as it is.

    python draft_updater.py                     # uses draft_videos.yml
    python draft_updater.py --vids edits.yml
    python draft_updater.py --dry-run           # show changes, don't save them

Each video costs 51 quota units (1 to read it, 50 to update it).
"""
import argparse
import copy
import sys

from googleapiclient.errors import HttpError

import yt_common as yt

DRAFT_VIDEOS_FILE = "draft_videos.yml"

# Fields videos.update accepts. Everything else in a videos.list response is
# read-only and gets dropped before sending.
WRITABLE_SNIPPET = ("title", "description", "tags", "categoryId",
                    "defaultLanguage", "defaultAudioLanguage")
WRITABLE_STATUS = ("privacyStatus", "embeddable", "license", "publicStatsViewable",
                   "publishAt", "selfDeclaredMadeForKids", "containsSyntheticMedia")


def parse_changes(vid):
    """Validate one YAML entry. Returns (video_id, changes, problems)."""
    problems = []
    if not isinstance(vid, dict):
        return None, None, ["entry is not a mapping"]
    if not vid.get("url"):
        return None, None, ["missing url"]
    try:
        video_id = yt.parse_video_id(vid["url"])
    except ValueError as e:
        return None, None, [str(e)]

    changes = {"snippet": {}, "status": {}, "recordingDetails": {}}
    if "title" in vid:
        title = str(vid["title"] or "").strip()
        if not title:
            problems.append("title can't be empty")
        yt.check_text("title", title, problems, max_chars=yt.MAX_TITLE_CHARS)
        changes["snippet"]["title"] = title
    if "description" in vid:
        description = str(vid["description"] or "").rstrip()
        yt.check_text("description", description, problems, max_bytes=yt.MAX_DESCRIPTION_BYTES)
        changes["snippet"]["description"] = description
    if "keywords" in vid:
        tags = yt.parse_tags(vid["keywords"])
        yt.check_tags(tags, problems)
        changes["snippet"]["tags"] = tags
    if "categoryId" in vid:
        changes["snippet"]["categoryId"] = str(vid["categoryId"])
    if "language" in vid:
        changes["snippet"]["defaultLanguage"] = vid["language"]
        changes["snippet"]["defaultAudioLanguage"] = vid["language"]
    if "privacyStatus" in vid:
        privacy = str(vid["privacyStatus"]).lower()
        if privacy not in yt.VALID_PRIVACY_STATUSES:
            problems.append(f"privacyStatus must be one of {', '.join(yt.VALID_PRIVACY_STATUSES)}")
        changes["status"]["privacyStatus"] = privacy
    if vid.get("publishAt"):
        try:
            changes["status"]["publishAt"] = yt.parse_publish_at(vid["publishAt"])
        except ValueError as e:
            problems.append(str(e))
        if changes["status"].get("privacyStatus", "private") != "private":
            problems.append("publishAt only works with privacyStatus private "
                            "(leave privacyStatus out to schedule)")
    if "madeForKids" in vid:
        changes["status"]["selfDeclaredMadeForKids"] = bool(vid["madeForKids"])
    if vid.get("recordingDate"):
        try:
            changes["recordingDetails"]["recordingDate"] = yt.parse_recording_date(vid["recordingDate"])
        except ValueError as e:
            problems.append(str(e))

    changes = {part: fields for part, fields in changes.items() if fields}
    if not changes and not problems:
        problems.append("nothing to change (give title, description, keywords, publishAt, ...)")
    return video_id, changes, problems


def merge(current, changes):
    """Current video resource + changes -> (update body, parts)."""
    body = {"id": current["id"]}

    snippet = {k: copy.deepcopy(v) for k, v in current.get("snippet", {}).items()
               if k in WRITABLE_SNIPPET}
    snippet.update(changes.get("snippet", {}))
    body["snippet"] = snippet

    parts = ["snippet"]
    if "status" in changes:
        status = {k: v for k, v in current.get("status", {}).items() if k in WRITABLE_STATUS}
        status.update(changes["status"])
        if "publishAt" in changes["status"]:
            status["privacyStatus"] = "private"  # scheduling requires private
        elif status.get("privacyStatus") != "private":
            status.pop("publishAt", None)  # a schedule only applies to private videos
        body["status"] = status
        parts.append("status")
    if "recordingDetails" in changes:
        rec = dict(current.get("recordingDetails", {}))
        rec.update(changes["recordingDetails"])
        body["recordingDetails"] = rec
        parts.append("recordingDetails")
    return body, parts


def describe(current, body):
    lines = []
    for part in ("snippet", "status", "recordingDetails"):
        for k, new in body.get(part, {}).items():
            old = current.get(part, {}).get(k)
            if old != new:
                short = lambda v: (repr(v)[:70] + "...") if len(repr(v)) > 73 else repr(v)
                lines.append(f"    {part}.{k}: {short(old)} -> {short(new)}")
    return lines or ["    (no changes)"]


def main(argv=None, youtube=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vids", default=DRAFT_VIDEOS_FILE, help="YAML file of video changes")
    parser.add_argument("--dry-run", action="store_true",
                        help="read the videos and show what would change, without saving")
    args = parser.parse_args(argv)

    data = yt.load_yaml(args.vids)
    jobs, bad = [], 0
    for i, vid in enumerate(data["videos"], 1):
        video_id, changes, problems = parse_changes(vid)
        label = video_id or f"entry {i}"
        for p in problems:
            print(f"ERROR {label}: {p}")
        if problems:
            bad += 1
        else:
            jobs.append((video_id, changes))
    if bad:
        print(f"\n{bad} entr{'y' if bad == 1 else 'ies'} need fixing. Nothing was changed.")
        return 2

    youtube = youtube or yt.get_youtube()
    updated, failed = 0, 0
    for n, (video_id, changes) in enumerate(jobs, 1):
        print(f"[{n}/{len(jobs)}] {video_id}")
        try:
            found = youtube.videos().list(part="snippet,status,recordingDetails",
                                          id=video_id).execute().get("items", [])
            if not found:
                print("  FAILED: no video with that ID on this channel/account")
                failed += 1
                continue
            current = found[0]
            body, parts = merge(current, changes)
            print("\n".join(describe(current, body)))
            if args.dry_run:
                continue
            youtube.videos().update(part=",".join(parts), body=body).execute()
            updated += 1
            print("  Updated.")
        except HttpError as e:
            reason = yt.http_error_reason(e)
            print(f"  FAILED: HTTP {e.resp.status} {reason}")
            failed += 1
            if reason in yt.FATAL_REASONS:
                print("  Stopping: this error will affect every remaining video.")
                break

    if args.dry_run:
        print(f"\nDry run: {len(jobs)} video(s) checked, nothing saved.")
        return 1 if failed else 0
    print(f"\n{updated} updated, {failed} failed.")
    return 1 if failed or updated < len(jobs) else 0


if __name__ == "__main__":
    sys.exit(main())
