"""Update the details of videos that are already on YouTube.

Reads each video's current details first and changes only the fields given
in the YAML, so anything you leave out (tags, language, translations, other
settings) stays as it is.

    python updater.py                     # uses draft_videos.yml
    python updater.py --vids edits.yml
    python updater.py --dry-run           # show changes, don't save them

Each video costs 51 quota units (1 to read it, 50 to update it), plus 50
for a thumbnail.
"""
import argparse
import copy
import os
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


def parse_changes(vid, base_dir=".", min_lead=yt.DEFAULT_MIN_LEAD_MINUTES):
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
    too_soon = None
    if vid.get("publishAt"):
        try:
            changes["status"]["publishAt"] = yt.parse_publish_at(vid["publishAt"], min_lead)
        except yt.PublishTooSoon as e:
            changes["status"]["publishAt"] = e.when
            too_soon = e.when  # main() offers to move it
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
    if too_soon:
        changes["too_soon"] = too_soon
    if vid.get("thumbnail"):
        # Not a videos.update part; main() sends it with thumbnails.set.
        changes["thumbnail"] = yt.resolve_path(vid["thumbnail"], base_dir)
        yt.check_thumbnail(changes["thumbnail"], problems)
    if not changes and not problems:
        problems.append("nothing to change (give title, description, keywords, publishAt, "
                        "thumbnail, ...)")
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
    parser.add_argument("--yes", action="store_true",
                        help="don't ask before making videos public/unlisted or scheduling them")
    parser.add_argument("--fix-schedule", action="store_true",
                        help="move a publishAt that's too close to --min-lead minutes from now "
                             "without asking")
    parser.add_argument("--min-lead", type=int, default=yt.DEFAULT_MIN_LEAD_MINUTES,
                        metavar="MINUTES",
                        help="publishAt must be at least this far in the future (default %(default)s)")
    args = parser.parse_args(argv)

    data = yt.load_yaml(args.vids)
    base_dir = os.path.dirname(os.path.abspath(args.vids))
    jobs, bad = [], 0
    for i, vid in enumerate(data["videos"], 1):
        video_id, changes, problems = parse_changes(vid, base_dir, args.min_lead)
        label = video_id or f"entry {i}"
        for p in problems:
            print(f"ERROR {label}: {p}")
        if problems:
            bad += 1
            continue
        if "too_soon" in changes:
            new = yt.offer_new_publish_time(label, changes.pop("too_soon"), args.min_lead,
                                            auto=args.fix_schedule)
            if not new:
                bad += 1
                continue
            changes["status"]["publishAt"] = new
        jobs.append((video_id, changes))
    if bad:
        print(f"\n{bad} {'entry needs' if bad == 1 else 'entries need'} fixing. Nothing was changed.")
        return 2

    youtube = youtube or yt.get_youtube()

    # Pass 1: read every video and work out the changes, without saving anything.
    plans, failed = [], 0
    for n, (video_id, changes) in enumerate(jobs, 1):
        print(f"[{n}/{len(jobs)}] {video_id}")
        changes = dict(changes)
        thumbnail = changes.pop("thumbnail", None)
        try:
            found = youtube.videos().list(part="snippet,status,recordingDetails,processingDetails",
                                          id=video_id).execute().get("items", [])
        except HttpError as e:
            reason = yt.http_error_reason(e)
            print(f"  FAILED: HTTP {e.resp.status} {reason}")
            failed += 1
            if reason in yt.FATAL_REASONS:
                print("  Stopping: this error will affect every remaining video.")
                break
            continue
        if not found:
            print("  FAILED: no video with that ID on this channel/account")
            failed += 1
            continue
        current = found[0]
        print(f"  \"{current.get('snippet', {}).get('title', '')}\"")
        body = parts = None
        if changes:
            body, parts = merge(current, changes)
            print("\n".join(describe(current, body)))
            if "status" in body:
                before = yt.visibility(current.get("status", {}))
                after = yt.visibility(body["status"])
                if before != after:
                    print(f"    visibility: {before} -> {after}")
        if thumbnail:
            print(f"    thumbnail -> {thumbnail}")
            processing = current.get("processingDetails", {}).get("processingStatus", "succeeded")
            if processing != "succeeded":
                print(f"  FAILED: video processing is '{processing}'; "
                      "thumbnails can only be set once it has succeeded")
                failed += 1
                continue
        plans.append({"id": video_id, "current": current, "body": body, "parts": parts,
                      "thumbnail": thumbnail})

    if args.dry_run:
        print(f"\nDry run: {len(jobs)} video(s) checked, nothing saved.")
        return 1 if failed else 0

    # Ask before anything becomes more visible.
    live = []
    for p in plans:
        if p["body"] and "status" in p["body"] and yt.goes_live(p["body"]["status"]):
            before = yt.visibility(p["current"].get("status", {}))
            after = yt.visibility(p["body"]["status"])
            if before != after:
                live.append(f"{p['id']} \"{p['body']['snippet']['title']}\": {after}")
    if live and not yt.confirm(live, assume_yes=args.yes):
        print("Stopped. Nothing was changed.")
        return 3

    # Pass 2: save.
    updated = 0
    for p in plans:
        try:
            if p["body"]:
                status = p["body"].get("status", {})
                if status.get("publishAt") and yt.seconds_until(status["publishAt"]) < 60:
                    print(f"{p['id']}: FAILED: scheduled time is now too close; pick a later one")
                    failed += 1
                    continue
                youtube.videos().update(part=",".join(p["parts"]), body=p["body"]).execute()
            if p["thumbnail"]:
                try:
                    yt.set_thumbnail(youtube, p["id"], p["thumbnail"])
                except HttpError as e:
                    print(f"{p['id']}: FAILED: thumbnail " + yt.thumbnail_error_message(e)
                          + (" (other changes were saved)" if p["body"] else ""))
                    failed += 1
                    continue
            updated += 1
            vis = yt.visibility(p["body"]["status"]) if p["body"] and "status" in p["body"] else None
            print(f"{p['id']}: updated" + (f" ({vis})" if vis else ""))
        except HttpError as e:
            reason = yt.http_error_reason(e)
            print(f"{p['id']}: FAILED: HTTP {e.resp.status} {reason}")
            failed += 1
            if reason in yt.FATAL_REASONS:
                print("Stopping: this error will affect every remaining video.")
                break

    print(f"\n{updated} updated, {failed} failed.")
    return 1 if failed or updated < len(jobs) else 0


if __name__ == "__main__":
    sys.exit(main())
