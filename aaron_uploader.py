"""Upload the videos listed in a YAML file.

Every entry is validated before anything is uploaded. Each successful upload
is written to the upload log right away, so a rerun skips videos that already
went up instead of uploading them twice.

    python aaron_uploader.py                    # uses video_summary.yml
    python aaron_uploader.py --vids batch.yml
    python aaron_uploader.py --dry-run          # validate only, no sign-in
"""
import argparse
import datetime
import json
import os
import sys

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import yt_common as yt

VIDEOS_FILE = "video_summary.yml"
UPLOAD_LOG = os.path.join(yt.HERE, "upload_log.json")
DEFAULT_CATEGORY = "22"  # People & Blogs; 19 = Travel & Events, 2 = Autos & Vehicles


# --------------------------------------------------------------------------
# Upload log
# --------------------------------------------------------------------------

def load_log(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_log(path, log):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(log, f, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic, so a crash can't leave a half-written log


def log_key(path):
    return os.path.realpath(path)


# --------------------------------------------------------------------------
# Building the request
# --------------------------------------------------------------------------

def build_job(vid, video_dir):
    """Validate one YAML entry. Returns (job, problems)."""
    problems = []
    if not isinstance(vid, dict):
        return None, ["entry is not a mapping"]

    filename = vid.get("filename")
    if not filename:
        return None, ["missing filename"]
    path = os.path.join(video_dir, os.path.expanduser(str(filename)))
    if not os.path.isfile(path):
        problems.append(f"file not found: {path}")

    title = str(vid.get("title") or "").strip()
    if not title:
        problems.append("missing title")
    yt.check_text("title", title, problems, max_chars=yt.MAX_TITLE_CHARS)

    description = str(vid.get("description") or "").rstrip()
    yt.check_text("description", description, problems, max_bytes=yt.MAX_DESCRIPTION_BYTES)

    tags = yt.parse_tags(vid.get("keywords"))
    yt.check_tags(tags, problems)

    privacy = str(vid.get("privacyStatus", "private")).lower()
    if privacy not in yt.VALID_PRIVACY_STATUSES:
        problems.append(f"privacyStatus must be one of {', '.join(yt.VALID_PRIVACY_STATUSES)}")

    status = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": bool(vid.get("madeForKids", False)),
    }

    if vid.get("publishAt"):
        try:
            status["publishAt"] = yt.parse_publish_at(vid["publishAt"])
        except ValueError as e:
            problems.append(str(e))
        if privacy != "private":
            # The API only schedules private videos; they flip public at publishAt.
            print(f"  note: {filename}: publishAt set, uploading as private until then")
            status["privacyStatus"] = "private"

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": str(vid.get("categoryId", DEFAULT_CATEGORY)),
            "defaultLanguage": vid.get("language", "en"),
            "defaultAudioLanguage": vid.get("language", "en"),
        },
        "status": status,
    }

    if vid.get("recordingDate"):
        try:
            body["recordingDetails"] = {"recordingDate": yt.parse_recording_date(vid["recordingDate"])}
        except ValueError as e:
            problems.append(str(e))

    thumbnail = None
    if vid.get("thumbnail"):
        thumbnail = yt.resolve_path(vid["thumbnail"], video_dir)
        yt.check_thumbnail(thumbnail, problems)

    return {"path": path, "filename": str(filename), "body": body, "thumbnail": thumbnail}, problems


def upload(youtube, job):
    body = job["body"]
    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=MediaFileUpload(job["path"], chunksize=-1, resumable=True),
    )
    return yt.resumable_upload(request)


def apply_thumbnails(youtube, items, log, log_path, timeout):
    """items: [(job, video_id)]. Waits for processing, then sets each thumbnail.

    Records the outcome in the upload log as thumbnail_status. Returns the
    number that weren't set.
    """
    if not items:
        return 0
    print(f"\nThumbnails: {len(items)} to set once processing finishes "
          f"(waiting up to {timeout // 60} min)")
    try:
        statuses = yt.wait_for_processing(youtube, [vid for _, vid in items], timeout)
    except HttpError as e:
        statuses = {}
        print(f"  couldn't check processing status: HTTP {e.resp.status} {yt.http_error_reason(e)}")

    not_set = 0
    for job, video_id in items:
        state = statuses.get(video_id, "unknown")
        entry = log[log_key(job["path"])]
        entry["thumbnail"] = job["thumbnail"]
        if state != "succeeded":
            if state in ("failed", "terminated"):
                entry["thumbnail_status"] = f"failed: video processing {state}"
            else:
                entry["thumbnail_status"] = "pending: video still processing"
            print(f"  {job['filename']}: not set ({entry['thumbnail_status']}); rerun to retry")
            not_set += 1
        else:
            try:
                yt.set_thumbnail(youtube, video_id, job["thumbnail"])
                entry["thumbnail_status"] = "set"
                print(f"  {job['filename']}: thumbnail set")
            except HttpError as e:
                entry["thumbnail_status"] = "failed: " + yt.thumbnail_error_message(e)
                print(f"  {job['filename']}: {entry['thumbnail_status']}")
                not_set += 1
        save_log(log_path, log)
    return not_set


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None, youtube=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vids", default=VIDEOS_FILE, help="YAML file of videos to upload")
    parser.add_argument("--log", default=UPLOAD_LOG, help="upload log (JSON)")
    parser.add_argument("--dry-run", action="store_true", help="validate the file and stop")
    parser.add_argument("--allow-duplicate", action="append", default=[], metavar="FILENAME",
                        help="upload this file even though it looks like a duplicate "
                             "(repeat for more files)")
    parser.add_argument("--force", action="store_true",
                        help="upload every file even if it looks like a duplicate")
    parser.add_argument("--no-channel-check", action="store_true",
                        help="skip checking the channel for duplicates (the upload log is "
                             "still checked); lets --dry-run work without signing in")
    parser.add_argument("--thumbnail-wait", type=int, default=900, metavar="SECONDS",
                        help="how long to wait for processing before setting thumbnails "
                             "(default 900)")
    args = parser.parse_args(argv)

    data = yt.load_yaml(args.vids)
    base = os.path.dirname(os.path.abspath(args.vids))
    video_dir = os.path.expanduser(str(data.get("video_dir") or "."))
    if not os.path.isabs(video_dir):
        video_dir = os.path.join(base, video_dir)

    jobs, bad = [], 0
    for i, vid in enumerate(data["videos"], 1):
        job, problems = build_job(vid, video_dir)
        label = job["filename"] if job else f"entry {i}"
        for p in problems:
            print(f"ERROR {label}: {p}")
        if problems:
            bad += 1
        else:
            jobs.append(job)
    seen = {}
    for job in jobs:
        key = log_key(job["path"])
        if key in seen:
            print(f"ERROR {job['filename']}: listed twice in {args.vids}")
            bad += 1
        seen[key] = job
    allowed = {os.path.basename(a).casefold() for a in args.allow_duplicate}
    unknown = allowed - {os.path.basename(j["filename"]).casefold() for j in jobs}
    for name in sorted(unknown):
        print(f"ERROR --allow-duplicate {name}: not in {args.vids}")
        bad += 1
    if bad:
        print(f"\n{bad} {'entry needs' if bad == 1 else 'entries need'} fixing. Nothing was uploaded.")
        return 2

    index = None
    if not args.no_channel_check:
        youtube = youtube or yt.get_youtube()
        print("Checking the channel for duplicates...")
        try:
            index = yt.DuplicateIndex(yt.channel_videos(youtube))
        except HttpError as e:
            print(f"Couldn't read the channel's videos: HTTP {e.resp.status} "
                  f"{yt.http_error_reason(e)}. Nothing was uploaded.\n"
                  "Rerun, or use --no-channel-check to rely on the upload log only.")
            return 1
        print(f"  {index.count} videos on the channel.\n")

    log = load_log(args.log)
    todo, thumbs = [], []  # thumbs: [(job, video_id)] still needing a thumbnail
    dupes_skipped = 0
    for job in jobs:
        prev = log.get(log_key(job["path"]))
        reasons = []
        if prev and index is not None and prev["video_id"] not in index.ids:
            print(f"NOTE {job['filename']}: its earlier upload https://youtu.be/{prev['video_id']} "
                  "is no longer on the channel (deleted?), so it isn't treated as a duplicate")
            prev = None
        if prev:
            reasons.append(f"already uploaded by this tool as https://youtu.be/{prev['video_id']} "
                           f"on {prev['uploaded_at'][:10]}")
        if index:
            reasons += [r for r in index.matches(job["filename"], job["body"]["snippet"]["title"])
                        if not prev or prev["video_id"] not in r]

        if not reasons:
            todo.append(job)
            continue
        if args.force or os.path.basename(job["filename"]).casefold() in allowed:
            print(f"DUPLICATE {job['filename']}: uploading anyway as requested")
            for r in reasons:
                print(f"     {r}")
            todo.append(job)
            continue

        dupes_skipped += 1
        print(f"DUPLICATE {job['filename']}: skipped")
        for r in reasons:
            print(f"     {r}")
        print(f"     To upload it anyway: --allow-duplicate \"{job['filename']}\"")
        if prev and job["thumbnail"] and prev.get("thumbnail_status") != "set":
            print("     Its thumbnail isn't set yet; retrying that.")
            thumbs.append((job, prev["video_id"]))

    if args.dry_run:
        for job in todo:
            s = job["body"]["status"]
            when = f", publishes {s['publishAt']}" if "publishAt" in s else ""
            thumb = ", + thumbnail" if job["thumbnail"] else ""
            print(f"OK   {job['filename']}: \"{job['body']['snippet']['title']}\" "
                  f"({s['privacyStatus']}{when}{thumb})")
        print(f"\nDry run: {len(todo)} to upload, {dupes_skipped} duplicate(s) skipped, "
              f"{len(thumbs)} thumbnail retr{'y' if len(thumbs) == 1 else 'ies'}, nothing sent.")
        return 0
    if not todo and not thumbs:
        print("\nNothing to upload.")
        return 0

    youtube = youtube or yt.get_youtube()
    done, failed = [], []
    for n, job in enumerate(todo, 1):
        print(f"[{n}/{len(todo)}] {job['filename']}")
        try:
            response = upload(youtube, job)
        except HttpError as e:
            reason = yt.http_error_reason(e)
            print(f"  FAILED: HTTP {e.resp.status} {reason}")
            failed.append(job)
            if reason in yt.FATAL_REASONS:
                print("  Stopping: this error will affect every remaining upload.")
                break
            continue
        except (RuntimeError, OSError) as e:
            print(f"  FAILED: {e}")
            failed.append(job)
            continue

        video_id = response["id"]
        earlier = log.get(log_key(job["path"]))
        log[log_key(job["path"])] = {
            "video_id": video_id,
            "title": job["body"]["snippet"]["title"],
            "uploaded_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "privacy_status": response.get("status", {}).get("privacyStatus"),
            "size": os.path.getsize(job["path"]),
        }
        if earlier:  # an allowed duplicate: keep the record of the earlier upload(s)
            entry = log[log_key(job["path"])]
            entry["previous_uploads"] = earlier.pop("previous_uploads", []) + [earlier]
        save_log(args.log, log)
        done.append(job)
        print(f"  Uploaded: https://youtu.be/{video_id}")
        if job["thumbnail"]:
            thumbs.append((job, video_id))

    thumbs_not_set = apply_thumbnails(youtube, thumbs, log, args.log, args.thumbnail_wait)

    remaining = len(todo) - len(done) - len(failed)
    summary = (f"\n{len(done)} uploaded, {len(failed)} failed"
               + (f", {remaining} not attempted" if remaining else ""))
    if thumbs:
        summary += f"; {len(thumbs) - thumbs_not_set} of {len(thumbs)} thumbnails set"
    print(summary + ".")
    return 1 if failed or remaining or thumbs_not_set else 0


if __name__ == "__main__":
    sys.exit(main())
