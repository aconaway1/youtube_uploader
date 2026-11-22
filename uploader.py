"""Upload the videos listed in a YAML file.

Every entry is validated before anything is uploaded. Each successful upload
is written to the upload log right away, so a rerun skips videos that already
went up instead of uploading them twice.

    python uploader.py                    # uses video_summary.yml
    python uploader.py --vids batch.yml
    python uploader.py --dry-run          # validate only, no sign-in
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
MIN_SECONDS_AT_UPLOAD = 5 * 60  # re-checked just before each upload
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

def build_job(vid, video_dir, min_lead=yt.DEFAULT_MIN_LEAD_MINUTES, finder=None):
    """Validate one YAML entry. Returns (job, problems).

    A bare filename is searched for in video_dir and all its subfolders.
    """
    finder = finder or yt.FileFinder(video_dir)
    problems = []
    if not isinstance(vid, dict):
        return None, ["entry is not a mapping"]

    filename = vid.get("filename")
    if not filename:
        return None, ["missing filename"]
    path, problem = finder.find(filename)
    if problem:
        problems.append(problem)

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

    # Publish-time problems are held back until after the duplicate check, so an
    # entry that's already on the channel is skipped instead of blocking the batch.
    too_soon = time_problem = None
    if vid.get("publishAt"):
        try:
            status["publishAt"] = yt.parse_publish_at(vid["publishAt"], min_lead)
        except yt.PublishTooSoon as e:
            status["publishAt"] = e.when
            too_soon = e.when  # main() offers to move it
        except ValueError as e:
            time_problem = str(e)
        if privacy != "private":
            # The API only schedules private videos; they flip public at publishAt.
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
        if os.path.splitext(str(vid["thumbnail"]))[1].lower() not in yt.THUMBNAIL_TYPES:
            problems.append(f"thumbnail must be .jpg or .png: {vid['thumbnail']}")
        else:
            # Look next to the video first, then anywhere under video_dir.
            thumbnail, problem = finder.find(vid["thumbnail"], prefer_dir=os.path.dirname(path),
                                             what="thumbnail")
            if problem:
                problems.append(problem)
            else:
                yt.check_thumbnail(thumbnail, problems)

    return {"path": path, "filename": str(filename), "body": body, "thumbnail": thumbnail,
            "too_soon": too_soon, "time_problem": time_problem, "may_move_schedule": False,
            "own_publish_at": bool(vid.get("publishAt")),
            "skip_schedule": bool(vid.get("skipSchedule")),
            "playlists": yt.as_name_list(vid.get("playlists")), "playlist_targets": []}, problems


# --------------------------------------------------------------------------
# Scheduling: one Short per open day
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Defaults for every video
# --------------------------------------------------------------------------

# Fields a `defaults:` block may set. filename, title, publishAt, recordingDate,
# thumbnail and skipSchedule are per-video only.
DEFAULTABLE = ("description", "keywords", "playlists", "categoryId", "privacyStatus", "language",
               "madeForKids")


def check_defaults(defaults):
    if defaults is None:
        return {}, []
    if not isinstance(defaults, dict):
        return {}, ["defaults: must be a block of fields, e.g.  defaults: {keywords: \"trains\"}"]
    unknown = sorted(set(defaults) - set(DEFAULTABLE))
    if unknown:
        return {}, [f"defaults: can't set {', '.join(unknown)} (only {', '.join(DEFAULTABLE)})"]
    return defaults, []


def apply_defaults(vid, defaults):
    """The video's own fields win, except keywords and playlists: the defaults come
    first and the video's own are added after them (duplicates dropped)."""
    if not defaults or not isinstance(vid, dict):
        return vid
    merged = {k: v for k, v in defaults.items() if k != "keywords"}
    merged.update(vid)
    if "keywords" in defaults:
        tags, seen = [], set()
        for t in yt.parse_tags(defaults["keywords"]) + yt.parse_tags(vid.get("keywords")):
            if t.casefold() not in seen:
                seen.add(t.casefold())
                tags.append(t)
        merged["keywords"] = tags
    if "playlists" in defaults:
        names, seen = [], set()
        for n in yt.as_name_list(defaults["playlists"]) + yt.as_name_list(vid.get("playlists")):
            if yt.normalize_title(n) not in seen:
                seen.add(yt.normalize_title(n))
                names.append(n)
        merged["playlists"] = names
    return merged


WEEKDAYS = {name: i for i, names in enumerate(
    [("monday", "mon"), ("tuesday", "tue", "tues"), ("wednesday", "wed"),
     ("thursday", "thu", "thur", "thurs"), ("friday", "fri"), ("saturday", "sat"),
     ("sunday", "sun")]) for name in names}


def parse_skip(value):
    """schedule.skip -> (set of dates, set of weekday numbers, problems).

    Accepts dates (2026-11-26) and weekday names (Sunday, sun), as a list or a
    single value."""
    items = value if isinstance(value, (list, tuple)) else [value]
    dates, weekdays, problems = set(), set(), []
    for item in items:
        if isinstance(item, datetime.datetime):
            dates.add(item.date())
        elif isinstance(item, datetime.date):
            dates.add(item)
        elif str(item).strip().lower() in WEEKDAYS:
            weekdays.add(WEEKDAYS[str(item).strip().lower()])
        else:
            try:
                dates.add(datetime.datetime.strptime(str(item).strip(), "%Y-%m-%d").date())
            except ValueError:
                problems.append(f"schedule: skip entries must be dates like 2026-11-26 or "
                                f"weekday names like Sunday (got {item!r})")
    return dates, weekdays, problems


def is_skipped(day, sched):
    return day in sched.get("skip_dates", ()) or day.weekday() in sched.get("skip_weekdays", ())


def parse_schedule(cfg, today=None):
    """The YAML `schedule:` block -> ({'time', 'start', 'skip_dates', 'skip_weekdays'}, problems)."""
    if cfg is None:
        return None, []
    if not isinstance(cfg, dict):
        return None, ["schedule: must look like  schedule: {time: \"13:00\", start: 2026-11-22}"]
    problems = []
    t = cfg.get("time")
    when = None
    if isinstance(t, int) and not isinstance(t, bool):
        # YAML reads an unquoted 13:00 as the number 780 (base 60).
        t = f"{t // 60}:{t % 60:02d}"
    try:
        when = datetime.datetime.strptime(str(t).strip(), "%H:%M").time()
    except ValueError:
        problems.append(f"schedule: time must look like \"13:00\" (got {cfg.get('time')!r})")
    today = today or datetime.date.today()
    start = cfg.get("start")
    if start is None:
        start = today + datetime.timedelta(days=1)
    elif not isinstance(start, datetime.date):
        try:
            start = datetime.datetime.strptime(str(start).strip(), "%Y-%m-%d").date()
        except ValueError:
            problems.append(f"schedule: start must look like 2026-11-22 (got {cfg.get('start')!r})")
    if isinstance(start, datetime.datetime):
        start = start.date()
    skip_dates, skip_weekdays, skip_problems = parse_skip(cfg.get("skip") or [])
    problems += skip_problems
    if len(skip_weekdays) == 7:
        problems.append("schedule: skip lists every day of the week, so nothing could be scheduled")
    unknown = set(cfg) - {"time", "start", "skip"}
    if unknown:
        problems.append(f"schedule: unknown setting(s) {', '.join(sorted(unknown))} "
                        "(only time, start and skip)")
    if problems:
        return None, problems
    return {"time": when, "start": start, "skip_dates": skip_dates,
            "skip_weekdays": skip_weekdays}, []


def assign_slots(jobs, sched, channel, min_lead, now=None):
    """Give each job without its own publishAt the next day with no Short scheduled.

    Days with a Short already scheduled or already published on the channel, a
    publishAt given in this batch, or listed in schedule.skip are passed over.
    Returns [(date, job or None, taken_title or None)] for the printout, from the
    start date through the last assigned day; (date, None, None) is a skip day.
    """
    now = now or datetime.datetime.now().astimezone()
    taken = {}
    for v in yt.scheduled_videos(channel, now):
        if v.get("isShort"):
            taken.setdefault(yt.local_date(v["publishAt"]), v["title"])
    # A day where a Short already went public is taken too (e.g. today).
    for v in yt.published_between(channel, sched["start"], datetime.date.max):
        if v.get("isShort"):
            taken.setdefault(yt.local_date(v["publishedAt"]), v["title"] + " (published)")
    for job in jobs:
        if job["own_publish_at"]:
            taken.setdefault(yt.local_date(job["body"]["status"]["publishAt"]),
                             job["body"]["snippet"]["title"])

    plan, day = [], sched["start"]
    earliest = now + datetime.timedelta(minutes=min_lead)
    for job in jobs:
        if job["own_publish_at"] or job["skip_schedule"]:
            continue
        while True:
            when = datetime.datetime.combine(day, sched["time"]).astimezone()
            if is_skipped(day, sched):
                if when >= earliest:
                    plan.append((day, None, None))
            elif day in taken:
                plan.append((day, None, taken[day]))
            elif when >= earliest:
                break
            day += datetime.timedelta(days=1)
        job["body"]["status"]["publishAt"] = when.isoformat()
        job["body"]["status"]["privacyStatus"] = "private"
        taken[day] = job["body"]["snippet"]["title"]
        plan.append((day, job, None))
        day += datetime.timedelta(days=1)
    return plan


def print_plan(plan, sched):
    n = sum(1 for _, job, _ in plan if job)
    t = datetime.datetime.combine(datetime.date.today(), sched["time"]).strftime("%I:%M %p").lstrip("0")
    print(f"\nScheduling {n} video{'s' if n != 1 else ''}, one per open day at {t}:")
    for day, job, taken_title in plan:
        label = day.strftime("%a %b %d").replace(" 0", " ")
        if job:
            print(f"  {label}  {job['filename']}: \"{job['body']['snippet']['title']}\"")
        elif taken_title is None:
            print(f"  {label}  (skipped: in schedule.skip)")
        else:
            print(f"  {label}  (taken: \"{taken_title}\")")
    print()


def upload(youtube, job):
    body = job["body"]
    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=MediaFileUpload(job["path"], chunksize=-1, resumable=True),
    )
    return yt.resumable_upload(request)


def add_playlists(youtube, filename, video_id, targets, entry, log, log_path):
    """Add one uploaded video to its playlists, recording each result in its log
    entry. Returns the number that failed."""
    failed = 0
    record = entry.setdefault("playlists", {})
    for t in targets:
        try:
            yt.add_to_playlist(youtube, t["id"], video_id)
            record[t["id"]] = {"title": t["title"], "status": "added"}
            print(f"  Added to playlist \"{t['title']}\"")
        except HttpError as e:
            record[t["id"]] = {"title": t["title"],
                               "status": f"failed: HTTP {e.resp.status} {yt.http_error_reason(e)}"}
            print(f"  {filename}: couldn't add to playlist \"{t['title']}\" "
                  f"({record[t['id']]['status'][8:]}); rerun to retry")
            failed += 1
        save_log(log_path, log)
    return failed


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
    parser.add_argument("--yes", action="store_true",
                        help="don't ask before uploading videos that will be public, unlisted "
                             "or scheduled (for unattended runs)")
    parser.add_argument("--fix-schedule", action="store_true",
                        help="move a publishAt that's too close to --min-lead minutes from now "
                             "without asking")
    parser.add_argument("--min-lead", type=int, default=yt.DEFAULT_MIN_LEAD_MINUTES,
                        metavar="MINUTES",
                        help="publishAt must be at least this far in the future (default %(default)s)")
    parser.add_argument("--thumbnail-wait", type=int, default=900, metavar="SECONDS",
                        help="how long to wait for processing before setting thumbnails "
                             "(default 900)")
    args = parser.parse_args(argv)

    data = yt.load_yaml(args.vids)
    base = os.path.dirname(os.path.abspath(args.vids))
    video_dir = os.path.expanduser(str(data.get("video_dir") or "."))
    if not os.path.isabs(video_dir):
        video_dir = os.path.join(base, video_dir)

    defaults, default_problems = check_defaults(data.get("defaults"))
    sched, sched_problems = parse_schedule(data.get("schedule"))
    sched_problems += default_problems
    unknown_top = sorted(set(data) - {"video_dir", "defaults", "schedule", "videos"})
    if unknown_top:
        sched_problems.append(f"unknown setting(s) at the top of {args.vids}: "
                              f"{', '.join(unknown_top)} (expected video_dir, defaults, "
                              "schedule, videos)")
    for p in sched_problems:
        print(f"ERROR {p}")
    if sched and args.no_channel_check:
        print("ERROR schedule: needs the channel check to find open days; "
              "drop --no-channel-check")
        sched_problems.append("needs channel check")

    finder = yt.FileFinder(video_dir)  # lists the folder tree once, only if a search is needed
    jobs, bad = [], len(sched_problems)
    for i, vid in enumerate(data["videos"], 1):
        job, problems = build_job(apply_defaults(vid, defaults), video_dir, args.min_lead, finder)
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
            channel = yt.channel_videos(youtube)
            index = yt.DuplicateIndex(channel)
        except HttpError as e:
            print(f"Couldn't read the channel's videos: HTTP {e.resp.status} "
                  f"{yt.http_error_reason(e)}. Nothing was uploaded.\n"
                  "Rerun, or use --no-channel-check to rely on the upload log only.")
            return 1
        print(f"  {index.count} videos on the channel.\n")

    log = load_log(args.log)
    todo, thumbs = [], []  # thumbs: [(job, video_id)] still needing a thumbnail
    pl_retry = []          # [(job, log entry)] uploaded earlier, playlists to check
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
            reasons += [r for r in index.matches(job["filename"], job["body"]["snippet"]["title"],
                                                 size=os.path.getsize(job["path"]))
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
        if prev and job["playlists"]:
            pl_retry.append((job, prev))

    # Now check publish times, only for videos that will actually be uploaded.
    late = 0
    for job in todo:
        if job["time_problem"]:
            print(f"ERROR {job['filename']}: {job['time_problem']}")
            late += 1
        elif job["too_soon"]:
            new = yt.offer_new_publish_time(job["filename"], job["too_soon"], args.min_lead,
                                            auto=args.fix_schedule)
            if new:
                job["body"]["status"]["publishAt"] = new
                job["may_move_schedule"] = True
            else:
                late += 1
    if late:
        print(f"\n{late} {'video needs' if late == 1 else 'videos need'} a new publish time. "
              "Nothing was uploaded.")
        return 2

    # Look up playlist names (only for videos that will upload, or retries).
    need = [j for j in todo if j["playlists"]] + [j for j, _ in pl_retry]
    if need:
        youtube = youtube or yt.get_youtube()
        try:
            resolver = yt.PlaylistResolver(yt.channel_playlists(youtube))
        except HttpError as e:
            print(f"Couldn't read the channel's playlists: HTTP {e.resp.status} "
                  f"{yt.http_error_reason(e)}. Nothing was uploaded.")
            return 1
        bad_pl = 0
        for job in need:
            job["playlist_targets"] = []
            for name in job["playlists"]:
                p, problem = resolver.resolve(name)
                if problem:
                    print(f"ERROR {job['filename']}: {problem}")
                    bad_pl += 1
                elif p["id"] not in {t["id"] for t in job["playlist_targets"]}:
                    job["playlist_targets"].append(p)
        if bad_pl:
            print(f"\n{bad_pl} playlist name{'s' if bad_pl != 1 else ''} not found. "
                  "Nothing was uploaded.")
            return 2
    retries = []
    for job, prev in pl_retry:
        added = {pid for pid, r in prev.get("playlists", {}).items() if r.get("status") == "added"}
        missing = [t for t in job["playlist_targets"] if t["id"] not in added]
        if missing:
            print(f"PLAYLISTS {job['filename']}: not yet in "
                  + ", ".join(f"\"{t['title']}\"" for t in missing) + "; adding it")
            retries.append((job, prev, missing))

    if sched:
        for job in todo:
            when = job["body"]["status"].get("publishAt")
            if job["own_publish_at"] and when and is_skipped(yt.local_date(when), sched):
                print(f"NOTE {job['filename']}: its own publishAt ({yt.format_when(when)}) is on a "
                      "day in schedule.skip; keeping it since you set it")
    if sched and todo:
        print_plan(assign_slots(todo, sched, channel, args.min_lead), sched)

    if args.dry_run:
        for job in todo:
            thumb = ", + thumbnail" if job["thumbnail"] else ""
            print(f"OK   {job['filename']}: \"{job['body']['snippet']['title']}\"\n"
                  f"     {yt.visibility(job['body']['status'])}{thumb}")
            if job["playlist_targets"]:
                print("     playlists: " + ", ".join(f"\"{t['title']}\"" for t in job["playlist_targets"]))
        print(f"\nDry run: {len(todo)} to upload, {dupes_skipped} duplicate(s) skipped, "
              f"{len(thumbs)} thumbnail retr{'y' if len(thumbs) == 1 else 'ies'}, "
              f"{len(retries)} playlist retr{'y' if len(retries) == 1 else 'ies'}, nothing sent.")
        return 0
    if not todo and not thumbs and not retries:
        print("\nNothing to upload.")
        return 0

    live = [j for j in todo if yt.goes_live(j["body"]["status"])]
    if live and not yt.confirm([f"{j['filename']}: \"{j['body']['snippet']['title']}\" -> "
                                f"{yt.visibility(j['body']['status'])}" for j in live],
                               assume_yes=args.yes):
        print("Stopped. Nothing was uploaded.")
        return 3

    youtube = youtube or yt.get_youtube()
    done, failed = [], []
    pl_failed = 0
    for n, job in enumerate(todo, 1):
        print(f"[{n}/{len(todo)}] {job['filename']}")
        publish_at = job["body"]["status"].get("publishAt")
        if publish_at and yt.seconds_until(publish_at) < MIN_SECONDS_AT_UPLOAD:
            # Earlier uploads took long enough that this schedule is now too close.
            if args.fix_schedule or job["may_move_schedule"]:
                job["body"]["status"]["publishAt"] = yt.earliest_publish_time(args.min_lead)
                print(f"  Schedule was {yt.format_when(publish_at)}; moved to "
                      f"{yt.format_when(job['body']['status']['publishAt'])}")
            else:
                print(f"  FAILED: scheduled for {yt.format_when(publish_at)}, too soon to upload "
                      "safely now. Pick a later publishAt, or use --fix-schedule.")
                failed.append(job)
                continue
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
        print(f"  Visibility: {yt.visibility(response.get('status') or job['body']['status'])}")
        if job["playlist_targets"]:
            pl_failed += add_playlists(youtube, job["filename"], video_id, job["playlist_targets"],
                                       log[log_key(job["path"])], log, args.log)
        if job["thumbnail"]:
            thumbs.append((job, video_id))

    for job, prev, missing in retries:
        print(f"{job['filename']}: adding to playlists")
        pl_failed += add_playlists(youtube, job["filename"], prev["video_id"], missing,
                                   prev, log, args.log)

    thumbs_not_set = apply_thumbnails(youtube, thumbs, log, args.log, args.thumbnail_wait)

    remaining = len(todo) - len(done) - len(failed)
    summary = (f"\n{len(done)} uploaded, {len(failed)} failed"
               + (f", {remaining} not attempted" if remaining else ""))
    if thumbs:
        summary += f"; {len(thumbs) - thumbs_not_set} of {len(thumbs)} thumbnails set"
    if pl_failed:
        summary += f"; {pl_failed} playlist add(s) failed (rerun to retry)"
    print(summary + ".")
    return 1 if failed or remaining or thumbs_not_set or pl_failed else 0


if __name__ == "__main__":
    sys.exit(main())
