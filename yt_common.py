"""Shared helpers for the uploader and the video updater.

Auth, the YouTube service object, resumable uploads with retry, and the
parsing/validation helpers both scripts need.
"""
import datetime
import os
import random
import sys
import time
from urllib.parse import parse_qs, urlparse

import httplib2
import yaml
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRETS_FILE = os.path.join(HERE, "client_secrets.json")
TOKEN_FILE = os.path.join(HERE, "token.json")

# One token for both scripts. upload covers videos.insert; force-ssl covers
# reading and editing existing videos (videos.list / videos.update).
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

VALID_PRIVACY_STATUSES = ("public", "private", "unlisted")
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500

MAX_RETRIES = 10
RETRIABLE_STATUS_CODES = (500, 502, 503, 504)
RETRIABLE_EXCEPTIONS = (httplib2.HttpLib2Error, OSError, ConnectionError)

MISSING_CLIENT_SECRETS_MESSAGE = f"""
Missing OAuth client file: {CLIENT_SECRETS_FILE}

Download the Desktop OAuth client JSON from Google Cloud Console
(APIs & Services -> Credentials) and save it at that path.
"""


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def get_credentials():
    """Load the cached token, refresh it, or run the browser sign-in."""
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise SystemExit(MISSING_CLIENT_SECRETS_MESSAGE)

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE)
        # A token minted with fewer scopes (e.g. upload-only) can't do
        # everything we need, so treat it as missing.
        if not set(SCOPES).issubset(set(creds.scopes or [])):
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # While the OAuth consent screen is in "Testing", Google expires
            # refresh tokens after 7 days. Fall through to a fresh sign-in.
            print("Saved sign-in has expired; opening the browser to sign in again.")
            creds = None

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return creds


def get_youtube():
    return build("youtube", "v3", credentials=get_credentials(), cache_discovery=False)


# --------------------------------------------------------------------------
# Uploading
# --------------------------------------------------------------------------

def resumable_upload(request, sleep=time.sleep):
    """Drive a resumable videos.insert request to completion.

    Retries transient errors with exponential backoff. Returns the API
    response. Raises HttpError for non-retriable API errors and
    RuntimeError if retries run out or the response is unexpected.
    """
    response = None
    retry = 0
    while response is None:
        error = None
        try:
            status, response = request.next_chunk()
            if status is not None:
                print(f"  {int(status.progress() * 100)}% uploaded")
        except HttpError as e:
            if e.resp.status in RETRIABLE_STATUS_CODES:
                error = f"retriable HTTP error {e.resp.status}"
            else:
                raise
        except RETRIABLE_EXCEPTIONS as e:
            error = f"retriable error: {e}"

        if error is not None:
            retry += 1
            if retry > MAX_RETRIES:
                raise RuntimeError(f"Giving up after {MAX_RETRIES} retries ({error})")
            delay = random.random() * (2 ** retry)
            print(f"  {error}; retrying in {delay:.1f}s")
            sleep(delay)

    if "id" not in response:
        raise RuntimeError(f"Upload finished with an unexpected response: {response}")
    return response


def http_error_reason(e):
    """The API's reason code for an HttpError, e.g. 'quotaExceeded'."""
    try:
        return e.error_details[0].get("reason", "")
    except (AttributeError, IndexError, TypeError, KeyError):
        return ""


# Errors that will fail every following request too, so a batch should stop.
FATAL_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded",
                 "rateLimitExceeded", "forbidden", "youtubeSignupRequired"}


# --------------------------------------------------------------------------
# Parsing and validation
# --------------------------------------------------------------------------

def load_yaml(path):
    if not os.path.exists(path):
        raise SystemExit(f"File not found: {path}")
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict) or not data.get("videos"):
        raise SystemExit(f"{path} does not contain a 'videos' list.")
    return data


def parse_tags(value):
    """'a, b,c' or ['a', 'b'] -> ['a', 'b', 'c'] with whitespace and blanks removed."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = [str(v) for v in value]
    return [p.strip() for p in parts if p and p.strip()]


def tags_length(tags):
    """Length as YouTube counts it: tags with spaces are quoted, joined by commas."""
    total = sum(len(t) + (2 if " " in t else 0) for t in tags)
    return total + max(len(tags) - 1, 0)


def parse_video_id(url_or_id):
    """Video ID from youtu.be, watch?v=, /shorts/, /live/ links, or a bare ID."""
    s = str(url_or_id).strip()
    if "/" not in s and "?" not in s:
        return s
    u = urlparse(s if "://" in s else "https://" + s)
    if u.hostname and u.hostname.endswith("youtu.be"):
        return u.path.strip("/").split("/")[0]
    qs = parse_qs(u.query)
    if "v" in qs:
        return qs["v"][0]
    parts = [p for p in u.path.split("/") if p]
    for marker in ("shorts", "live", "embed"):
        if marker in parts and parts.index(marker) + 1 < len(parts):
            return parts[parts.index(marker) + 1]
    raise ValueError(f"Can't find a video ID in {url_or_id!r}")


DEFAULT_MIN_LEAD_MINUTES = 15


def parse_publish_at(value, min_lead_minutes=DEFAULT_MIN_LEAD_MINUTES):
    """Local 'YYYY-MM-DD HH:MM[:SS]' (string or YAML datetime) -> RFC 3339 string.

    Rejects times less than min_lead_minutes from now, so a video can't go
    live before it has processed and you've had a chance to check it.
    """
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, datetime.date):
        raise ValueError(f"publishAt needs a time as well as a date: {value}")
    else:
        s = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"publishAt must look like 2026-11-21 13:00 (got {value!r})")
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret as this computer's local time
    now = datetime.datetime.now(dt.tzinfo)
    if dt <= now:
        raise ValueError(f"publishAt {format_when(dt)} is in the past")
    if dt < now + datetime.timedelta(minutes=min_lead_minutes):
        raise ValueError(f"publishAt {format_when(dt)} is less than {min_lead_minutes} minutes "
                         "away; pick a later time (or change --min-lead)")
    return dt.isoformat()


# --------------------------------------------------------------------------
# Plain-language visibility
# --------------------------------------------------------------------------

def _as_datetime(value):
    if isinstance(value, datetime.datetime):
        return value
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def format_when(value):
    """'Sat Nov 22, 1:00 PM CST (in 1 day 4 hr)' in this computer's local time."""
    dt = _as_datetime(value).astimezone()
    stamp = dt.strftime("%a %b %d, %I:%M %p %Z").replace(" 0", " ")
    secs = int((dt - datetime.datetime.now(dt.tzinfo)).total_seconds())
    if secs < 0:
        return f"{stamp} (already passed)"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = ([f"{days} day{'s' if days != 1 else ''}"] if days else []) + \
            ([f"{hours} hr"] if hours else []) + \
            ([f"{minutes} min"] if minutes and not days else [])
    return f"{stamp} (in {' '.join(parts) or 'under a minute'})"


def visibility(status):
    """How visible a video with this status is, in words."""
    privacy = status.get("privacyStatus", "private")
    if status.get("publishAt") and privacy == "private":
        return f"private, goes PUBLIC {format_when(status['publishAt'])}"
    if privacy == "public":
        return "PUBLIC immediately"
    if privacy == "unlisted":
        return "UNLISTED (anyone with the link)"
    return "private"


def goes_live(status):
    """True if this status makes the video visible to others, now or later."""
    return (status.get("privacyStatus") in ("public", "unlisted")
            or bool(status.get("publishAt")))


def confirm(lines, assume_yes=False, input_fn=None, stream=None):
    """Show what will become visible and ask. Returns True to go ahead."""
    input_fn = input_fn or input
    stream = stream or sys.stdin
    print("\nThese will be visible to others:")
    for line in lines:
        print(f"  {line}")
    if assume_yes:
        print("Going ahead (--yes).")
        return True
    if not stream.isatty():
        print("Not running in a terminal, so I can't ask. Rerun with --yes to confirm.")
        return False
    try:
        answer = input_fn("Continue? [y/N] ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def parse_recording_date(value):
    """'2026-05-20', 20260520, or a YAML date -> RFC 3339 string for recordingDetails."""
    if isinstance(value, datetime.datetime):
        d = value.date()
    elif isinstance(value, datetime.date):
        d = value
    else:
        s = str(value).strip()
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                d = datetime.datetime.strptime(s, fmt).date()
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"recordingDate must look like 2026-05-20 (got {value!r})")
    return f"{d.isoformat()}T00:00:00Z"


def check_text(field, text, problems, max_chars=None, max_bytes=None):
    if "<" in text or ">" in text:
        problems.append(f"{field} can't contain < or >")
    if max_chars and len(text) > max_chars:
        problems.append(f"{field} is {len(text)} characters (max {max_chars})")
    if max_bytes and len(text.encode("utf-8")) > max_bytes:
        problems.append(f"{field} is {len(text.encode('utf-8'))} bytes (max {max_bytes})")


# --------------------------------------------------------------------------
# Thumbnails
# --------------------------------------------------------------------------

THUMBNAIL_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
MAX_THUMBNAIL_BYTES = 50 * 1024 * 1024


def resolve_path(value, base_dir):
    """Expand ~ and make a relative path relative to base_dir."""
    p = os.path.expanduser(str(value))
    return p if os.path.isabs(p) else os.path.join(base_dir, p)


def check_thumbnail(path, problems):
    ext = os.path.splitext(path)[1].lower()
    if ext not in THUMBNAIL_TYPES:
        problems.append(f"thumbnail must be .jpg or .png: {path}")
    elif not os.path.isfile(path):
        problems.append(f"thumbnail not found: {path}")
    elif os.path.getsize(path) > MAX_THUMBNAIL_BYTES:
        problems.append(f"thumbnail is over 50 MB: {path}")


def set_thumbnail(youtube, video_id, path):
    """thumbnails.set (about 50 quota units). Raises HttpError on failure."""
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(path, mimetype=THUMBNAIL_TYPES[os.path.splitext(path)[1].lower()])
    return youtube.thumbnails().set(videoId=video_id, media_body=media).execute(num_retries=3)


def thumbnail_error_message(e):
    reason = http_error_reason(e)
    if e.resp.status == 403:
        return ("not allowed (403). Custom thumbnails need a verified channel: "
                "check youtube.com/verify, then retry with draft_updater.py")
    if reason == "uploadRateLimitExceeded":
        return "too many thumbnails uploaded recently (429); retry later with draft_updater.py"
    if reason == "invalidImage":
        return "YouTube rejected the image (invalidImage)"
    return f"HTTP {e.resp.status} {reason}".strip()


def processing_status(youtube, video_ids):
    """{video_id: 'processing' | 'succeeded' | 'failed' | 'terminated' | 'missing'}"""
    found = {}
    ids = list(video_ids)
    for i in range(0, len(ids), 50):
        resp = youtube.videos().list(part="processingDetails",
                                     id=",".join(ids[i:i + 50])).execute()
        for item in resp.get("items", []):
            found[item["id"]] = item.get("processingDetails", {}).get("processingStatus", "processing")
    return {v: found.get(v, "missing") for v in ids}


def wait_for_processing(youtube, video_ids, timeout, interval=20, sleep=None, clock=None):
    """Poll until every video has finished processing or the timeout passes.

    Costs 1 quota unit per poll for up to 50 videos. Returns the last status map.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + timeout
    pending = set(video_ids)
    statuses = {}
    while pending:
        statuses.update(processing_status(youtube, sorted(pending)))
        pending = {v for v in pending if statuses[v] in ("processing", "missing")}
        if not pending or clock() >= deadline:
            break
        print(f"  waiting for YouTube to finish processing {len(pending)} video(s)...")
        sleep(interval)
    return statuses


# --------------------------------------------------------------------------
# Duplicate check against the channel
# --------------------------------------------------------------------------

def normalize_title(title):
    return " ".join(str(title).split()).casefold()


def channel_videos(youtube):
    """Every video on the signed-in channel: [{id, title, fileName}].

    fileName is the name of the file originally uploaded (owner-only data;
    empty for some very old videos). Costs 1 quota unit per 50 videos for the
    list, plus 1 per 50 for the file names, plus 1 to find the uploads list.
    """
    channels = youtube.channels().list(part="contentDetails", mine=True).execute().get("items", [])
    if not channels:
        return []
    uploads = channels[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    videos, token = [], None
    while True:
        resp = youtube.playlistItems().list(part="snippet", playlistId=uploads,
                                            maxResults=50, pageToken=token).execute()
        for item in resp.get("items", []):
            sn = item["snippet"]
            videos.append({"id": sn["resourceId"]["videoId"], "title": sn.get("title", "")})
        token = resp.get("nextPageToken")
        if not token:
            break

    live = []
    for i in range(0, len(videos), 50):
        chunk = videos[i:i + 50]
        resp = youtube.videos().list(part="fileDetails",
                                     id=",".join(v["id"] for v in chunk)).execute()
        names = {it["id"]: it.get("fileDetails", {}).get("fileName", "")
                 for it in resp.get("items", [])}
        for v in chunk:
            # videos.list leaves out deleted videos, even if the uploads list
            # hasn't caught up with the deletion yet.
            if v["id"] in names:
                v["fileName"] = names[v["id"]]
                live.append(v)
    return live


class DuplicateIndex:
    """Look up existing channel videos by original filename or title."""

    def __init__(self, videos):
        self.by_file, self.by_title = {}, {}
        self.ids = {v["id"] for v in videos}
        for v in videos:
            if v.get("fileName"):
                self.by_file.setdefault(os.path.basename(v["fileName"]).casefold(), []).append(v)
            if v.get("title"):
                self.by_title.setdefault(normalize_title(v["title"]), []).append(v)
        self.count = len(videos)

    def matches(self, filename, title):
        """Reasons this upload looks like a duplicate, e.g. ['same file name as abc123 "Title"']."""
        reasons, seen = [], set()
        for v in self.by_file.get(os.path.basename(filename).casefold(), []):
            seen.add(v["id"])
            reasons.append(f"same file name as https://youtu.be/{v['id']} \"{v['title']}\"")
        for v in self.by_title.get(normalize_title(title), []):
            if v["id"] not in seen:
                reasons.append(f"same title as https://youtu.be/{v['id']}")
        return reasons


def check_tags(tags, problems):
    n = tags_length(tags)
    if n > MAX_TAGS_CHARS:
        problems.append(f"tags total {n} characters (max {MAX_TAGS_CHARS})")


def seconds_until(value):
    dt = _as_datetime(value)
    return (dt - datetime.datetime.now(dt.tzinfo)).total_seconds()
