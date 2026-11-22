"""Tests that run without network or Google credentials (fake YouTube service)."""
import datetime
import json
import os
import sys

import httplib2
import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uploader  # noqa: E402
import updater  # noqa: E402
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
            if "statistics" in part:
                if vid in self.files:
                    v = self.files[vid]
                    items.append({"id": vid, "statistics": {"viewCount": str(v.get("views", 0))},
                                  "snippet": {"publishedAt": v.get("published")}})
                continue
            if "fileDetails" in part:
                if vid in self.files:
                    v = self.files[vid]
                    item = {"id": vid,
                            "fileDetails": {"fileName": v.get("fileName", ""),
                                            **({"fileSize": str(v["fileSize"])} if v.get("fileSize") else {})},
                            "contentDetails": {"duration": v.get("duration", "PT45S")},
                            "status": {"privacyStatus": v.get("privacyStatus", "public")}}
                    if v.get("publishAt"):
                        item["status"]["publishAt"] = v["publishAt"]
                    if v.get("publishedAt"):
                        item["snippet"] = {"publishedAt": v["publishedAt"]}
                    if v.get("size"):
                        w, h = v["size"]
                        item["fileDetails"]["videoStreams"] = [{"widthPixels": w, "heightPixels": h}]
                    items.append(item)
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


class FakePlaylists:
    """playlists.list pages + playlistItems.insert results."""

    def __init__(self, playlists, insert_results=None, members=None):
        self.playlists = playlists
        self.insert_results = list(insert_results or [])
        self.added = []  # (playlist_id, video_id)
        self.members = set(members or [])  # (playlist_id, video_id) already in a playlist
        self.member_checks = 0

    def membership(self, part, playlistId, videoId):
        self.member_checks += 1
        hit = (playlistId, videoId) in self.members
        return FakeRequest([{"items": [{"id": "x"}] if hit else []}])

    def list(self, part, mine, maxResults, pageToken=None):
        start = int(pageToken or 0)
        chunk = self.playlists[start:start + 2]
        resp = {"items": [{"id": p["id"], "snippet": {"title": p["title"]}} for p in chunk]}
        if start + 2 < len(self.playlists):
            resp["nextPageToken"] = str(start + 2)
        return FakeRequest([resp])

    def insert(self, part, body):
        r = self.insert_results.pop(0) if self.insert_results else {"id": "PLI"}
        pid = body["snippet"]["playlistId"]
        vid = body["snippet"]["resourceId"]["videoId"]
        fake = self

        class Req:
            def execute(self, num_retries=0):
                if isinstance(r, Exception):
                    raise r
                fake.added.append((pid, vid))
                fake.members.add((pid, vid))
                return r
        return Req()


class FakeYouTube:
    def __init__(self, thumbnail_results=None, existing=None, playlists=None,
                 playlist_results=None, members=None, **kw):
        self._videos = FakeVideos(**kw)
        self._thumbs = FakeThumbnails(thumbnail_results)
        self.pl = FakePlaylists(playlists or [], playlist_results, members)
        self.channel = FakeChannel(existing or [])
        for v in existing or []:
            if not v.get("deleted"):  # deleted videos are still in the uploads list, briefly
                self._videos.files[v["id"]] = v

    def videos(self):
        return self._videos

    def thumbnails(self):
        return self._thumbs

    def channels(self):
        return type("C", (), {"list": lambda _s, **k: self.channel.channels_list(**k)})()

    def playlistItems(self):
        return type("P", (), {
            "list": lambda _s, **k: (self.pl.membership(**k) if "videoId" in k
                                     else self.channel.playlist_list(**k)),
            "insert": lambda _s, **k: self.pl.insert(**k)})()

    def playlists(self):
        return self.pl


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
    assert uploader.main(["--vids", custom, "--dry-run", "--no-channel-check", "--log", str(workdir / "log.json")]) == 0
    assert "From custom" in capsys.readouterr().out


def test_validation_blocks_whole_batch(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - filename: a.mov\n    title: ok\n"
                   "  - title: no filename\n"
                   "  - filename: missing.mov\n    title: x\n"
                   "  - filename: b.mov\n    title: 'bad <title>'\n    privacyStatus: secret\n")
    yt_fake = FakeYouTube()
    assert uploader.main(["--vids", f, "--log", str(workdir / "log.json")], youtube=yt_fake) == 2
    out = capsys.readouterr().out
    assert "missing filename" in out and "file not found" in out
    assert "can't contain < or >" in out and "privacyStatus must be" in out
    assert yt_fake.videos().inserted == []


def test_body_defaults_and_options(workdir):
    job, problems = uploader.build_job(
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
    assert uploader.main(["--vids", f, "--log", log], youtube=fake) == 1
    assert [b["snippet"]["title"] for _, b in fake.videos().inserted] == ["A", "B", "C"]
    saved = json.load(open(log))
    assert sorted(v["video_id"] for v in saved.values()) == ["IDA", "IDC"]

    # Rerun: only b is attempted.
    on_channel = [{"id": "IDA", "title": "A", "fileName": "a.mov"},
                  {"id": "IDC", "title": "C", "fileName": "c.mov"}]
    fake2 = FakeYouTube(insert_results=[{"id": "IDB"}], existing=on_channel)
    assert uploader.main(["--vids", f, "--log", log], youtube=fake2) == 0
    assert [b["snippet"]["title"] for _, b in fake2.videos().inserted] == ["B"]
    assert "DUPLICATE a.mov: skipped" in capsys.readouterr().out


def test_quota_error_stops_batch(workdir):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: A}\n  - {filename: b.mov, title: B}\n")
    fake = FakeYouTube(insert_results=[http_error(403, "quotaExceeded"), {"id": "never"}])
    assert uploader.main(["--vids", f, "--log", str(workdir / "log.json")], youtube=fake) == 1
    assert len(fake.videos().inserted) == 1


def test_video_dir_relative_to_yaml_and_tilde(workdir, monkeypatch):
    monkeypatch.setenv("HOME", str(workdir))
    f = write_yaml(workdir / "v.yml", "video_dir: '~/vids'\nvideos:\n  - {filename: a.mov, title: A}\n")
    assert uploader.main(["--vids", f, "--dry-run", "--no-channel-check", "--log", str(workdir / "l.json")]) == 0


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
    return updater.main(argv, youtube=fake), fake.videos()


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
    assert updater.main(["--vids", f], youtube=fake) == 1


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
    assert uploader.main(["--vids", f, "--dry-run", "--no-channel-check", "--log", str(thumbdir / "l.json")]) == 2
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
    assert uploader.main(["--vids", f, "--log", log], youtube=fake) == 0
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
    assert uploader.main(["--vids", f, "--log", log, "--thumbnail-wait", "0"], youtube=fake) == 1
    assert fake.thumbnails().set_calls == []
    assert json.load(open(log)).popitem()[1]["thumbnail_status"].startswith("pending")

    # Rerun: no re-upload, thumbnail gets set.
    on_channel = [{"id": "IA", "title": "A", "fileName": "a.mov"}]
    fake2 = FakeYouTube(processing={"IA": ["succeeded"]}, existing=on_channel)
    assert uploader.main(["--vids", f, "--log", log], youtube=fake2) == 0
    assert fake2.videos().inserted == []
    assert [v for v, _, _ in fake2.thumbnails().set_calls] == ["IA"]
    assert json.load(open(log)).popitem()[1]["thumbnail_status"] == "set"

    # Third run: nothing to do.
    fake3 = FakeYouTube(existing=on_channel)
    assert uploader.main(["--vids", f, "--log", log], youtube=fake3) == 0
    assert fake3.thumbnails().set_calls == [] and fake3.videos().inserted == []


def test_thumbnail_forbidden_does_not_fail_upload(thumbdir, monkeypatch, capsys):
    no_sleep(monkeypatch)
    f = write_yaml(thumbdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: A, thumbnail: a.jpg}\n")
    log = str(thumbdir / "l.json")
    fake = FakeYouTube(insert_results=[{"id": "IA"}], processing={"IA": ["succeeded"]},
                       thumbnail_results=[http_error(403, "forbidden")])
    assert uploader.main(["--vids", f, "--log", log], youtube=fake) == 1
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
    assert updater.main(["--vids", f], youtube=fake) == 0
    assert fake.videos().updated == []  # no videos.update for a thumbnail-only change
    assert fake.thumbnails().set_calls[0][:2] == ("VID", str(tmp_path / "t.png"))


def test_updater_thumbnail_waits_for_processing(tmp_path):
    (tmp_path / "t.jpg").write_bytes(b"\xff\xd8")
    f = write_yaml(tmp_path / "d.yml", "videos:\n  - url: VID\n    title: New\n    thumbnail: t.jpg\n")
    fake = FakeYouTube(current={"VID": dict(CURRENT, processingDetails={"processingStatus": "processing"})})
    assert updater.main(["--vids", f], youtube=fake) == 1
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
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 0
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
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json"),
                                "--allow-duplicate", "a.mov"], youtube=fake) == 0
    assert [b["snippet"]["title"] for _, b in fake.videos().inserted] == \
        ["A Wave from a BNSF Tanker Train 20260520", "Brand new"]
    out = capsys.readouterr().out
    assert "DUPLICATE a.mov: uploading anyway" in out and "DUPLICATE b.mov: skipped" in out


def test_force_allows_all_duplicates(workdir):
    f = dup_yaml(workdir)
    fake = FakeYouTube(existing=EXISTING, insert_results=[{"id": "1"}, {"id": "2"}, {"id": "3"}])
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json"), "--force"],
                               youtube=fake) == 0
    assert len(fake.videos().inserted) == 3


def test_allow_duplicate_typo_is_an_error(workdir, capsys):
    f = dup_yaml(workdir)
    fake = FakeYouTube(existing=EXISTING)
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json"),
                                "--allow-duplicate", "aa.mov"], youtube=fake) == 2
    assert "--allow-duplicate aa.mov: not in" in capsys.readouterr().out
    assert fake.videos().inserted == []


def test_same_file_listed_twice_is_an_error(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: One}\n  - {filename: a.mov, title: Two}\n")
    assert uploader.main(["--vids", f, "--no-channel-check", "--dry-run",
                                "--log", str(workdir / "l.json")]) == 2
    assert "listed twice" in capsys.readouterr().out


def test_dry_run_reports_channel_duplicates(workdir, capsys):
    f = dup_yaml(workdir)
    fake = FakeYouTube(existing=EXISTING)
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json"), "--dry-run"],
                               youtube=fake) == 0
    out = capsys.readouterr().out
    assert "OK   c.mov" in out and "1 to upload, 2 duplicate(s) skipped" in out
    assert fake.videos().inserted == []


def test_log_and_channel_match_reported_once(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    log = str(workdir / "l.json")
    uploader.main(["--vids", f, "--log", log, "--no-channel-check"],
                        youtube=FakeYouTube(insert_results=[{"id": "IDA"}]))
    capsys.readouterr()
    fake = FakeYouTube(existing=[{"id": "IDA", "title": "A", "fileName": "a.mov"}])
    uploader.main(["--vids", f, "--log", log], youtube=fake)
    out = capsys.readouterr().out
    assert "already uploaded by this tool as https://youtu.be/IDA" in out
    assert "same file name" not in out and "same title" not in out


def test_reupload_keeps_previous_log_entry(workdir):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    log = str(workdir / "l.json")
    uploader.main(["--vids", f, "--log", log, "--no-channel-check"],
                        youtube=FakeYouTube(insert_results=[{"id": "FIRST"}]))
    uploader.main(["--vids", f, "--log", log, "--no-channel-check", "--allow-duplicate", "a.mov"],
                        youtube=FakeYouTube(insert_results=[{"id": "SECOND"}]))
    entry = json.load(open(log)).popitem()[1]
    assert entry["video_id"] == "SECOND"
    assert [p["video_id"] for p in entry["previous_uploads"]] == ["FIRST"]


def test_channel_read_error_uploads_nothing(workdir, capsys):
    f = dup_yaml(workdir)
    fake = FakeYouTube()
    fake.channel.channels_list = lambda part, mine: FakeRequest([http_error(403, "forbidden")])
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 1
    assert fake.videos().inserted == []
    assert "--no-channel-check" in capsys.readouterr().out


def test_deleted_video_in_log_is_not_a_duplicate(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    log = str(workdir / "l.json")
    uploader.main(["--vids", f, "--log", log, "--no-channel-check"],
                        youtube=FakeYouTube(insert_results=[{"id": "GONE"}]))
    capsys.readouterr()
    # The video was deleted on YouTube; the channel no longer has it.
    fake = FakeYouTube(existing=[{"id": "OTHER", "title": "x", "fileName": "x.mov"}],
                       insert_results=[{"id": "NEW"}])
    assert uploader.main(["--vids", f, "--log", log], youtube=fake) == 0
    out = capsys.readouterr().out
    assert "no longer on the channel" in out and "DUPLICATE" not in out
    entry = json.load(open(log)).popitem()[1]
    assert entry["video_id"] == "NEW" and entry["previous_uploads"][0]["video_id"] == "GONE"


def test_deleted_video_still_in_uploads_list_is_ignored(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    fake = FakeYouTube(existing=[{"id": "GONE", "title": "A", "fileName": "a.mov", "deleted": True}],
                       insert_results=[{"id": "NEW"}])
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 0
    out = capsys.readouterr().out
    assert "DUPLICATE" not in out and "0 videos on the channel" in out


def test_log_hit_without_channel_check_still_skips(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    log = str(workdir / "l.json")
    uploader.main(["--vids", f, "--log", log, "--no-channel-check"],
                        youtube=FakeYouTube(insert_results=[{"id": "IDA"}]))
    capsys.readouterr()
    fake = FakeYouTube()
    uploader.main(["--vids", f, "--log", log, "--no-channel-check"], youtube=fake)
    assert "DUPLICATE a.mov: skipped" in capsys.readouterr().out and fake.videos().inserted == []


# ---------------------------------------------------------------- examples stay complete

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
UPLOADER_KEYS = {"filename", "title", "description", "keywords", "categoryId", "privacyStatus",
                 "publishAt", "recordingDate", "madeForKids", "language", "thumbnail",
                 "skipSchedule", "playlists"}
UPDATER_KEYS = {"url", "title", "description", "keywords", "categoryId", "language",
                "privacyStatus", "publishAt", "recordingDate", "madeForKids", "thumbnail",
                "playlists"}


def _keys_in_example(path):
    import re
    text = open(path).read()
    return set(re.findall(r"^\s+(?:- |# )?([A-Za-z]+):", text, re.M))


def test_uploader_example_shows_every_field_and_is_valid():
    path = os.path.join(EXAMPLES, "video_summary.example.yml")
    assert UPLOADER_KEYS <= _keys_in_example(path)
    data = yt.load_yaml(path)
    defaults, problems = uploader.check_defaults(data.get("defaults"))
    assert problems == [] and defaults
    for vid in data["videos"]:
        _, problems = uploader.build_job(uploader.apply_defaults(vid, defaults), "/nonexistent")
        problems = [p for p in problems if "not found" not in p and "in the past" not in p]
        assert problems == []


def test_updater_example_shows_every_field_and_is_valid():
    path = os.path.join(EXAMPLES, "draft_videos.example.yml")
    assert UPDATER_KEYS <= _keys_in_example(path)
    data = yt.load_yaml(path)
    for vid in data["videos"]:
        _, _, problems = updater.parse_changes(vid, "/nonexistent")
        problems = [p for p in problems if "not found" not in p and "in the past" not in p]
        assert problems == []


def test_code_reads_no_undocumented_keys():
    """Every vid.get(...)/vid[...] key in the scripts must be in the key lists above."""
    import re
    for script, keys in (("uploader.py", UPLOADER_KEYS), ("updater.py", UPDATER_KEYS)):
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
    uploader.main(["--vids", f, "--dry-run", "--no-channel-check", "--log", str(workdir / "l.json")])
    out = capsys.readouterr().out
    assert "PUBLIC immediately" in out and "goes PUBLIC" in out
    assert "(in 1 hr)" in out or "(in 59 min)" in out


def test_go_live_needs_confirmation_when_not_a_terminal(workdir, capsys):
    f = golive_yaml(workdir)
    fake = FakeYouTube(insert_results=[{"id": "1"}, {"id": "2"}, {"id": "3"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--log", str(workdir / "l.json")],
                               youtube=fake) == 3
    out = capsys.readouterr().out
    assert "These will be visible to others" in out and "Rerun with --yes" in out
    assert "c.mov" not in out.split("These will be visible")[1]  # private one isn't listed
    assert fake.videos().inserted == []


def test_go_live_with_yes_uploads(workdir, capsys):
    f = golive_yaml(workdir)
    fake = FakeYouTube(insert_results=[{"id": "1", "status": {"privacyStatus": "public"}},
                                       {"id": "2"}, {"id": "3"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--yes",
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
    assert uploader.main(["--vids", f, "--no-channel-check", "--log", str(workdir / "l.json")],
                               youtube=fake) == code
    assert len(fake.videos().inserted) == uploads


def test_private_only_batch_never_prompts(workdir):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    fake = FakeYouTube(insert_results=[{"id": "1"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--log", str(workdir / "l.json")],
                               youtube=fake) == 0


def test_schedule_too_close_by_upload_time_is_not_uploaded(workdir, monkeypatch, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   f"  - {{filename: a.mov, title: A, publishAt: '{in_minutes(60)}'}}\n")
    monkeypatch.setattr(yt, "seconds_until", lambda v: 120)  # time passed during earlier uploads
    fake = FakeYouTube(insert_results=[{"id": "1"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--yes",
                                "--log", str(workdir / "l.json")], youtube=fake) == 1
    assert fake.videos().inserted == [] and "too soon to upload" in capsys.readouterr().out


def test_min_lead_flag(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   f"  - {{filename: a.mov, title: A, publishAt: '{in_minutes(10)}'}}\n")
    args = ["--vids", f, "--dry-run", "--no-channel-check", "--log", str(workdir / "l.json")]
    assert uploader.main(args) == 2
    assert uploader.main(args + ["--min-lead", "5"]) == 0


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


# ---------------------------------------------------------------- moving a too-close schedule

def soon_yaml(workdir, minutes=5):
    return write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                      f"  - {{filename: a.mov, title: A, publishAt: '{in_minutes(minutes)}'}}\n")


def test_earliest_publish_time_is_past_min_lead():
    t = yt.seconds_until(yt.earliest_publish_time(15))
    assert 15 * 60 < t <= 17 * 60
    dt = datetime.datetime.fromisoformat(yt.earliest_publish_time(15))
    assert dt.second == 0 and dt.microsecond == 0 and dt.tzinfo is not None


def test_fix_schedule_moves_it(workdir, capsys):
    f = soon_yaml(workdir)
    fake = FakeYouTube(insert_results=[{"id": "1"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--yes", "--fix-schedule",
                                "--log", str(workdir / "l.json")], youtube=fake) == 0
    sent = fake.videos().inserted[0][1]["status"]["publishAt"]
    assert 15 * 60 < yt.seconds_until(sent) <= 17 * 60
    assert "Moved to" in capsys.readouterr().out


def test_too_soon_without_terminal_explains_and_uploads_nothing(workdir, capsys):
    f = soon_yaml(workdir)
    fake = FakeYouTube(insert_results=[{"id": "1"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--yes",
                                "--log", str(workdir / "l.json")], youtube=fake) == 2
    assert "--fix-schedule" in capsys.readouterr().out and fake.videos().inserted == []


@pytest.mark.parametrize("answer,code,uploads", [("y", 0, 1), ("n", 2, 0)])
def test_too_soon_prompt(workdir, monkeypatch, answer, code, uploads):
    f = soon_yaml(workdir)
    monkeypatch.setattr(yt.sys, "stdin", FakeTTY())
    answers = iter([answer, "y"])  # 2nd answer is the go-live confirmation
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    fake = FakeYouTube(insert_results=[{"id": "1"}])
    assert uploader.main(["--vids", f, "--no-channel-check",
                                "--log", str(workdir / "l.json")], youtube=fake) == code
    assert len(fake.videos().inserted) == uploads


def test_past_time_is_still_an_error_even_with_fix_schedule(workdir, capsys):
    f = soon_yaml(workdir, minutes=-30)
    assert uploader.main(["--vids", f, "--no-channel-check", "--dry-run", "--fix-schedule",
                                "--log", str(workdir / "l.json")]) == 2
    assert "in the past" in capsys.readouterr().out


def test_upload_time_recheck_moves_when_allowed(workdir, monkeypatch, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   f"  - {{filename: a.mov, title: A, publishAt: '{in_minutes(60)}'}}\n")
    real = yt.seconds_until
    calls = iter([120])  # first check at upload time says only 2 minutes left
    monkeypatch.setattr(yt, "seconds_until", lambda v: next(calls, None) or real(v))
    fake = FakeYouTube(insert_results=[{"id": "1"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--yes", "--fix-schedule",
                                "--log", str(workdir / "l.json")], youtube=fake) == 0
    assert "moved to" in capsys.readouterr().out
    assert yt.seconds_until(fake.videos().inserted[0][1]["status"]["publishAt"]) > 15 * 60


def test_updater_fix_schedule(tmp_path, capsys):
    draft = dict(CURRENT, status=dict(CURRENT["status"], privacyStatus="private"))
    rc, v = run_updater(tmp_path, f"videos:\n  - url: VID\n    publishAt: '{in_minutes(3)}'\n",
                        current=draft)
    assert rc == 2 and v.updated == []
    f = write_yaml(tmp_path / "d.yml", f"videos:\n  - url: VID\n    publishAt: '{in_minutes(3)}'\n")
    fake = FakeYouTube(current={"VID": draft})
    assert updater.main(["--vids", f, "--yes", "--fix-schedule"], youtube=fake) == 0
    assert yt.seconds_until(fake.videos().updated[0][1]["status"]["publishAt"]) > 15 * 60


# ---------------------------------------------------------------- finding files in dated folders

@pytest.fixture
def finals(tmp_path):
    """Finals/2025/..., Finals/2026/... like the Dropbox layout."""
    root = tmp_path / "Finals"
    files = {
        "2025/2025-03-01/NS Arr 20250301.mov": b"v",
        "2026/2026-05-20/BNSF 674 yard job 20260520.mov": b"v",
        "2026/2026-05-20/BNSF 674 yard job 20260520.jpg": b"\xff\xd8",
        "2026/2026-06-01/MARTA CQ400 20260601.mov": b"v",
        "2026/2026-06-01/thumb.jpg": b"\xff\xd8",
        "2026/2026-06-02/thumb.jpg": b"\xff\xd8",
        "2026/2026-06-02/same name.mov": b"v",
        "2025/2025-06-02/same name.mov": b"v",
        ".dropbox.cache/2026/MARTA CQ400 20260601.mov": b"v",   # hidden: ignored
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def test_finder_bare_name_found_in_dated_folder(finals):
    path, problem = yt.FileFinder(str(finals)).find("BNSF 674 yard job 20260520.mov")
    assert problem is None and path.endswith("2026/2026-05-20/BNSF 674 yard job 20260520.mov")


def test_finder_ignores_hidden_folders(finals):
    path, problem = yt.FileFinder(str(finals)).find("MARTA CQ400 20260601.mov")
    assert problem is None and ".dropbox.cache" not in path


def test_finder_is_case_insensitive(finals):
    path, problem = yt.FileFinder(str(finals)).find("ns arr 20250301.MOV")
    assert problem is None and path.endswith("NS Arr 20250301.mov")


def test_finder_ambiguous_lists_matches(finals):
    _, problem = yt.FileFinder(str(finals)).find("same name.mov")
    assert "2 files named same name.mov" in problem
    assert "2025/2025-06-02/same name.mov" in problem and "2026/2026-06-02/same name.mov" in problem


def test_finder_subfolder_path_picks_one(finals):
    path, problem = yt.FileFinder(str(finals)).find("2026/2026-06-02/same name.mov")
    assert problem is None and path == str(finals / "2026/2026-06-02/same name.mov")


def test_finder_not_found_message(finals):
    _, problem = yt.FileFinder(str(finals)).find("nope.mov")
    assert problem.startswith("file not found: nope.mov") and "every folder under it" in problem


def test_finder_does_not_walk_when_file_is_direct(tmp_path, monkeypatch):
    (tmp_path / "a.mov").write_bytes(b"v")
    walked = []
    monkeypatch.setattr(yt.os, "walk", lambda *a, **k: walked.append(1) or iter(()))
    path, problem = yt.FileFinder(str(tmp_path)).find("a.mov")
    assert problem is None and walked == []


def test_finder_walks_once_per_run(finals, monkeypatch):
    real_walk, walks = os.walk, []
    monkeypatch.setattr(yt.os, "walk", lambda *a, **k: walks.append(1) or real_walk(*a, **k))
    f = yt.FileFinder(str(finals))
    f.find("BNSF 674 yard job 20260520.mov")
    f.find("MARTA CQ400 20260601.mov")
    assert len(walks) == 1


def test_uploader_finds_video_and_thumbnail_in_dated_folders(finals, tmp_path, capsys):
    y = write_yaml(tmp_path / "v.yml", f"video_dir: '{finals}'\nvideos:\n"
                   "  - {filename: BNSF 674 yard job 20260520.mov, title: BNSF 674,"
                   " thumbnail: BNSF 674 yard job 20260520.jpg}\n"
                   "  - {filename: MARTA CQ400 20260601.mov, title: MARTA, thumbnail: thumb.jpg}\n")
    assert uploader.main(["--vids", y, "--dry-run", "--no-channel-check",
                                "--log", str(tmp_path / "l.json")]) == 0
    jobs = [uploader.build_job(v, str(finals))[0] for v in yt.load_yaml(y)["videos"]]
    assert jobs[0]["path"].endswith("2026-05-20/BNSF 674 yard job 20260520.mov")
    # thumb.jpg exists in two folders; the one next to the video wins
    assert jobs[1]["thumbnail"] == str(finals / "2026/2026-06-01/thumb.jpg")


def test_uploader_ambiguous_name_blocks_batch(finals, tmp_path, capsys):
    y = write_yaml(tmp_path / "v.yml", f"video_dir: '{finals}'\nvideos:\n"
                   "  - {filename: same name.mov, title: X}\n")
    assert uploader.main(["--vids", y, "--dry-run", "--no-channel-check",
                                "--log", str(tmp_path / "l.json")]) == 2
    assert "put the folder in filename" in capsys.readouterr().out


# ---------------------------------------------------------------- scheduling one Short per day

def day_at(days_from_today, hhmm="13:00"):
    d = datetime.date.today() + datetime.timedelta(days=days_from_today)
    t = datetime.datetime.strptime(hhmm, "%H:%M").time()
    return datetime.datetime.combine(d, t).astimezone()


def test_parse_schedule():
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    assert uploader.parse_schedule({"time": "13:00"})[0] == \
        {"time": datetime.time(13, 0), "start": tomorrow, "skip_dates": set(), "skip_weekdays": set()}
    # unquoted 13:00 in YAML arrives as 780
    import yaml
    cfg = yaml.safe_load("time: 13:00\nstart: 2027-01-05\n")
    sched, problems = uploader.parse_schedule(cfg)
    assert problems == [] and sched["time"] == datetime.time(13, 0)
    assert sched["start"] == datetime.date(2027, 1, 5)
    assert uploader.parse_schedule({"time": "1pm"})[1]
    assert uploader.parse_schedule({"time": "13:00", "every": 2})[1]
    assert uploader.parse_schedule("13:00")[1]
    assert uploader.parse_schedule(None) == (None, [])


def make_jobs(tmp_path, entries):
    (tmp_path / "vids").mkdir(exist_ok=True)
    jobs = []
    for e in entries:
        (tmp_path / "vids" / e["filename"]).write_bytes(b"v")
        job, problems = uploader.build_job(e, str(tmp_path / "vids"), min_lead=0)
        assert problems == []
        jobs.append(job)
    return jobs


def test_assign_slots_skips_days_with_scheduled_shorts(tmp_path):
    channel = [
        {"id": "S2", "title": "Short on day 2", "isShort": True, "publishAt": day_at(2).isoformat()},
        {"id": "L3", "title": "Long on day 3", "isShort": False, "publishAt": day_at(3).isoformat()},
        {"id": "OLD", "title": "Published", "isShort": True, "publishAt": None},
    ]
    jobs = make_jobs(tmp_path, [{"filename": f"{n}.mov", "title": n} for n in "abc"])
    sched = {"time": datetime.time(13, 0), "start": datetime.date.today() + datetime.timedelta(days=1)}
    plan = uploader.assign_slots(jobs, sched, channel, min_lead=15)
    got = [yt.local_date(j["body"]["status"]["publishAt"]) for j in jobs]
    assert got == [day_at(1).date(), day_at(3).date(), day_at(4).date()]  # day 2 taken; long-form doesn't block
    assert all(j["body"]["status"]["privacyStatus"] == "private" for j in jobs)
    assert [(d, j is not None, t) for d, j, t in plan] == [
        (day_at(1).date(), True, None), (day_at(2).date(), False, "Short on day 2"),
        (day_at(3).date(), True, None), (day_at(4).date(), True, None)]


def test_assign_slots_respects_own_publish_at_and_skip(tmp_path):
    jobs = make_jobs(tmp_path, [
        {"filename": "a.mov", "title": "A"},
        {"filename": "b.mov", "title": "B", "publishAt": day_at(2).strftime("%Y-%m-%d %H:%M")},
        {"filename": "c.mov", "title": "C", "skipSchedule": True},
        {"filename": "d.mov", "title": "D"},
    ])
    sched = {"time": datetime.time(9, 30), "start": datetime.date.today() + datetime.timedelta(days=1)}
    uploader.assign_slots(jobs, sched, [], min_lead=15)
    a, b, c, d = jobs
    assert yt.local_date(a["body"]["status"]["publishAt"]) == day_at(1).date()
    assert yt.local_date(b["body"]["status"]["publishAt"]) == day_at(2).date()   # its own time, kept
    assert "publishAt" not in c["body"]["status"]                                 # skipped
    assert yt.local_date(d["body"]["status"]["publishAt"]) == day_at(3).date()   # day 2 was B's
    assert _as_time(a) == datetime.time(9, 30)


def _as_time(job):
    return datetime.datetime.fromisoformat(job["body"]["status"]["publishAt"]).time()


def test_assign_slots_today_too_late_moves_to_tomorrow(tmp_path):
    jobs = make_jobs(tmp_path, [{"filename": "a.mov", "title": "A"}])
    past = (datetime.datetime.now() - datetime.timedelta(hours=1)).time().replace(second=0, microsecond=0)
    sched = {"time": past, "start": datetime.date.today()}
    uploader.assign_slots(jobs, sched, [], min_lead=15)
    assert yt.local_date(jobs[0]["body"]["status"]["publishAt"]) == day_at(1).date()


def sched_yaml(workdir, extra=""):
    return write_yaml(workdir / "v.yml", "video_dir: vids\nschedule:\n  time: 13:00\n" + extra +
                      "videos:\n  - {filename: a.mov, title: A}\n  - {filename: b.mov, title: B}\n")


def test_schedule_dry_run_shows_plan(workdir, capsys):
    f = sched_yaml(workdir)
    existing = [{"id": "S1", "title": "Already Tomorrow", "fileName": "x.mov", "duration": "PT40S",
                 "privacyStatus": "private", "publishAt": day_at(1).isoformat(), "size": (1080, 1920)}]
    fake = FakeYouTube(existing=existing)
    assert uploader.main(["--vids", f, "--dry-run", "--log", str(workdir / "l.json")],
                               youtube=fake) == 0
    out = capsys.readouterr().out
    assert "Scheduling 2 videos, one per open day at 1:00 PM" in out
    assert '(taken: "Already Tomorrow")' in out
    assert out.count("goes PUBLIC") == 2 and fake.videos().inserted == []


def test_schedule_upload_sends_assigned_times(workdir):
    f = sched_yaml(workdir)
    existing = [{"id": "L1", "title": "Long", "fileName": "x.mov", "duration": "PT12M",
                 "privacyStatus": "private", "publishAt": day_at(1).isoformat(), "size": (1920, 1080)}]
    fake = FakeYouTube(existing=existing, insert_results=[{"id": "1"}, {"id": "2"}])
    assert uploader.main(["--vids", f, "--yes", "--log", str(workdir / "l.json")],
                               youtube=fake) == 0
    sent = [yt.local_date(b["status"]["publishAt"]) for _, b in fake.videos().inserted]
    assert sent == [day_at(1).date(), day_at(2).date()]  # long-form on day 1 doesn't block it


def test_schedule_needs_channel_check(workdir, capsys):
    f = sched_yaml(workdir)
    assert uploader.main(["--vids", f, "--dry-run", "--no-channel-check",
                                "--log", str(workdir / "l.json")]) == 2
    assert "needs the channel check" in capsys.readouterr().out


def test_schedule_skips_duplicates_when_assigning(workdir, capsys):
    f = sched_yaml(workdir)
    existing = [{"id": "D", "title": "A", "fileName": "a.mov", "duration": "PT30S"}]  # a.mov already up
    fake = FakeYouTube(existing=existing, insert_results=[{"id": "2"}])
    assert uploader.main(["--vids", f, "--yes", "--log", str(workdir / "l.json")],
                               youtube=fake) == 0
    sent = [(b["snippet"]["title"], yt.local_date(b["status"]["publishAt"])) for _, b in fake.videos().inserted]
    assert sent == [("B", day_at(1).date())]  # B gets tomorrow, not the duplicate


@pytest.mark.parametrize("duration,size,short", [
    ("PT45S", (1080, 1920), True), ("PT2M59S", None, True), ("PT3M1S", (1080, 1920), False),
    ("PT30S", (1920, 1080), False), ("PT1H2M3S", None, False), ("P0D", None, True), ("", None, False)])
def test_looks_like_short(duration, size, short):
    item = {"contentDetails": {"duration": duration}, "fileDetails": {}}
    if size:
        item["fileDetails"]["videoStreams"] = [{"widthPixels": size[0], "heightPixels": size[1]}]
    assert yt.looks_like_short(item) is short


def test_looks_like_short_handles_rotation():
    item = {"contentDetails": {"duration": "PT20S"},
            "fileDetails": {"videoStreams": [{"widthPixels": 1920, "heightPixels": 1080,
                                              "rotation": "clockwise"}]}}
    assert yt.looks_like_short(item) is True


# ---------------------------------------------------------------- upcoming.py

import upcoming  # noqa: E402


def test_upcoming_render():
    today = datetime.date.today()
    channel = [
        {"id": "S1", "title": "Short tomorrow", "isShort": True, "publishAt": day_at(1).isoformat()},
        {"id": "L1", "title": "Long day 2", "isShort": False, "publishAt": day_at(2, "18:00").isoformat()},
        {"id": "S3", "title": "Short day 3", "isShort": True, "publishAt": day_at(3, "09:05").isoformat()},
        {"id": "S3b", "title": "Second short day 3", "isShort": True, "publishAt": day_at(3).isoformat()},
        {"id": "S20", "title": "Far away", "isShort": True, "publishAt": day_at(20).isoformat()},
        {"id": "P", "title": "Published", "isShort": True, "publishAt": None},
    ]
    lines = upcoming.render(channel, days=5, today=today)
    text = "\n".join(lines)
    assert lines[1].endswith("open")                                       # today
    assert '1:00 PM  Short  scheduled  "Short tomorrow"' in lines[2]
    assert '6:00 PM  Video  scheduled  "Long day 2"' in lines[3] and lines[3].endswith("(no Short: open)")
    assert '9:05 AM  Short  scheduled  "Short day 3"' in lines[4]            # sorted by time
    assert '"Second short day 3"' in lines[5] and not lines[5].strip().startswith(upcoming._day(today))
    assert lines[6].endswith("open")                                       # day 4
    assert "3 Shorts scheduled, 3 open days. 1 more scheduled after" in text
    assert "Published" not in text


def test_upcoming_main_with_fake_channel(capsys):
    existing = [{"id": "S1", "title": "Tomorrow", "fileName": "x.mov", "duration": "PT30S",
                 "privacyStatus": "private", "publishAt": day_at(1).isoformat(), "size": (1080, 1920)}]
    assert upcoming.main(["--days", "3", "--vids", "/nonexistent.yml"],
                         youtube=FakeYouTube(existing=existing)) == 0
    out = capsys.readouterr().out
    assert '"Tomorrow"' in out and "1 Short scheduled, 2 open days." in out


# ---------------------------------------------------------------- stale entries don't block a batch

def test_uploaded_entry_with_past_time_is_skipped_not_an_error(workdir, capsys):
    """The A6 case: an entry uploaded this morning, its publishAt now passed, left in the YAML."""
    log = str(workdir / "l.json")
    first = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                       f"  - {{filename: a.mov, title: A6, publishAt: '{in_minutes(60)}'}}\n")

    uploader.main(["--vids", first, "--yes", "--log", log], youtube=FakeYouTube(insert_results=[{"id": "A6"}]))
    capsys.readouterr()
    later = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                       f"  - {{filename: a.mov, title: A6, publishAt: '{in_minutes(-120)}'}}\n"
                       "  - {filename: b.mov, title: New one}\n")
    on_channel = [{"id": "A6", "title": "A6", "fileName": "a.mov"}]
    fake = FakeYouTube(existing=on_channel, insert_results=[{"id": "B"}])
    assert uploader.main(["--vids", later, "--log", log], youtube=fake) == 0
    out = capsys.readouterr().out
    assert "DUPLICATE a.mov: skipped" in out and "in the past" not in out
    assert [b["snippet"]["title"] for _, b in fake.videos().inserted] == ["New one"]


def test_channel_duplicate_with_past_time_is_skipped(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   f"  - {{filename: a.mov, title: A, publishAt: '{in_minutes(-30)}'}}\n")
    fake = FakeYouTube(existing=[{"id": "X", "title": "A", "fileName": "a.mov"}])
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 0
    assert "in the past" not in capsys.readouterr().out


def test_new_video_with_past_time_is_still_an_error(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   f"  - {{filename: a.mov, title: A, publishAt: '{in_minutes(-30)}'}}\n")
    fake = FakeYouTube(insert_results=[{"id": "1"}])
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 2
    out = capsys.readouterr().out
    assert "in the past" in out and "1 video needs a new publish time" in out
    assert fake.videos().inserted == []


def test_duplicate_with_too_soon_time_is_not_offered_a_move(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   f"  - {{filename: a.mov, title: A, publishAt: '{in_minutes(5)}'}}\n")
    fake = FakeYouTube(existing=[{"id": "X", "title": "A", "fileName": "a.mov"}])
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 0
    assert "less than 15 minutes" not in capsys.readouterr().out


def test_allowed_duplicate_with_past_time_is_an_error(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   f"  - {{filename: a.mov, title: A, publishAt: '{in_minutes(-30)}'}}\n")
    fake = FakeYouTube(existing=[{"id": "X", "title": "A", "fileName": "a.mov"}])
    assert uploader.main(["--vids", f, "--allow-duplicate", "a.mov",
                          "--log", str(workdir / "l.json")], youtube=fake) == 2
    assert "in the past" in capsys.readouterr().out and fake.videos().inserted == []


# ---------------------------------------------------------------- defaults for every video

def test_apply_defaults_override_and_tag_merge():
    defaults = {"keywords": "trains, railfan, Galesburg", "categoryId": "19",
                "description": "Default description"}
    got = uploader.apply_defaults({"filename": "a.mov", "title": "A", "keywords": "BNSF, railfan",
                                   "description": "Own"}, defaults)
    assert got["keywords"] == ["trains", "railfan", "Galesburg", "BNSF"]   # merged, no dupes
    assert got["description"] == "Own" and got["categoryId"] == "19"
    plain = uploader.apply_defaults({"filename": "b.mov", "title": "B"}, defaults)
    assert plain["keywords"] == ["trains", "railfan", "Galesburg"]
    assert plain["description"] == "Default description"


def test_defaults_in_uploads(workdir):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\n"
                   "defaults:\n"
                   "  keywords: trains, railfan\n"
                   "  categoryId: '19'\n"
                   "  description: |\n    Shot in Galesburg.\n\n    #railfan\n"
                   "videos:\n"
                   "  - {filename: a.mov, title: A}\n"
                   "  - {filename: b.mov, title: B, keywords: BNSF, categoryId: '2'}\n")
    fake = FakeYouTube(insert_results=[{"id": "1"}, {"id": "2"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--log", str(workdir / "l.json")],
                         youtube=fake) == 0
    (_, a), (_, b) = fake.videos().inserted
    assert a["snippet"]["tags"] == ["trains", "railfan"] and a["snippet"]["categoryId"] == "19"
    assert a["snippet"]["description"] == "Shot in Galesburg.\n\n#railfan"
    assert b["snippet"]["tags"] == ["trains", "railfan", "BNSF"] and b["snippet"]["categoryId"] == "2"


def test_defaults_reject_per_video_fields(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\ndefaults:\n  title: Nope\n  thumbnail: t.jpg\n"
                   "videos:\n  - {filename: a.mov, title: A}\n")
    assert uploader.main(["--vids", f, "--dry-run", "--no-channel-check",
                          "--log", str(workdir / "l.json")]) == 2
    assert "defaults: can't set thumbnail, title" in capsys.readouterr().out


def test_unknown_top_level_key_is_an_error(workdir, capsys):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\ndefault:\n  keywords: x\n"
                   "videos:\n  - {filename: a.mov, title: A}\n")
    assert uploader.main(["--vids", f, "--dry-run", "--no-channel-check",
                          "--log", str(workdir / "l.json")]) == 2
    out = capsys.readouterr().out
    assert "unknown setting(s) at the top" in out and ": default (expected" in out


def test_default_tags_count_toward_500_limit(workdir, capsys):
    long_tags = ", ".join(f"tag{i:03d}word" for i in range(50))  # 50 * 10 + 49 commas = 549
    f = write_yaml(workdir / "v.yml", f"video_dir: vids\ndefaults:\n  keywords: '{long_tags}'\n"
                   "videos:\n  - {filename: a.mov, title: A, keywords: extra}\n")
    assert uploader.main(["--vids", f, "--dry-run", "--no-channel-check",
                          "--log", str(workdir / "l.json")]) == 2
    assert "tags total" in capsys.readouterr().out


# ---------------------------------------------------------------- playlists

PLAYLISTS = [{"id": "PL_SHORTS", "title": "Railfan Shorts"}, {"id": "PL_BNSF", "title": "BNSF"},
             {"id": "PL_GBB", "title": "Galesburg, Illinois"}]


def pl_yaml(workdir, body):
    return write_yaml(workdir / "v.yml", "video_dir: vids\n" + body)


def test_playlists_from_defaults_and_video(workdir, capsys):
    f = pl_yaml(workdir, "defaults:\n  playlists: [Railfan Shorts]\n"
                "videos:\n"
                "  - {filename: a.mov, title: A, playlists: [bnsf, 'Galesburg, Illinois']}\n"
                "  - {filename: b.mov, title: B, playlists: PL_BNSF}\n"
                "  - {filename: c.mov, title: C, playlists: [railfan shorts]}\n")
    log = str(workdir / "l.json")
    fake = FakeYouTube(playlists=PLAYLISTS, insert_results=[{"id": "VA"}, {"id": "VB"}, {"id": "VC"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--log", log], youtube=fake) == 0
    assert fake.pl.added == [("PL_SHORTS", "VA"), ("PL_BNSF", "VA"), ("PL_GBB", "VA"),
                             ("PL_SHORTS", "VB"), ("PL_BNSF", "VB"),
                             ("PL_SHORTS", "VC")]   # c's own entry repeats the default: added once
    saved = json.load(open(log))
    entry_a = next(e for e in saved.values() if e["video_id"] == "VA")
    assert {k: v["status"] for k, v in entry_a["playlists"].items()} == \
        {"PL_SHORTS": "added", "PL_BNSF": "added", "PL_GBB": "added"}
    assert 'Added to playlist "Galesburg, Illinois"' in capsys.readouterr().out


def test_unknown_playlist_stops_before_uploading(workdir, capsys):
    f = pl_yaml(workdir, "videos:\n  - {filename: a.mov, title: A, playlists: [BSNF]}\n")
    fake = FakeYouTube(playlists=PLAYLISTS, insert_results=[{"id": "1"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--log", str(workdir / "l.json")],
                         youtube=fake) == 2
    out = capsys.readouterr().out
    assert 'no playlist named "BSNF"' in out and '"BNSF"' in out and "Nothing was uploaded" in out
    assert fake.videos().inserted == []


def test_ambiguous_playlist_name(workdir, capsys):
    twins = PLAYLISTS + [{"id": "PL_BNSF2", "title": "BNSF"}]
    f = pl_yaml(workdir, "videos:\n  - {filename: a.mov, title: A, playlists: [BNSF]}\n")
    fake = FakeYouTube(playlists=twins)
    assert uploader.main(["--vids", f, "--no-channel-check", "--dry-run",
                          "--log", str(workdir / "l.json")], youtube=fake) == 2
    assert '2 playlists are named "BNSF" (PL_BNSF, PL_BNSF2); use the ID' in capsys.readouterr().out


def test_dry_run_lists_playlists(workdir, capsys):
    f = pl_yaml(workdir, "videos:\n  - {filename: a.mov, title: A, playlists: [BNSF]}\n")
    fake = FakeYouTube(playlists=PLAYLISTS)
    assert uploader.main(["--vids", f, "--no-channel-check", "--dry-run",
                          "--log", str(workdir / "l.json")], youtube=fake) == 0
    assert 'playlists: "BNSF"' in capsys.readouterr().out and fake.pl.added == []


def test_failed_playlist_add_is_retried_once_without_reupload(workdir, capsys):
    f = pl_yaml(workdir, "videos:\n  - {filename: a.mov, title: A, playlists: [BNSF, Railfan Shorts]}\n")
    log = str(workdir / "l.json")
    fake = FakeYouTube(playlists=PLAYLISTS, insert_results=[{"id": "VA"}],
                       playlist_results=[http_error(500, "backendError"), {"id": "ok"}])
    assert uploader.main(["--vids", f, "--no-channel-check", "--log", log], youtube=fake) == 1
    assert fake.pl.added == [("PL_SHORTS", "VA")]
    assert "1 playlist add(s) failed" in capsys.readouterr().out

    # Rerun: no upload; only the missing playlist is added (no double add to Railfan Shorts).
    on_channel = [{"id": "VA", "title": "A", "fileName": "a.mov"}]
    fake2 = FakeYouTube(playlists=PLAYLISTS, existing=on_channel)
    assert uploader.main(["--vids", f, "--log", log], youtube=fake2) == 0
    assert fake2.videos().inserted == [] and fake2.pl.added == [("PL_BNSF", "VA")]
    assert 'PLAYLISTS a.mov: not yet in "BNSF"' in capsys.readouterr().out

    # Third run: nothing left to do.
    fake3 = FakeYouTube(playlists=PLAYLISTS, existing=on_channel)
    assert uploader.main(["--vids", f, "--log", log], youtube=fake3) == 0
    assert fake3.pl.added == []


def test_playlist_added_later_to_uploaded_entry(workdir):
    log = str(workdir / "l.json")
    f = pl_yaml(workdir, "videos:\n  - {filename: a.mov, title: A}\n")
    uploader.main(["--vids", f, "--no-channel-check", "--log", log],
                  youtube=FakeYouTube(insert_results=[{"id": "VA"}]))
    f = pl_yaml(workdir, "videos:\n  - filename: a.mov\n    title: A\n    playlists:\n      - Galesburg, Illinois\n")
    fake = FakeYouTube(playlists=PLAYLISTS, existing=[{"id": "VA", "title": "A", "fileName": "a.mov"}])
    assert uploader.main(["--vids", f, "--log", log], youtube=fake) == 0
    assert fake.pl.added == [("PL_GBB", "VA")] and fake.videos().inserted == []


def test_playlists_need_no_lookup_when_unused(workdir):
    f = pl_yaml(workdir, "videos:\n  - {filename: a.mov, title: A}\n")
    fake = FakeYouTube(insert_results=[{"id": "1"}])
    fake.pl.list = lambda **k: (_ for _ in ()).throw(AssertionError("playlists were read"))
    assert uploader.main(["--vids", f, "--no-channel-check", "--log", str(workdir / "l.json")],
                         youtube=fake) == 0


def test_as_name_list_does_not_split_commas():
    assert yt.as_name_list("Galesburg, Illinois") == ["Galesburg, Illinois"]
    assert yt.as_name_list(["A", " B ", ""]) == ["A", "B"]
    assert yt.as_name_list(None) == []



# ---------------------------------------------------------------- a day with a published Short is taken

def minutes_ago(m):
    return (datetime.datetime.now().astimezone() - datetime.timedelta(minutes=m)).isoformat()


def test_channel_videos_records_published_at_only_for_public():
    existing = [
        {"id": "P", "title": "Public", "fileName": "p.mov", "privacyStatus": "public",
         "publishedAt": minutes_ago(60)},
        {"id": "Q", "title": "Private", "fileName": "q.mov", "privacyStatus": "private",
         "publishedAt": minutes_ago(60)},
    ]
    got = {v["id"]: v for v in yt.channel_videos(FakeYouTube(existing=existing))}
    assert got["P"]["publishedAt"] and got["Q"]["publishedAt"] is None


def test_schedule_skips_today_if_a_short_went_public_today(tmp_path):
    jobs = make_jobs(tmp_path, [{"filename": "a.mov", "title": "A"}, {"filename": "b.mov", "title": "B"}])
    channel = [{"id": "T", "title": "Went live this morning", "isShort": True,
                "privacyStatus": "public", "publishAt": None, "publishedAt": minutes_ago(1)}]
    late = (datetime.datetime.now() + datetime.timedelta(hours=1)).time().replace(second=0, microsecond=0)
    if late < datetime.datetime.now().time():  # near midnight: skip, today has no later slot
        pytest.skip("too close to midnight")
    sched = {"time": late, "start": datetime.date.today()}
    plan = uploader.assign_slots(jobs, sched, channel, min_lead=15)
    got = [yt.local_date(j["body"]["status"]["publishAt"]) for j in jobs]
    assert got == [day_at(1).date(), day_at(2).date()]
    assert plan[0] == (datetime.date.today(), None, "Went live this morning (published)")


def test_published_long_form_does_not_take_today(tmp_path):
    jobs = make_jobs(tmp_path, [{"filename": "a.mov", "title": "A"}])
    channel = [{"id": "L", "title": "Long", "isShort": False, "privacyStatus": "public",
                "publishAt": None, "publishedAt": minutes_ago(1)}]
    late = (datetime.datetime.now() + datetime.timedelta(hours=1)).time().replace(second=0, microsecond=0)
    if late < datetime.datetime.now().time():
        pytest.skip("too close to midnight")
    uploader.assign_slots(jobs, {"time": late, "start": datetime.date.today()}, channel, min_lead=15)
    assert yt.local_date(jobs[0]["body"]["status"]["publishAt"]) == datetime.date.today()


def test_upcoming_shows_todays_published_short_and_counts_today_taken():
    channel = [
        {"id": "T", "title": "A6 this morning", "isShort": True, "privacyStatus": "public",
         "publishAt": None, "publishedAt": minutes_ago(1)},
        {"id": "S1", "title": "Tomorrow", "isShort": True, "publishAt": day_at(1).isoformat()},
        {"id": "OLD", "title": "Last week", "isShort": True, "privacyStatus": "public",
         "publishAt": None, "publishedAt": day_at(-7).isoformat()},
    ]
    lines = upcoming.render(channel, days=3)
    text = "\n".join(lines)
    assert 'published  "A6 this morning"' in lines[1] and "open" not in lines[1]
    assert 'scheduled  "Tomorrow"' in lines[2]
    assert "Last week" not in text
    assert "1 Short scheduled, 1 open day." in text



# ---------------------------------------------------------------- schedule.skip

def test_parse_skip():
    import yaml
    cfg = yaml.safe_load("time: '07:15'\nskip:\n  - 2026-11-26\n  - '2026-12-25'\n  - Sunday\n  - sat\n")
    sched, problems = uploader.parse_schedule(cfg)
    assert problems == []
    assert sched["skip_dates"] == {datetime.date(2026, 11, 26), datetime.date(2026, 12, 25)}
    assert sched["skip_weekdays"] == {6, 5}
    assert uploader.parse_schedule({"time": "07:15", "skip": "Sunday"})[0]["skip_weekdays"] == {6}
    assert uploader.parse_schedule({"time": "07:15", "skip": ["Funday"]})[1]
    every = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert "nothing could be scheduled" in uploader.parse_schedule({"time": "07:15", "skip": every})[1][0]


def test_assign_slots_skips_listed_days(tmp_path):
    jobs = make_jobs(tmp_path, [{"filename": f"{n}.mov", "title": n} for n in "abc"])
    d1, d2, d3, d4 = (datetime.date.today() + datetime.timedelta(days=i) for i in (1, 2, 3, 4))
    sched = {"time": datetime.time(13, 0), "start": d1, "skip_dates": {d2}, "skip_weekdays": set()}
    channel = [{"id": "S3", "title": "Taken", "isShort": True, "publishAt": day_at(3).isoformat()}]
    plan = uploader.assign_slots(jobs, sched, channel, min_lead=15)
    got = [yt.local_date(j["body"]["status"]["publishAt"]) for j in jobs]
    assert got[0] == d1 and got[1] == d4                  # d2 skipped, d3 taken
    assert (d2, None, None) in plan and (d3, None, "Taken") in plan


def test_assign_slots_skips_weekdays(tmp_path):
    jobs = make_jobs(tmp_path, [{"filename": f"{n}.mov", "title": n} for n in "abcdefg"])
    start = datetime.date.today() + datetime.timedelta(days=1)
    sched = {"time": datetime.time(13, 0), "start": start, "skip_dates": set(), "skip_weekdays": {5, 6}}
    uploader.assign_slots(jobs, sched, [], min_lead=15)
    days = [yt.local_date(j["body"]["status"]["publishAt"]) for j in jobs]
    assert all(d.weekday() < 5 for d in days) and len(set(days)) == 7


def test_skip_day_in_dry_run_and_own_publish_at_note(workdir, capsys):
    skip_day = datetime.date.today() + datetime.timedelta(days=1)
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nschedule:\n  time: '13:00'\n"
                   f"  skip: [{skip_day.isoformat()}]\n"
                   "videos:\n  - {filename: a.mov, title: A}\n"
                   f"  - {{filename: b.mov, title: B, publishAt: '{skip_day.isoformat()} 18:00'}}\n")
    fake = FakeYouTube(existing=[])
    assert uploader.main(["--vids", f, "--dry-run", "--log", str(workdir / "l.json")], youtube=fake) == 0
    out = capsys.readouterr().out
    assert "(skipped: in schedule.skip)" in out
    assert "NOTE b.mov: its own publishAt" in out


def test_upcoming_marks_skip_days(tmp_path):
    channel = [{"id": "S1", "title": "Tomorrow", "isShort": True, "publishAt": day_at(1).isoformat()}]
    skip = {"skip_dates": {datetime.date.today() + datetime.timedelta(days=2)}, "skip_weekdays": set()}
    lines = upcoming.render(channel, days=4, skip=skip)
    assert lines[3].endswith("skipped")
    assert "1 Short scheduled, 2 open days, 1 skipped day." in "\n".join(lines)


def test_upcoming_reads_skip_from_yaml(tmp_path, capsys):
    d = datetime.date.today() + datetime.timedelta(days=1)
    vids = write_yaml(tmp_path / "v.yml", f"schedule:\n  time: '13:00'\n  skip: [{d.isoformat()}]\nvideos: []\n")
    assert upcoming.main(["--days", "3", "--vids", vids], youtube=FakeYouTube(existing=[])) == 0
    out = capsys.readouterr().out
    assert "skipped" in out and "1 skipped day" in out


# ---------------------------------------------------------------- updater playlists

def test_updater_adds_to_playlists_skipping_ones_its_in(tmp_path, capsys):
    f = write_yaml(tmp_path / "d.yml", "videos:\n  - url: VID\n    playlists: [BNSF, Railfan Shorts]\n")
    fake = FakeYouTube(current={"VID": CURRENT}, playlists=PLAYLISTS, members={("PL_SHORTS", "VID")})
    assert updater.main(["--vids", f], youtube=fake) == 0
    assert fake.pl.added == [("PL_BNSF", "VID")]
    assert fake.videos().updated == []              # playlists only: no videos.update
    out = capsys.readouterr().out
    assert 'playlist "BNSF": + add' in out and 'playlist "Railfan Shorts": already in it' in out


def test_updater_playlists_with_other_changes(tmp_path):
    f = write_yaml(tmp_path / "d.yml", "videos:\n  - url: VID\n    title: New\n    playlists: BNSF\n")
    fake = FakeYouTube(current={"VID": CURRENT}, playlists=PLAYLISTS)
    assert updater.main(["--vids", f], youtube=fake) == 0
    assert fake.videos().updated[0][1]["snippet"]["title"] == "New"
    assert fake.pl.added == [("PL_BNSF", "VID")]


def test_updater_unknown_playlist_changes_nothing(tmp_path, capsys):
    f = write_yaml(tmp_path / "d.yml", "videos:\n  - url: VID\n    title: New\n    playlists: [Nope]\n")
    fake = FakeYouTube(current={"VID": CURRENT}, playlists=PLAYLISTS)
    assert updater.main(["--vids", f], youtube=fake) == 2
    assert fake.videos().updated == [] and fake.pl.added == []
    assert 'no playlist named "Nope"' in capsys.readouterr().out


def test_updater_playlist_dry_run(tmp_path, capsys):
    f = write_yaml(tmp_path / "d.yml", "videos:\n  - url: VID\n    playlists: [BNSF]\n")
    fake = FakeYouTube(current={"VID": CURRENT}, playlists=PLAYLISTS)
    assert updater.main(["--vids", f, "--dry-run"], youtube=fake) == 0
    assert fake.pl.added == [] and 'playlist "BNSF": + add' in capsys.readouterr().out


def test_updater_already_in_every_playlist_is_a_no_op(tmp_path, capsys):
    f = write_yaml(tmp_path / "d.yml", "videos:\n  - url: VID\n    playlists: [BNSF]\n")
    fake = FakeYouTube(current={"VID": CURRENT}, playlists=PLAYLISTS, members={("PL_BNSF", "VID")})
    assert updater.main(["--vids", f], youtube=fake) == 0
    assert fake.pl.added == [] and "(nothing to change)" in capsys.readouterr().out


def test_updater_failed_playlist_add_counts_as_failure(tmp_path, capsys):
    f = write_yaml(tmp_path / "d.yml", "videos:\n  - url: VID\n    playlists: [BNSF]\n")
    fake = FakeYouTube(current={"VID": CURRENT}, playlists=PLAYLISTS,
                       playlist_results=[http_error(403, "playlistItemsNotAccessible")])
    assert updater.main(["--vids", f], youtube=fake) == 1
    assert "couldn't add to playlist" in capsys.readouterr().out



# ---------------------------------------------------------------- exact file size

def test_same_file_with_new_name_and_title_is_caught_by_size(workdir, capsys):
    """The AMTK 301 case: uploaded before via the API/app (no fileName recorded) with
    another title; same bytes now under a new name and title."""
    (workdir / "vids" / "AMTK 301 Departs GBB 20261022.mov").write_bytes(b"x" * 4321)
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: AMTK 301 Departs GBB 20261022.mov,"
                   " title: AMTK 301 Leads the Illinois Zephyr Out of Galesburg 20261022}\n")
    existing = [{"id": "uOYYEDHkYdg", "title": "AMTK 301 Takes Ill Zephyr Toward Quincy 20261022",
                 "fileName": "", "fileSize": 4321}]
    fake = FakeYouTube(existing=existing, insert_results=[{"id": "NEW"}])
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 0
    out = capsys.readouterr().out
    assert "DUPLICATE AMTK 301 Departs GBB 20261022.mov: skipped" in out
    assert "same file (identical size, 4,321 bytes) as https://youtu.be/uOYYEDHkYdg" in out
    assert fake.videos().inserted == []


def test_different_size_same_night_is_not_a_duplicate(workdir):
    (workdir / "vids" / "a.mov").write_bytes(b"x" * 100)
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: New one}\n")
    existing = [{"id": "O", "title": "Other 20261022", "fileName": "", "fileSize": 101}]
    fake = FakeYouTube(existing=existing, insert_results=[{"id": "NEW"}])
    assert uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake) == 0
    assert len(fake.videos().inserted) == 1


def test_size_and_name_match_reported_once(workdir, capsys):
    (workdir / "vids" / "a.mov").write_bytes(b"x" * 50)
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n  - {filename: a.mov, title: A}\n")
    existing = [{"id": "O", "title": "A", "fileName": "a.mov", "fileSize": 50}]
    fake = FakeYouTube(existing=existing)
    uploader.main(["--vids", f, "--log", str(workdir / "l.json")], youtube=fake)
    out = capsys.readouterr().out
    assert out.count("https://youtu.be/O") == 1 and "identical size" in out


def test_channel_videos_reads_file_size():
    existing = [{"id": "A", "title": "t", "fileName": "", "fileSize": 328170768},
                {"id": "B", "title": "u", "fileName": "b.mov"}]
    got = {v["id"]: v for v in yt.channel_videos(FakeYouTube(existing=existing))}
    assert got["A"]["fileSize"] == 328170768 and got["B"]["fileSize"] is None


def test_channel_videos_drops_repeats_from_paging():
    existing = [{"id": "A", "title": "a", "fileName": "a.mov", "fileSize": 10},
                {"id": "B", "title": "b", "fileName": "b.mov", "fileSize": 20},
                {"id": "A", "title": "a", "fileName": "a.mov", "fileSize": 10},   # repeated on page 2
                {"id": "C", "title": "c", "fileName": "c.mov", "fileSize": 30}]
    got = yt.channel_videos(FakeYouTube(existing=existing))
    assert [v["id"] for v in got] == ["A", "B", "C"]



# ---------------------------------------------------------------- find_duplicates.py

import find_duplicates  # noqa: E402


def test_find_duplicates_groups_and_keeps_most_viewed(capsys):
    existing = [
        {"id": "OLD", "title": "AMTK 301 Takes Ill Zephyr", "fileSize": 328170768, "views": 900,
         "published": "2026-10-27T12:00:00Z"},
        {"id": "NEW", "title": "AMTK 301 Leads the Illinois Zephyr", "fileSize": 328170768, "views": 249,
         "published": "2026-11-22T13:15:00Z"},
        {"id": "M1", "title": "Metrolinks A", "fileSize": 5, "views": 10, "published": "2026-05-14T12:00:00Z"},
        {"id": "M2", "title": "Metrolinks B", "fileSize": 5, "views": 10, "published": "2026-05-13T12:00:00Z"},
        {"id": "M3", "title": "Metrolinks C", "fileSize": 5, "views": 50, "published": "2026-05-15T12:00:00Z"},
        {"id": "SOLO", "title": "Unique", "fileSize": 7, "views": 1},
    ]
    existing.append(dict(existing[0]))   # the API repeating a video while paging
    fake = FakeYouTube(existing=existing)
    assert find_duplicates.main([], youtube=fake) == 0
    out = capsys.readouterr().out
    assert "2 groups of identical files, 3 extra copies" in out
    assert "KEEP    OLD" in out and "delete  NEW" in out
    assert "KEEP    M3" in out and "delete  M2" in out and "delete  M1" in out
    assert out.index("delete  M2") < out.index("delete  M1")          # tie: earlier first
    assert "https://studio.youtube.com/video/NEW/edit" in out
    assert "SOLO" not in out
    assert fake.videos().inserted == [] and fake.videos().updated == []


def test_find_duplicates_none(capsys):
    fake = FakeYouTube(existing=[{"id": "A", "title": "a", "fileSize": 1}, {"id": "B", "title": "b", "fileSize": 2}])
    assert find_duplicates.main([], youtube=fake) == 0
    assert "No videos on the channel share an identical file." in capsys.readouterr().out
