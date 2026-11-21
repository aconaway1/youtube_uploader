# Changelog

## Unreleased

### Fixed
- An entry that's already on the channel no longer blocks the batch because its old
  `publishAt` has passed. Publish times are now checked only for videos that will actually be
  uploaded, after the duplicate check, so you can leave uploaded entries in the YAML.

## v0.3 (2026-11-21)

Mostly a rewrite. Script names changed: `aaron_uploader.py` is now `uploader.py` and
`draft_updater.py` is now `updater.py`. Your YAML files keep their names.

### New
- **Scheduling.** A `schedule:` block (`time`, optional `start`) gives each video the next day with
  no Short already scheduled on the channel. Long-form videos don't take a day. `skipSchedule: true`
  leaves a video out.
- **`upcoming.py`** shows what's scheduled to go public, day by day, with open days marked.
- **Duplicate check against the channel.** Before uploading, files whose original file name or
  title matches a video already on the channel are skipped with a link to the match.
  `--allow-duplicate FILE` (repeatable) or `--force` uploads anyway; `--no-channel-check` skips it.
- **Upload log** (`upload_log.json`). Each upload is recorded as soon as it finishes, so a failed
  batch can be rerun without re-uploading. A logged video that was deleted from YouTube no longer
  counts as a duplicate.
- **Thumbnails** (`thumbnail:` in both YAML files). The uploader sets them once processing finishes
  and retries unset ones on the next run.
- **Go-live safeguards** in both scripts: plain-language visibility ("PUBLIC immediately",
  "goes PUBLIC Sun Nov 22, 1:00 PM CST (in 1 day 3 hr)"); a y/N prompt before anything public,
  unlisted or scheduled (`--yes` for unattended runs); `publishAt` must be at least 15 minutes away
  (`--min-lead`), with an offer to move it (`--fix-schedule` to move without asking).
- **Dated folders.** A bare `filename` or `thumbnail` is found in `video_dir` or any folder under it.
- **`mark_published.py`** (macOS) tags uploaded files Published in Finder and removes Approved for
  Publishing, from the upload log only. `launchd/install_mark_published.sh` runs it every 6 hours.
- `--dry-run` in both scripts; `publishAt`, `recordingDate`, `madeForKids`, `language` in the uploader.
- Tests (`tests/`, against a fake YouTube service) and complete example YAML files in `examples/`.

### Fixed
- `--vids` was ignored by both scripts.
- The uploader sent `madeForKids: True` (a read-only field). It now sends
  `selfDeclaredMadeForKids: false`.
- One failed video aborted the whole batch, with no record of what had already uploaded.
- A missing `filename` crashed instead of being reported; entries are now all validated before
  anything uploads.
- The updater wiped fields it wasn't given (tags, language, translations, status settings) and
  forced videos private. It now reads each video first and changes only what's in the YAML.
- Share links with `?si=` (and shorts/watch/live URLs) now work in the updater.
- The retry loop kept sleeping after a recovered error.
- Tags are trimmed and length-checked; descriptions keep their line breaks with `|`.

### Changed
- Sign-in uses `google-auth` instead of the long-deprecated `oauth2client`, with one shared
  `token.json`. An expired Testing-mode sign-in reopens the browser instead of failing.
- Removed the unrelated `apiclient` package and unused `pycurl` / `http-client` from requirements.
- Privacy defaults to private.
- Removed `upload_video.py` (the original Google sample). Test videos and personal YAML files are
  no longer in the repo, and all `.json` files are git-ignored.
- MIT license.

## v0.2 (2024-10-07)
- Added `draft_updater.py` for updating details of videos already on YouTube.
- Updated `README.md`.

## v0.1 (2024-10-03)
- First version.
