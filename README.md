# Aaron YouTube Uploader

[![Tests](https://github.com/aconaway1/youtube_uploader/actions/workflows/tests.yml/badge.svg)](https://github.com/aconaway1/youtube_uploader/actions/workflows/tests.yml)

I post railfan Shorts to [Ron Con Rails](https://ronconrails.com) most days, and uploading them one
at a time by hand got old. These scripts upload a batch of videos from a YAML file, spread them out
one a day, and keep me from posting the same clip twice. It started from Google's upload sample,
but there's not much of that left.

# Summary

Three scripts that share one sign-in, plus one that never touches YouTube:

- `uploader.py` uploads the videos listed in a YAML file (filename, title, description, tags,
  thumbnail, schedule, etc.), skips anything already on the channel, and can schedule a batch to
  go public one per day.
- `updater.py` changes the details of videos that are already on YouTube, such as a title,
  description, tags, thumbnail, or the publish date and time.
- `upcoming.py` shows what's scheduled to go public, day by day, and which days are open.
- `mark_published.py` (macOS) tags uploaded files in Finder. It only reads the upload log.

# Setup

You need an OAuth 2.0 client, which is much easier than you think it is. The complicated
stuff is handled by the scripts.

## Create a Project and Enable the APIs

This is just establishing a container for your client.

1. Go to [this link](https://console.cloud.google.com/apis/api/youtube.googleapis.com),
log in, and agree to the ToS.
1. Create a new project, which is a logical container for your application. The link to do that
should be right on the page you land on.
1. Make sure `Enabled APIs & services` is the focus on the left.
1. Click `ENABLE APIS AND SERVICES` near the top.
1. You wind up at the API Library page. Search for "youtube" and select `YouTube Data API v3`.
1. Click `ENABLE`. Some spinny stuff happens, then it's enabled.

## Set up the Consent Screen

This is that screen that says "This flashlight app wants access to your contacts".

1. Go to `Google Auth Platform` (formerly `OAuth consent screen`). Choose `External`.
1. Give your app a name and pick your email address for the support and developer contacts.
1. Under `Data Access`, add the scopes `.../auth/youtube.upload` and `.../auth/youtube.force-ssl`.
1. Under `Audience`, add your YouTube account's Google login as a test user.

**Heads up:** while the app's publishing status is `Testing`, Google expires the saved sign-in after
7 days. The scripts notice and open the browser for you to sign in again. Switching the status to
`In production` stops that; you'll click through an "unverified app" warning once.

## Set Up an OAuth Client

This is the creds your client uses to auth to YouTube.

1. Click `Credentials` (or `Clients`) on the left.
1. `+ CREATE CREDENTIALS` → `OAuth client ID`. Application type is `Desktop app`.
1. Give it a useful name and click `CREATE`.
1. Download the JSON and save it in this repo as `client_secrets.json`.

## Install

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The first run opens a browser window to sign in. If your channel is a brand channel, pick the
channel (not your personal account) on the account chooser. The sign-in is saved in `token.json`
and shared by both scripts. Delete `token.json` to switch accounts or channels.

If you used an older version, you can delete the old `*-oauth2.json` token files.

# The Uploader

Copy `examples/video_summary.example.yml` to `video_summary.yml` and edit it.

```yaml
video_dir: "~/Library/CloudStorage/Dropbox/Video/Finals"   # optional; default is the YAML file's folder
videos:
  - filename: "my clip.mov"    # required; found in video_dir or any folder under it
    title: "My title"          # required, 100 characters max
    description: |             # use | (not >) so your line breaks are kept
      First line shows in search.

      #hashtag #another
    keywords: "tag one, tag two"     # optional, 500 characters total
    categoryId: "19"                 # optional, default "22"
    privacyStatus: "private"         # public | private | unlisted, default private
    publishAt: "2026-11-22 13:00"    # optional, your local time; keeps it private until then
    recordingDate: 2026-05-20        # optional
    madeForKids: false               # optional, default false
    thumbnail: "my clip thumb.jpg"   # optional .jpg/.png; next to the video, or under video_dir
```

## Defaults for every video

Put shared fields in a `defaults:` block and each video only needs `filename` and `title`:

```yaml
defaults:
  keywords: "trains, railfan, railroad, Galesburg"
  categoryId: "19"
  description: |
    Railfanning around Galesburg, Illinois.

    #railfan #trains
videos:
  - filename: "A6 Departs 20260520.mov"
    title: "BNSF Helps the California Zephyr Toward Chicago 20260520"
    keywords: "BNSF, Amtrak, California Zephyr"   # added to the default tags
  - filename: "BNSF 674 yard job 20260520.mov"
    title: "BNSF 674 Sansa Fe Pulling a Yard Job 20260520"
    description: |                                # replaces the default description
      Ex-Santa Fe warbonnet BNSF 674 working the yard.
```

A video's own value replaces the default, except `keywords` and `playlists`: the video's own are
added after the defaults (repeats dropped). The combined tags must fit YouTube's 500-character limit.
`defaults:` can set `description`, `keywords`, `playlists`, `categoryId`, `privacyStatus`,
`language` and `madeForKids`; the rest are per-video. A misspelled top-level setting (like `default:`) is an error.

Then:

```
python uploader.py --dry-run           # check the file and the channel; nothing uploaded
python uploader.py                     # upload video_summary.yml
python uploader.py --vids other.yml    # upload a different file
```

**Finding files in dated folders.** Give just the file name and the uploader finds it in `video_dir`
or any folder under it (e.g. `Finals/2026/2026-05-20/`). Hidden folders like `.dropbox.cache` are
skipped. If two folders have a file with the same name, it stops and lists them; put the folder in
`filename` for that entry (`2026/2026-05-20/clip.mov`). Thumbnails are looked for next to their video
first. Listing folders doesn't download Dropbox online-only files; only the file being uploaded is
downloaded, which makes that upload slower.

What it does:

- **Checks every entry before uploading anything.** A missing file, a missing title, `<` or `>` in a
  title or description, too many tag characters, or a bad privacy setting stops the run with a list
  of what to fix.
- **Remembers what it uploaded.** Each successful upload is written to `upload_log.json` right away,
  with the video ID. Running the same YAML again skips those files, so a run that fails partway
  can just be rerun.
- **Checks the channel for duplicates.** Before uploading, it reads your channel's video list and
  skips any file whose original file name or title (ignoring case and spacing) matches a video
  already there, including ones uploaded by hand in Studio. Skipped files are listed with a link to
  the matching video; the rest of the batch still uploads.
  - `--allow-duplicate "file.mov"` uploads that file anyway (repeat for more files).
  - `--force` uploads every file regardless.
  - `--no-channel-check` skips the channel check (the upload log is still checked); handy for a
    `--dry-run` without signing in.
  - The check costs about 1 quota unit per 25 videos on the channel.
- **Keeps going when one video fails**, and reports the totals at the end. Errors that will hit every
  remaining video (out of quota, upload limit) stop the run.
- **Sets thumbnails after processing.** YouTube only accepts a thumbnail once the video has finished
  processing, so the uploader uploads everything first, then waits (up to 15 minutes; change it
  with `--thumbnail-wait SECONDS`) and sets each thumbnail. If one isn't set in time, or fails, the
  upload still counts; the log records why, and the next run retries just the thumbnail.
  Custom thumbnails need a [verified channel](https://www.youtube.com/verify).

The Shorts "Related video" link isn't available in the YouTube API, so set that in Studio.

## Going public: safeguards

Google says videos uploaded through an API project that hasn't passed the
[YouTube API audit](https://support.google.com/youtube/contact/yt_api_form) are locked to private.
**Don't rely on that.** In testing, this project's uploads went public without an audit. So:

- **Plain-language visibility.** The dry run, the confirmation prompt and the upload summary say
  `private`, `PUBLIC immediately`, `UNLISTED (anyone with the link)` or
  `private, goes PUBLIC Sun Nov 22, 12:00 PM CST (in 1 day 3 hr)`.
- **You're asked first.** Before uploading anything public, unlisted or scheduled, the uploader lists
  those videos and asks `Continue? [y/N]`. All-private batches don't ask. When it isn't running in
  a terminal (launchd, cron, a pipe) it can't ask, so it stops unless you pass `--yes`.
- **A minimum lead time for `publishAt`.** A schedule less than 15 minutes away is rejected, so a
  video can't go live before it has processed and you've looked at it. Change it with
  `--min-lead MINUTES`. In a terminal you're offered `Move it to <15 minutes from now>? [y/N]`;
  `--fix-schedule` moves it without asking. Times already in the past are always an error (likely
  a wrong date). Each scheduled video is checked again just before it uploads; if a long batch has
  left less than 5 minutes it's moved (when you allowed moving) or skipped.

The draft updater works the same way: it reads every video and shows all changes first, asks once if
any change makes a video more visible or schedules it (`--yes` to skip), then saves. Retitling a
public video or making a video private doesn't ask.

## Playlists

```yaml
defaults:
  playlists: ["Railfan Shorts"]
videos:
  - filename: "A6 Departs 20260520.mov"
    title: "..."
    playlists:
      - "BNSF"
      - "Galesburg, Illinois"
```

Use playlist names as they appear on your channel (any capitalization) or playlist IDs. Before
anything uploads, the names are looked up; one that isn't on the channel (or matches two playlists)
stops the run, so a typo can't create a stray playlist. Each video is added right after it uploads,
and each result is recorded in `upload_log.json`. A failed add is retried on the next run without
re-uploading, and a video is never added to the same playlist twice. Adding a playlist to an entry
you've already uploaded adds the video on the next run. Each add costs 50 quota units.

**Commas:** in the bracket style, `[Galesburg, Illinois]` is two playlists. Quote names with commas
(`["Galesburg, Illinois"]`) or use one name per line as above.

## Scheduling a week of Shorts

Add a `schedule:` block to the YAML and leave `publishAt` off the videos:

```yaml
video_dir: "~/Library/CloudStorage/Dropbox/Video/Finals"
schedule:
  time: "13:00"          # local time each one goes public
  start: 2026-11-22      # optional; defaults to tomorrow
videos:
  - filename: "A6 Departs 20260520.mov"
    title: "..."
  - filename: "BNSF 674 yard job 20260520.mov"
    title: "..."
```

Each video gets the next **open day**: a day with no Short already scheduled on the channel,
including ones you scheduled in Studio. Long-form videos don't take up a day. The uploader prints
the plan before anything happens:

```
Scheduling 2 videos, one per open day at 1:00 PM:
  Sun Nov 22  A6 Departs 20260520.mov: "BNSF Helps the California Zephyr ..."
  Mon Nov 23  (taken: "MARTA CQ400 New Railcar at Five Points 20260601")
  Tue Nov 24  BNSF 674 yard job 20260520.mov: "BNSF 674 Sansa Fe Pulling a Yard Job ..."
```

**Days off.** List dates or weekday names under `skip:` and nothing is scheduled on them:

```yaml
schedule:
  time: "07:15"
  skip:
    - 2026-11-26     # Thanksgiving
    - 2026-12-25
    - Sunday         # every Sunday
```

They show as `(skipped: in schedule.skip)` in the plan, and `upcoming.py` marks them "skipped"
(it reads `skip:` from `video_summary.yml`, or the file given with `--vids`).

A video with its own `publishAt` keeps it (and claims that day), even on a skip day, with a note;
`skipSchedule: true` leaves a video out of the schedule. Days are assigned in YAML order. Run `--dry-run` first to see the plan.
To move something later, change its `publishAt` with the updater.

## What's scheduled: upcoming.py

```
python upcoming.py              # next 14 days
python upcoming.py --days 30
```

```
Scheduled to go public, Sat Nov 21 through Fri Dec 4:
  Sat Nov 21  open
  Sun Nov 22   1:00 PM  Short  "BNSF Helps the California Zephyr ..."  https://youtu.be/...
  Mon Nov 23   1:00 PM  Short  "MARTA CQ400 New Railcar at Five Points 20260601"  https://youtu.be/...
  Tue Nov 24  open
  ...
2 Shorts scheduled, 12 open days.
```

# The Updater

This script updates the details of videos that are already uploaded. I've been uploading videos via
the YouTube page or app and using it to set the title, description, tags, and, most importantly to
me, the publish date and time.

Copy `examples/draft_videos.example.yml` to `draft_videos.yml` and edit it. Only `url` is required;
**anything you leave out stays as it is on YouTube.**

```yaml
videos:
  - url: "https://youtube.com/shorts/VIDEO_ID"   # any YouTube link or the bare video ID
    title: "New title"
    description: |
      New description.
    keywords: "tag one, tag two"
    categoryId: "19"
    publishAt: "2026-11-25 13:00"   # your local time; video must not be published yet
    privacyStatus: "unlisted"       # don't combine with publishAt
    recordingDate: 2026-05-20
    thumbnail: "thumbs/new.jpg"     # .jpg/.png, relative to this YAML file
    playlists: ["BNSF", "Galesburg"]  # adds it to these playlists
```

```
python updater.py --dry-run    # shows old -> new for each change, saves nothing
python updater.py
```

`playlists:` adds the video to each playlist it isn't already in (it checks first, since YouTube
would otherwise let it appear twice). It never removes a video from a playlist. Playlist names are
checked before anything changes, as in the uploader. Handy for sorting older Shorts:

```yaml
videos:
  - url: https://youtube.com/shorts/AAAA
    playlists: ["MARTA"]
  - url: https://youtube.com/shorts/BBBB
    playlists: ["Amtrak", "Galesburg, Illinois"]
```

It reads each video's current details first and only changes the fields you gave, so tags,
language, translations and other settings aren't wiped. It only touches the privacy setting when you
give `privacyStatus` or `publishAt`, so retitling a public video leaves it public.

# Tagging Uploaded Files in Finder

`mark_published.py` is a separate, macOS-only script. It never talks to YouTube. It reads
`upload_log.json` and, for each file the uploader successfully uploaded, adds the green
**Published** Finder tag and removes **Approved for Publishing**. Files that are already tagged are
skipped, so running it repeatedly is harmless.

To use different tag names or a different color, copy `examples/settings.example.yml` to
`settings.yml` (git-ignored) and edit it:

```yaml
finder_tags:
  published: "Published"                 # added to each uploaded file
  published_color: green                 # none, gray, green, purple, blue, yellow, red, orange
  approved: "Approved for Publishing"    # removed from each uploaded file...
  remove_approved: true                  # ...unless this is false
```

Anything you leave out keeps the default above. A bad setting stops the run before any file is
touched. If you rename your approved tag, update your Finder saved search too.

```
python3 mark_published.py --dry-run        # show what would change
python3 mark_published.py                  # tag
python3 mark_published.py --keep-approved  # add Published, leave Approved alone
```

Run it right after an upload (`python uploader.py; python3 mark_published.py`), or have
launchd run it every 6 hours and at login:

```
./launchd/install_mark_published.sh             # install or update
INTERVAL_HOURS=3 ./launchd/install_mark_published.sh
./launchd/install_mark_published.sh --remove    # uninstall
launchctl kickstart gui/$(id -u)/com.ronconrails.mark-published   # run now
tail ~/Library/Logs/mark_published.log
```

It only knows about videos the uploader logged. Videos uploaded by hand in Studio aren't tagged.
If your videos live in Desktop, Documents, Downloads, iCloud Drive, or on an external drive, macOS
may block the background job until you give Python access in System Settings → Privacy & Security.
If a file is moved or renamed after uploading, the script reports it as MISSING.

# Quotas

You get 10,000 units a day for most calls, and uploads have their own limit of 100 a day.

| Call | Cost |
|---|---|
| Upload a video (`videos.insert`) | 1 unit, max 100 uploads a day |
| Read a video's details (`videos.list`) | 1 unit |
| Update a video (`videos.update`) | 50 units |
| Set a thumbnail (`thumbnails.set`) | 50 units |
| Add to a playlist (`playlistItems.insert`) | 50 units |
| Look up your playlists (`playlists.list`) | 1 unit per 50 playlists |
| Check if a video is in a playlist (`playlistItems.list`) | 1 unit |

So the uploader costs 1 unit per video, plus about 1 unit per 25 channel videos for the duplicate
check, plus about 51 for a thumbnail (a few 1-unit checks while
it waits for processing, then the thumbnail). The updater costs 51 units per video, plus 50 for a
thumbnail. See
[this page](https://developers.google.com/youtube/v3/determine_quota_cost) for the full list.

# Tests

```
pip install -r requirements-dev.txt
python -m pytest tests
```

The tests use a fake YouTube service, so they need no network or sign-in. GitHub Actions runs them
on every push (Ubuntu with Python 3.10, 3.12 and 3.13, plus macOS, which also checks real Finder
tags); see `.github/workflows/tests.yml`.

# License

MIT. See [LICENSE](LICENSE).
