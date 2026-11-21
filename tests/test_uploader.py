"""Tests that run without network or Google credentials (fake YouTube service)."""
import datetime
import json
import os
import sys

import httplib2
import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aaron_uploader  # noqa: E402
import draft_updater  # noqa: E402
import yt_common as yt  # noqa: E402

FUTURE = (datetime.datetime.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%d %H:%M")


def http_error(status, reason=""):
    content = json.dumps({"error": {"errors": [{"reason": reason}], "message": reason}}).encode()
    return HttpError(httplib2.Response({"status": status}), content)


# ---------------------------------------------------------------- fakes

class FakeRequest:
    def __init__(self, results):
        self.results = list(results)

    def next_chunk(self):
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return None, r

    def execute(self):
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeVideos:
    def __init__(self, insert_results=None, current=None, processing=None):
        self.insert_results = list(insert_results or [])
        self.inserted, self.updated, self.list_calls = [], [], 0
        self.current = current or {}
        # processing: {video_id: [status on 1st poll, 2nd poll, ...]} (last one repeats)
        self.processing = {k: list(v) for k, v in (processing or {}).items()}
        self.files = {}

    def insert(self, part, body, media_body):
        self.inserted.append((part, body))
        return FakeRequest([self.insert_results.pop(0)])

    def list(self, part, id):
        self.list_calls += 1
        items = []
        for vid in id.split(","):
            if part == "fileDetails":
                if vid in self.files:
                    items.append({"id": vid, "fileDetails": {"fileName": self.files[vid]}})
                continue
            if vid in self.current:
                items.append(self.current[vid])
            elif vid in self.processing:
                seq = self.processing[vid]
                state = seq.pop(0) if len(seq) > 1 else seq[0]
                items.append({"id": vid, "processingDetails": {"processingStatus": state}})
        return FakeRequest([{"items": items}])

    def update(self, part, body):
        self.updated.append((part, body))
        return FakeRequest([body])


class FakeThumbnails:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.set_calls = []

    def set(self, videoId, media_body):
        self.set_calls.append((videoId, media_body._filename, media_body.mimetype()))
        r = self.results.pop(0) if self.results else {"items": []}


        class Req:
            def execute(self, num_retries=0):
                if isinstance(r, Exception):
                    raise r
                return r
        return Req()


class FakeChannel:
    """channels/playlistItems for a channel with `existing` videos [{id, title, fileName}]."""

    def __init__(self, existing, page_size=2):
        self.existing = existing
        self.page_size = page_size
        self.pages_read = 0

    def channels_list(self, part, mine):
        return FakeRequest([{"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}])

    def playlist_list(self, part, playlistId, maxResults, pageToken=None):
        assert playlistId == "UU1"
        start = int(pageToken or 0)
        chunk = self.existing[start:start + self.page_size]
        resp = {"items": [{"snippet": {"title": v["title"], "resourceId": {"videoId": v["id"]}}}
                          for v in chunk]}
        if start + self.page_size < len(self.existing):
            resp["nextPageToken"] = str(start + self.page_size)
        self.pages_read += 1
        return FakeRequest([resp])


class FakeYouTube:
    def __init__(self, thumbnail_results=None, existing=None, **kw):
        self._videos = FakeVideos(**kw)
        self._thumbs = FakeThumbnails(thumbnail_results)
        self.channel = FakeChannel(existing or [])
        for v in existing or []:
            if not v.get("deleted"):  # deleted videos are still in the uploads list, briefly
                self._videos.files[v["id"]] = v.get("fileName", "")

    def videos(self):
        return self._videos

    def thumbnails(self):
        return self._thumbs

    def channels(self):
        return type("C", (), {"list": lambda _s, **k: self.channel.channels_list(**k)})()

    def playlistItems(self):
        return type("P", (), {"list": lambda _s, **k: self.channel.playlist_list(**k)})()


@pytest.fixture
def workdir(tmp_path):
    vids = tmp_path / "vids"
    vids.mkdir()
    for name in ("a.mov", "b.mov", "c.mov"):
        (vids / name).write_bytes(b"x" * 10)
    return tmp_path


def write_yaml(path, text):
    path.write_text(text)
    return str(path)


# ---------------------------------------------------------------- parsing

@pytest.mark.parametrize("url,expected", [
    ("https://youtu.be/zVtUCUpq1vs", "zVtUCUpq1vs"),
    ("https://youtu.be/zVtUCUpq1vs?si=abc123", "zVtUCUpq1vs"),
    ("https://youtube.com/shorts/9cEKURK164w", "9cEKURK164w"),
    ("https://www.youtube.com/shorts/9cEKURK164w?feature=share", "9cEKURK164w"),
    ("https://www.youtube.com/watch?v=abcDEF12345&t=10", "abcDEF12345"),
    ("youtube.com/watch?v=abcDEF12345", "abcDEF12345"),
    ("abcDEF12345", "abcDEF12345"),
    ("https://studio.youtube.com/video/abcDEF12345/edit", None),
])
def test_parse_video_id(url, expected):
    if expected is None:
        with pytest.raises(ValueError):
            yt.parse_video_id(url)
    else:
        assert yt.parse_video_id(url) == expected


def test_parse_tags_strips_and_drops_blanks():
    assert yt.parse_tags("atl, atlanta ,,marta ") == ["atl", "atlanta", "marta"]
    assert yt.parse_tags(["a ", " b"]) == ["a", "b"]
    assert yt.parse_tags(None) == []


def test_tags_length_counts_quotes_for_spaces():
    assert yt.tags_length(["ab", "c d"]) == 2 + 5 + 1


def test_publish_at_accepts_string_and_yaml_datetime():
    s = yt.parse_publish_at(FUTURE)
    assert s.startswith(FUTURE.replace(" ", "T")) and ("+" in s[10:] or "-" in s[10:])
    dt = datetime.datetime.now() + datetime.timedelta(days=1)
    assert yt.parse_publish_at(dt)
    with pytest.raises(ValueError):
        yt.parse_publish_at("2020-01-01 10:00")
    with pytest.raises(ValueError):
        yt.parse_publish_at("tomorrow")


def test_recording_date_formats():
    assert yt.parse_recording_date("2026-05-20") == "2026-05-20T00:00:00Z"
    assert yt.parse_recording_date(20260520) == "2026-05-20T00:00:00Z"
    assert yt.parse_recording_date(datetime.date(2026, 5, 20)) == "2026-05-20T00:00:00Z"


# ---------------------------------------------------------------- retry

def test_resumable_upload_retries_then_succeeds():
    req = FakeRequest([http_error(503), OSError("reset"), {"id": "VID"}])
    sleeps = []
    assert yt.resumable_upload(req, sleep=sleeps.append)["id"] == "VID"
    assert len(sleeps) == 2


def test_resumable_upload_does_not_sleep_after_success():
    req = FakeRequest([http_error(503), {"id": "VID"}])
    sleeps = []
    yt.resumable_upload(req, sleep=sleeps.append)
    assert len(sleeps) == 1  # old code kept sleeping after the error was over


def test_resumable_upload_raises_non_retriable():
    with pytest.raises(HttpError):
        yt.resumable_upload(FakeRequest([http_error(400, "invalidTitle")]), sleep=lambda s: None)


def test_resumable_upload_gives_up():
    req = FakeRequest([http_error(500)] * (yt.MAX_RETRIES + 1))
    with pytest.raises(RuntimeError):
        yt.resumable_upload(req, sleep=lambda s: None)


# ---------------------------------------------------------------- uploader

def test_vids_argument_is_used(workdir, capsys):
    custom = write_yaml(workdir / "custom.yml", 'video_dir: vids\nvideos:\n  - filename: a.mov\n    title: "From custom"\n')
    assert aaron_uploader.main(["--vids", custom, "--dry-run", "--no-channel-check", "--log", str(workdir / "log.json")]) == 0
    assert "From custom" in capsys.readouterr().out


def test_validation_blocks_whole_batch(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - filename: a.mov\n    title: ok\n"
                   "  - title: no filename\n"
                   "  - filename: missing.mov\n    title: x\n"
                   "  - filename: b.mov\n    title: 'bad <title>'\n    privacyStatus: secret\n")
    yt_fake = FakeYouTube()
    assert aaron_uploader.main(["--vids", f, "--log", str(workdir / "log.json")], youtube=yt_fake) == 2
    out = capsys.readouterr().out
    assert "missing filename" in out and "file not found" in out
    assert "can't contain < or >" in out and "privacyStatus must be" in out
    assert yt_fake.videos().inserted == []


def test_body_defaults_and_options(workdir):
    job, problems = aaron_uploader.build_job(
        {"filename": "a.mov", "title": " T ", "description": "line1\nline2\n",
         "keywords": "a, b", "privacyStatus": "public", "publishAt": FUTURE,
         "recordingDate": "2026-05-20"}, str(workdir / "vids"))
    assert problems == []
    b = job["body"]
    assert b["snippet"]["title"] == "T"
    assert b["snippet"]["description"] == "line1\nline2"
    assert b["snippet"]["tags"] == ["a", "b"]
    assert b["status"]["selfDeclaredMadeForKids"] is False
    assert "madeForKids" not in b["status"]
    assert b["status"]["privacyStatus"] == "private"  # forced by publishAt
    assert b["recordingDetails"]["recordingDate"] == "2026-05-20T00:00:00Z"


def test_upload_log_prevents_reupload_after_partial_failure(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: A}\n"
                   "  - {filename: b.mov, title: B}\n"
                   "  - {filename: c.mov, title: C}\n")
    log = str(workdir / "log.json")

    # First run: a succeeds, b fails with a non-fatal error, c succeeds.
    fake = FakeYouTube(insert_results=[{"id": "IDA"}, http_error(400, "invalidCategoryId"), {"id": "IDC"}])
    assert aaron_uploader.main(["--vids", f, "--log", log], youtube=fake) == 1
    assert [b["snippet"]["title"] for _, b in fake.videos().inserted] == ["A", "B", "C"]
    saved = json.load(open(log))
    assert sorted(v["video_id"] for v in saved.values()) == ["IDA", "IDC"]

    # Rerun: only b is attempted.
    on_channel = [{"id": "IDA", "title": "A", "fileName": "a.mov"},
                  {"id": "IDC", "title": "C", "fileName": "c.mov"}]
    fake2 = FakeYouTube(insert_results=[{"id": "IDB"}], existing=on_channel)
    assert aaron_uploader.main(["--vids", f, "--log", log], youtube=fake2) == 0
    assert [b["snippet"]["title"] for _, b in fake2.videos().inserted] == ["B"]
    assert "DUPLICATE a.mov: skipped" in capsys.readouterr().out


def test_quota_error_stops_batch(workdir):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: A}\n  - {filename: b.mov, title: B}\n")
    fake = FakeYouTube(insert_results=[http_error(403, "quotaExceeded"), {"id": "never"}])
    assert aaron_uploader.main(["--vids", f, "--log", str(workdir / "log.json")], youtube=fake) == 1
    assert len(fake.videos().inserted) == 1


def test_video_dir_relative_to_yaml_and_tilde(workdir, monkeypatch):
    monkeypatch.setenv("HOME", str(workdir))
    f = write_yaml(workdir / "v.yml", "video_dir: '~/vids'\nvideos:\n  - {filename: a.mov, title: A}\n")
    assert aaron_uploader.main(["--vids", f, "--dry-run", "--no-channel-check", "--log", str(workdir / "l.json")]) == 0


# ---------------------------------------------------------------- updater

CURRENT = {
    "id": "VID",
    "snippet": {"title": "Old", "description": "Old desc", "tags": ["x"], "categoryId": "19",
                "defaultLanguage": "en", "defaultAudioLanguage": "en",
                "channelId": "C", "publishedAt": "2026-01-01T00:00:00Z", "thumbnails": {}},
    "status": {"privacyStatus": "public", "embeddable": True, "license": "youtube",
               "publicStatsViewable": True, "selfDeclaredMadeForKids": False,
               "madeForKids": False, "uploadStatus": "processed"},
}


def run_updater(tmp_path, yaml_text, current=CURRENT, dry=False, yes=True):
    f = write_yaml(tmp_path / "d.yml", yaml_text)
    fake = FakeYouTube(current={"VID": current})
    argv = ["--vids", f] + (["--dry-run"] if dry else []) + (["--yes"] if yes else [])
    return draft_updater.main(argv, youtube=fake), fake.videos()


def test_updater_title_only_keeps_everything_else(tmp_path):
    rc, v = run_updater(tmp_path, "videos:\n  - url: 'https://youtu.be/VID?si=zz'\n    title: New\n")
    assert rc == 0
    part, body = v.updated[0]
    assert part == "snippet"  # no status, no localizations
    assert body["snippet"] == {"title": "New", "description": "Old desc", "tags": ["x"],
                               "categoryId": "19", "defaultLanguage": "en", "defaultAudioLanguage": "en"}


def test_updater_does_not_make_public_video_private(tmp_path):
    rc, v = run_updater(tmp_path, "videos:\n  - url: VID\n    description: |\n      a\n      b\n")
    part, body = v.updated[0]
    assert "status" not in body
    assert body["snippet"]["description"] == "a\nb"


def test_updater_schedule_sets_private_and_keeps_status_fields(tmp_path):
    draft = dict(CURRENT, status=dict(CURRENT["status"], privacyStatus="private"))
    rc, v = run_updater(tmp_path, f"videos:\n  - url: VID\n    publishAt: '{FUTURE}'\n", current=draft)
    part, body = v.updated[0]
    assert part == "snippet,status"
    st = body["status"]
    assert st["privacyStatus"] == "private" and "publishAt" in st
    assert st["embeddable"] is True and "madeForKids" not in st and "uploadStatus" not in st


def test_updater_rejects_publishat_with_public(tmp_path, capsys):
    rc, v = run_updater(tmp_path, f"videos:\n  - url: VID\n    publishAt: '{FUTURE}'\n    privacyStatus: public\n")
    assert rc == 2 and v.updated == []


def test_updater_dry_run_saves_nothing(tmp_path, capsys):
    rc, v = run_updater(tmp_path, "videos:\n  - url: VID\n    title: New\n", dry=True)
    assert rc == 0 and v.updated == []
    assert "'Old' -> 'New'" in capsys.readouterr().out


def test_updater_unknown_video(tmp_path):
    f = write_yaml(tmp_path / "d.yml", "videos:\n  - url: NOPE\n    title: x\n")
    fake = FakeYouTube(current={})
    assert draft_updater.main(["--vids", f], youtube=fake) == 1


# ---------------------------------------------------------------- thumbnails

@pytest.fixture
def thumbdir(workdir):
    (workdir / "vids" / "a.jpg").write_bytes(b"\xff\xd8jpeg")
    (workdir / "vids" / "b.png").write_bytes(b"\x89PNG")
    return workdir


def no_sleep(monkeypatch):
    monkeypatch.setattr(yt.time, "sleep", lambda s: None)


def test_thumbnail_validation(thumbdir, capsys):
    f = write_yaml(thumbdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: A, thumbnail: nope.jpg}\n"
                   "  - {filename: b.mov, title: B, thumbnail: b.gif}\n")
    assert aaron_uploader.main(["--vids", f, "--dry-run", "--no-channel-check", "--log", str(thumbdir / "l.json")]) == 2
    out = capsys.readouterr().out
    assert "thumbnail not found" in out and "must be .jpg or .png" in out


def test_thumbnail_set_after_processing(thumbdir, monkeypatch):
    no_sleep(monkeypatch)
    f = write_yaml(thumbdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: A, thumbnail: a.jpg}\n"
                   "  - {filename: b.mov, title: B, thumbnail: b.png}\n"
                   "  - {filename: c.mov, title: C}\n")
    log = str(thumbdir / "l.json")
    fake = FakeYouTube(insert_results=[{"id": "IA"}, {"id": "IB"}, {"id": "IC"}],
                       processing={"IA": ["processing", "succeeded"], "IB": ["succeeded"]})
    assert aaron_uploader.main(["--vids", f, "--log", log], youtube=fake) == 0
    calls = fake.thumbnails().set_calls
    assert [(v, m) for v, _, m in calls] == [("IA", "image/jpeg"), ("IB", "image/png")]
    # all three uploads happened before any thumbnail was set
    assert len(fake.videos().inserted) == 3
    saved = json.load(open(log))
    assert sorted(e.get("thumbnail_status") for e in saved.values() if "thumbnail" in e) == ["set", "set"]


def test_thumbnail_pending_is_retried_on_rerun(thumbdir, monkeypatch, capsys):
    no_sleep(monkeypatch)
    f = write_yaml(thumbdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: A, thumbnail: a.jpg}\n")
    log = str(thumbdir / "l.json")
    # Processing never finishes within the wait.
    fake = FakeYouTube(insert_results=[{"id": "IA"}], processing={"IA": ["processing"]})
    assert aaron_uploader.main(["--vids", f, "--log", log, "--thumbnail-wait", "0"], youtube=fake) == 1
    assert fake.thumbnails().set_calls == []
    assert json.load(open(log)).popitem()[1]["thumbnail_status"].startswith("pending")

    # Rerun: no re-upload, thumbnail gets set.
    on_channel = [{"id": "IA", "title": "A", "fileName": "a.mov"}]
    fake2 = FakeYouTube(processing={"IA": ["succeeded"]}, existing=on_channel)
    assert aaron_uploader.main(["--vids", f, "--log", log], youtube=fake2) == 0
    assert fake2.videos().inserted == []
    assert [v for v, _, _ in fake2.thumbnails().set_calls] == ["IA"]
    assert json.load(open(log)).popitem()[1]["thumbnail_status"] == "set"

    # Third run: nothing to do.
    fake3 = FakeYouTube(existing=on_channel)
    assert aaron_uploader.main(["--vids", f, "--log", log], youtube=fake3) == 0
    assert fake3.thumbnails().set_calls == [] and fake3.videos().inserted == []


def test_thumbnail_forbidden_does_not_fail_upload(thumbdir, monkeypatch, capsys):
    no_sleep(monkeypatch)
    f = write_yaml(thumbdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: A, thumbnail: a.jpg}\n")
    log = str(thumbdir / "l.json")
    fake = FakeYouTube(insert_results=[{"id": "IA"}], processing={"IA": ["succeeded"]},
                       thumbnail_results=[http_error(403, "forbidden")])
    assert aaron_uploader.main(["--vids", f, "--log", log], youtube=fake) == 1
    entry = json.load(open(log)).popitem()[1]
    assert entry["video_id"] == "IA"
    assert "verified channel" in entry["thumbnail_status"]


def test_wait_for_processing_batches_and_times_out():
    fake = FakeYouTube(processing={"X": ["processing"], "Y": ["succeeded"]})
    t = [0]
    st = yt.wait_for_processing(fake, ["X", "Y"], timeout=60, interval=20,
                                sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert st == {"X": "processing", "Y": "succeeded"}
    assert fake.videos().list_calls == 4  # t=0,20,40,60


def test_updater_thumbnail_only(tmp_path):
    (tmp_path / "t.png").write_bytes(b"\x89PNG")
    f = write_yaml(tmp_path / "d.yml", "videos:\n  - url: VID\n    thumbnail: t.png\n")
    fake = FakeYouTube(current={"VID": dict(CURRENT, processingDetails={"processingStatus": "succeeded"})})
    assert draft_updater.main(["--vids", f], youtube=fake) == 0
    assert fake.videos().updated == []  # no videos.update for a thumbnail-only change
    assert fake.thumbnails().set_calls[0][:2] == ("VID", str(tmp_path / "t.png"))


def test_updater_thumbnail_waits_for_processing(tmp_path):
    (tmp_path / "t.jpg").write_bytes(b"\xff\xd8")
    f = write_yaml(tmp_path / "d.yml", "videos:\n  - url: VID\n    title: New\n    thumbnail: t.jpg\n")
    fake = FakeYouTube(current={"VID": dict(CURRENT, processingDetails={"processingStatus": "processing"})})
    assert draft_updater.main(["--vids", f], youtube=fake) == 1
    assert fake.thumbnails().set_calls == [] and fake.videos().updated == []


# ---------------------------------------------------------------- duplicates vs channel

EXISTING = [
    {"id": "OLD1", "title": "BNSF Tankers Passing GBB Station 20260520", "fileName": "A.MOV"},
    {"id": "OLD2", "title": "Big Boy at West Chicago", "fileName": "IMG_0001.mov"},
    {"id": "OLD3", "title": "Ancient video", "fileName": ""},
]


def dup_yaml(workdir):
    return write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                      "  - {filename: a.mov, title: A Wave from a BNSF Tanker Train 20260520}\n"
                      "  - {filename: b.mov, title: '  big boy AT west   chicago '}\n"
                      "  - {filename: c.mov, title: Brand new}\n")


def test_channel_duplicates_are_skipped(workdir, capsys):
    f = dup_yaml(workdir)
    fake = FakeYouTube(existing=EXISTING, insert_results=[{"id": "NEWC"}])
    assert aaron_uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 0
    assert [b["snippet"]["title"] for _, b in fake.videos().inserted] == ["Brand new"]
    out = capsys.readouterr().out
    assert "DUPLICATE a.mov: skipped" in out and "same file name as https://youtu.be/OLD1" in out
    assert "DUPLICATE b.mov: skipped" in out and "same title as https://youtu.be/OLD2" in out
    assert '--allow-duplicate "a.mov"' in out
    assert "3 videos on the channel" in out
    assert fake.channel.pages_read == 2  # paged through the uploads list


def test_allow_duplicate_for_one_file(workdir, capsys):
    f = dup_yaml(workdir)
    fake = FakeYouTube(existing=EXISTING, insert_results=[{"id": "NEWA"}, {"id": "NEWC"}])
    assert aaron_uploader.main(["--vids", f, "--log", str(workdir / "l.json"),
                                "--allow-duplicate", "a.mov"], youtube=fake) == 0
    assert [b["snippet"]["title"] for _, b in fake.videos().inserted] == \
        ["A Wave from a BNSF Tanker Train 20260520", "Brand new"]
    out = capsys.readouterr().out
    assert "DUPLICATE a.mov: uploading anyway" in out and "DUPLICATE b.mov: skipped" in out


def test_force_allows_all_duplicates(workdir):
    f = dup_yaml(workdir)
    fake = FakeYouTube(existing=EXISTING, insert_results=[{"id": "1"}, {"id": "2"}, {"id": "3"}])
    assert aaron_uploader.main(["--vids", f, "--log", str(workdir / "l.json"), "--force"],
                               youtube=fake) == 0
    assert len(fake.videos().inserted) == 3


def test_allow_duplicate_typo_is_an_error(workdir, capsys):
    f = dup_yaml(workdir)
    fake = FakeYouTube(existing=EXISTING)
    assert aaron_uploader.main(["--vids", f, "--log", str(workdir / "l.json"),
                                "--allow-duplicate", "aa.mov"], youtube=fake) == 2
    assert "--allow-duplicate aa.mov: not in" in capsys.readouterr().out
    assert fake.videos().inserted == []


def test_same_file_listed_twice_is_an_error(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: One}\n  - {filename: a.mov, title: Two}\n")
    assert aaron_uploader.main(["--vids", f, "--no-channel-check", "--dry-run",
                                "--log", str(workdir / "l.json")]) == 2
    assert "listed twice" in capsys.readouterr().out


def test_dry_run_reports_channel_duplicates(workdir, capsys):
    f = dup_yaml(workdir)
    fake = FakeYouTube(existing=EXISTING)
    assert aaron_uploader.main(["--vids", f, "--log", str(workdir / "l.json"), "--dry-run"],
                               youtube=fake) == 0
    out = capsys.readouterr().out
    assert "OK   c.mov" in out and "1 to upload, 2 duplicate(s) skipped" in out
    assert fake.videos().inserted == []


def test_log_and_channel_match_reported_once(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    log = str(workdir / "l.json")
    aaron_uploader.main(["--vids", f, "--log", log, "--no-channel-check"],
                        youtube=FakeYouTube(insert_results=[{"id": "IDA"}]))
    capsys.readouterr()
    fake = FakeYouTube(existing=[{"id": "IDA", "title": "A", "fileName": "a.mov"}])
    aaron_uploader.main(["--vids", f, "--log", log], youtube=fake)
    out = capsys.readouterr().out
    assert "already uploaded by this tool as https://youtu.be/IDA" in out
    assert "same file name" not in out and "same title" not in out


def test_reupload_keeps_previous_log_entry(workdir):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    log = str(workdir / "l.json")
    aaron_uploader.main(["--vids", f, "--log", log, "--no-channel-check"],
                        youtube=FakeYouTube(insert_results=[{"id": "FIRST"}]))
    aaron_uploader.main(["--vids", f, "--log", log, "--no-channel-check", "--allow-duplicate", "a.mov"],
                        youtube=FakeYouTube(insert_results=[{"id": "SECOND"}]))
    entry = json.load(open(log)).popitem()[1]
    assert entry["video_id"] == "SECOND"
    assert [p["video_id"] for p in entry["previous_uploads"]] == ["FIRST"]


def test_channel_read_error_uploads_nothing(workdir, capsys):
    f = dup_yaml(workdir)
    fake = FakeYouTube()
    fake.channel.channels_list = lambda part, mine: FakeRequest([http_error(403, "forbidden")])
    assert aaron_uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 1
    assert fake.videos().inserted == []
    assert "--no-channel-check" in capsys.readouterr().out


def test_deleted_video_in_log_is_not_a_duplicate(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    log = str(workdir / "l.json")
    aaron_uploader.main(["--vids", f, "--log", log, "--no-channel-check"],
                        youtube=FakeYouTube(insert_results=[{"id": "GONE"}]))
    capsys.readouterr()
    # The video was deleted on YouTube; the channel no longer has it.
    fake = FakeYouTube(existing=[{"id": "OTHER", "title": "x", "fileName": "x.mov"}],
                       insert_results=[{"id": "NEW"}])
    assert aaron_uploader.main(["--vids", f, "--log", log], youtube=fake) == 0
    out = capsys.readouterr().out
    assert "no longer on the channel" in out and "DUPLICATE" not in out
    entry = json.load(open(log)).popitem()[1]
    assert entry["video_id"] == "NEW" and entry["previous_uploads"][0]["video_id"] == "GONE"


def test_deleted_video_still_in_uploads_list_is_ignored(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    fake = FakeYouTube(existing=[{"id": "GONE", "title": "A", "fileName": "a.mov", "deleted": True}],
                       insert_results=[{"id": "NEW"}])
    assert aaron_uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 0
    out = capsys.readouterr().out
    assert "DUPLICATE" not in out and "0 videos on the channel" in out


def test_log_hit_without_channel_check_still_skips(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    log = str(workdir / "l.json")
    aaron_uploader.main(["--vids", f, "--log", log, "--no-channel-check"],
                        youtube=FakeYouTube(insert_results=[{"id": "IDA"}]))
    capsys.readouterr()
    fake = FakeYouTube()
    aaron_uploader.main(["--vids", f, "--log", log, "--no-channel-check"], youtube=fake)
    assert "DUPLICATE a.mov: skipped" in capsys.readouterr().out and fake.videos().inserted == []


# ---------------------------------------------------------------- examples stay complete

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
UPLOADER_KEYS = {"filename", "title", "description", "keywords", "categoryId", "privacyStatus",
                 "publishAt", "recordingDate", "madeForKids", "language", "thumbnail"}
UPDATER_KEYS = {"url", "title", "description", "keywords", "categoryId", "language",
                "privacyStatus", "publishAt", "recordingDate", "madeForKids", "thumbnail"}


def _keys_in_example(path):
    import re
    text = open(path).read()
    return set(re.findall(r"^\s+(?:- |# )?([A-Za-z]+):", text, re.M))


def test_uploader_example_shows_every_field_and_is_valid():
    path = os.path.join(EXAMPLES, "video_summary.example.yml")
    assert UPLOADER_KEYS <= _keys_in_example(path)
    data = yt.load_yaml(path)
    for vid in data["videos"]:
        _, problems = aaron_uploader.build_job(vid, "/nonexistent")
        problems = [p for p in problems if "not found" not in p and "in the past" not in p]
        assert problems == []


def test_updater_example_shows_every_field_and_is_valid():
    path = os.path.join(EXAMPLES, "draft_videos.example.yml")
    assert UPDATER_KEYS <= _keys_in_example(path)
    data = yt.load_yaml(path)
    for vid in data["videos"]:
        _, _, problems = draft_updater.parse_changes(vid, "/nonexistent")
        problems = [p for p in problems if "not found" not in p and "in the past" not in p]
        assert problems == []


def test_code_reads_no_undocumented_keys():
    """Every vid.get(...)/vid[...] key in the scripts must be in the key lists above."""
    import re
    for script, keys in (("aaron_uploader.py", UPLOADER_KEYS), ("draft_updater.py", UPDATER_KEYS)):
        src = open(os.path.join(os.path.dirname(EXAMPLES), script)).read()
        used = set(re.findall(r'vid(?:\.get\(|\[)"([A-Za-z]+)"', src))
        used |= set(re.findall(r'"([A-Za-z]+)" in vid', src))
        assert used <= keys, f"{script} reads {used - keys} but the example doesn't show it"



# ---------------------------------------------------------------- go-live safeguards

def in_minutes(m):
    return (datetime.datetime.now() + datetime.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M")


def test_publish_at_minimum_lead():
    with pytest.raises(ValueError, match="less than 15 minutes"):
        yt.parse_publish_at(in_minutes(5))
    assert yt.parse_publish_at(in_minutes(20))
    assert yt.parse_publish_at(in_minutes(5), min_lead_minutes=2)
    with pytest.raises(ValueError, match="in the past"):
        yt.parse_publish_at(in_minutes(-5), min_lead_minutes=0)


def test_visibility_wording():
    soon = (datetime.datetime.now().astimezone() + datetime.timedelta(days=1, hours=2, minutes=5)).isoformat()
    v = yt.visibility({"privacyStatus": "private", "publishAt": soon})
    assert v.startswith("private, goes PUBLIC ") and "(in 1 day 2 hr)" in v
    assert yt.visibility({"privacyStatus": "public"}) == "PUBLIC immediately"
    assert yt.visibility({"privacyStatus": "unlisted"}).startswith("UNLISTED")
    assert yt.visibility({"privacyStatus": "private"}) == "private"
    assert yt.goes_live({"privacyStatus": "unlisted"}) and not yt.goes_live({"privacyStatus": "private"})


def golive_yaml(workdir):
    return write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                      "  - {filename: a.mov, title: Pub, privacyStatus: public}\n"
                      f"  - {{filename: b.mov, title: Sched, publishAt: '{in_minutes(60)}'}}\n"
                      "  - {filename: c.mov, title: Priv}\n")


def test_dry_run_spells_out_visibility(workdir, capsys):
    f = golive_yaml(workdir)
    aaron_uploader.main(["--vids", f, "--dry-run", "--no-channel-check", "--log", str(workdir / "l.json")])
    out = capsys.readouterr().out
    assert "PUBLIC immediately" in out and "goes PUBLIC" in out
    assert "(in 1 hr)" in out or "(in 59 min)" in out


def test_go_live_needs_confirmation_when_not_a_terminal(workdir, capsys):
    f = golive_yaml(workdir)
    fake = FakeYouTube(insert_results=[{"id": "1"}, {"id": "2"}, {"id": "3"}])
    assert aaron_uploader.main(["--vids", f, "--no-channel-check", "--log", str(workdir / "l.json")],
                               youtube=fake) == 3
    out = capsys.readouterr().out
    assert "These will be visible to others" in out and "Rerun with --yes" in out
    assert "c.mov" not in out.split("These will be visible")[1]  # private one isn't listed
    assert fake.videos().inserted == []


def test_go_live_with_yes_uploads(workdir, capsys):
    f = golive_yaml(workdir)
    fake = FakeYouTube(insert_results=[{"id": "1", "status": {"privacyStatus": "public"}},
                                       {"id": "2"}, {"id": "3"}])
    assert aaron_uploader.main(["--vids", f, "--no-channel-check", "--yes",
                                "--log", str(workdir / "l.json")], youtube=fake) == 0
    assert len(fake.videos().inserted) == 3
    assert "Visibility: PUBLIC immediately" in capsys.readouterr().out


class FakeTTY:
    def isatty(self):
        return True


@pytest.mark.parametrize("answer,code,uploads", [("y", 0, 3), ("n", 3, 0), ("", 3, 0)])
def test_go_live_prompt_answer(workdir, monkeypatch, answer, code, uploads):
    f = golive_yaml(workdir)
    monkeypatch.setattr(yt.sys, "stdin", FakeTTY())
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    fake = FakeYouTube(insert_results=[{"id": "1"}, {"id": "2"}, {"id": "3"}])
    assert aaron_uploader.main(["--vids", f, "--no-channel-check", "--log", str(workdir / "l.json")],
                               youtube=fake) == code
    assert len(fake.videos().inserted) == uploads


def test_private_only_batch_never_prompts(workdir):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    fake = FakeYouTube(insert_results=[{"id": "1"}])
    assert aaron_uploader.main(["--vids", f, "--no-channel-check", "--log", str(workdir / "l.json")],
                               youtube=fake) == 0


def test_schedule_too_close_by_upload_time_is_not_uploaded(workdir, monkeypatch, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   f"  - {{filename: a.mov, title: A, publishAt: '{in_minutes(60)}'}}\n")
    monkeypatch.setattr(yt, "seconds_until", lambda v: 120)  # time passed during earlier uploads
    fake = FakeYouTube(insert_results=[{"id": "1"}])
    assert aaron_uploader.main(["--vids", f, "--no-channel-check", "--yes",
                                "--log", str(workdir / "l.json")], youtube=fake) == 1
    assert fake.videos().inserted == [] and "too soon to upload" in capsys.readouterr().out


def test_min_lead_flag(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   f"  - {{filename: a.mov, title: A, publishAt: '{in_minutes(10)}'}}\n")
    args = ["--vids", f, "--dry-run", "--no-channel-check", "--log", str(workdir / "l.json")]
    assert aaron_uploader.main(args) == 2
    assert aaron_uploader.main(args + ["--min-lead", "5"]) == 0


def test_updater_title_change_on_public_video_does_not_prompt(tmp_path):
    rc, v = run_updater(tmp_path, "videos:\n  - url: VID\n    title: New\n", yes=False)
    assert rc == 0 and len(v.updated) == 1


def test_updater_making_public_needs_confirmation(tmp_path, capsys):
    draft = dict(CURRENT, status=dict(CURRENT["status"], privacyStatus="private"))
    rc, v = run_updater(tmp_path, "videos:\n  - url: VID\n    privacyStatus: public\n",
                        current=draft, yes=False)
    assert rc == 3 and v.updated == []
    out = capsys.readouterr().out
    assert "visibility: private -> PUBLIC immediately" in out and "Rerun with --yes" in out
    rc, v = run_updater(tmp_path, "videos:\n  - url: VID\n    privacyStatus: public\n",
                        current=draft, yes=True)
    assert rc == 0 and len(v.updated) == 1


def test_updater_making_private_does_not_prompt(tmp_path):
    rc, v = run_updater(tmp_path, "videos:\n  - url: VID\n    privacyStatus: private\n", yes=False)
    assert rc == 0 and v.updated[0][1]["status"]["privacyStatus"] == "private"
