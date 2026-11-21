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

    return {"path": path, "filename": str(filename), "body": body}, problems


def upload(youtube, job):
    body = job["body"]
    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=MediaFileUpload(job["path"], chunksize=-1, resumable=True),
    )
    return yt.resumable_upload(request)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None, youtube=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vids", default=VIDEOS_FILE, help="YAML file of videos to upload")
    parser.add_argument("--log", default=UPLOAD_LOG, help="upload log (JSON)")
    parser.add_argument("--dry-run", action="store_true", help="validate the file and stop")
    parser.add_argument("--force", action="store_true",
                        help="upload even if the upload log says a file already went up")
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
    if bad:
        print(f"\n{bad} {'entry needs' if bad == 1 else 'entries need'} fixing. Nothing was uploaded.")
        return 2

    log = load_log(args.log)
    todo = []
    for job in jobs:
        prev = log.get(log_key(job["path"]))
        if prev and not args.force:
            print(f"SKIP {job['filename']}: already uploaded as {prev['video_id']} "
                  f"on {prev['uploaded_at']} (use --force to upload again)")
        else:
            todo.append(job)

    if args.dry_run:
        for job in todo:
            s = job["body"]["status"]
            when = f", publishes {s['publishAt']}" if "publishAt" in s else ""
            print(f"OK   {job['filename']}: \"{job['body']['snippet']['title']}\" "
                  f"({s['privacyStatus']}{when})")
        print(f"\nDry run: {len(todo)} to upload, nothing sent.")
        return 0
    if not todo:
        print("Nothing to upload.")
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
        log[log_key(job["path"])] = {
            "video_id": video_id,
            "title": job["body"]["snippet"]["title"],
            "uploaded_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "privacy_status": response.get("status", {}).get("privacyStatus"),
            "size": os.path.getsize(job["path"]),
        }
        save_log(args.log, log)
        done.append(job)
        print(f"  Uploaded: https://youtu.be/{video_id}")

    remaining = len(todo) - len(done) - len(failed)
    print(f"\n{len(done)} uploaded, {len(failed)} failed"
          + (f", {remaining} not attempted" if remaining else "") + ".")
    return 1 if failed or remaining else 0


if __name__ == "__main__":
    sys.exit(main())
