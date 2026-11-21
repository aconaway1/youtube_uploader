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

    def insert(self, part, body, media_body):
        self.inserted.append((part, body))
        return FakeRequest([self.insert_results.pop(0)])

    def list(self, part, id):
        self.list_calls += 1
        items = []
        for vid in id.split(","):
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


class FakeYouTube:
    def __init__(self, thumbnail_results=None, **kw):
        self._videos = FakeVideos(**kw)
        self._thumbs = FakeThumbnails(thumbnail_results)

    def videos(self):
        return self._videos

    def thumbnails(self):
        return self._thumbs


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
    assert aaron_uploader.main(["--vids", custom, "--dry-run", "--log", str(workdir / "log.json")]) == 0
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
    fake2 = FakeYouTube(insert_results=[{"id": "IDB"}])
    assert aaron_uploader.main(["--vids", f, "--log", log], youtube=fake2) == 0
    assert [b["snippet"]["title"] for _, b in fake2.videos().inserted] == ["B"]
    assert "SKIP a.mov" in capsys.readouterr().out


def test_quota_error_stops_batch(workdir):
    f = write_yaml(workdir / "v.yml", "video_dir: vids\nvideos:\n"
                   "  - {filename: a.mov, title: A}\n  - {filename: b.mov, title: B}\n")
    fake = FakeYouTube(insert_results=[http_error(403, "quotaExceeded"), {"id": "never"}])
    assert aaron_uploader.main(["--vids", f, "--log", str(workdir / "log.json")], youtube=fake) == 1
    assert len(fake.videos().inserted) == 1


def test_video_dir_relative_to_yaml_and_tilde(workdir, monkeypatch):
    monkeypatch.setenv("HOME", str(workdir))
    f = write_yaml(workdir / "v.yml", "video_dir: '~/vids'\nvideos:\n  - {filename: a.mov, title: A}\n")
    assert aaron_uploader.main(["--vids", f, "--dry-run", "--log", str(workdir / "l.json")]) == 0


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


def run_updater(tmp_path, yaml_text, current=CURRENT, dry=False):
    f = write_yaml(tmp_path / "d.yml", yaml_text)
    fake = FakeYouTube(current={"VID": current})
    argv = ["--vids", f] + (["--dry-run"] if dry else [])
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
    assert aaron_uploader.main(["--vids", f, "--dry-run", "--log", str(thumbdir / "l.json")]) == 2
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
    fake2 = FakeYouTube(processing={"IA": ["succeeded"]})
    assert aaron_uploader.main(["--vids", f, "--log", log], youtube=fake2) == 0
    assert fake2.videos().inserted == []
    assert [v for v, _, _ in fake2.thumbnails().set_calls] == ["IA"]
    assert json.load(open(log)).popitem()[1]["thumbnail_status"] == "set"

    # Third run: nothing to do.
    fake3 = FakeYouTube()
    assert aaron_uploader.main(["--vids", f, "--log", log], youtube=fake3) == 0
    assert fake3.thumbnails().set_calls == [] and fake3.videos().list_calls == 0


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
