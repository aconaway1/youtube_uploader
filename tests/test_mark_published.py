"""mark_published.py against a fake `xattr` that mimics macOS output."""
import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mark_published as mp  # noqa: E402

FAKE_XATTR = r'''#!/usr/bin/env python3
import json, os, sys
store = os.environ["FAKE_XATTR_STORE"]
db = json.load(open(store)) if os.path.exists(store) else {}
args = sys.argv[1:]
if args[0] == "-px":
    name, path = args[1], args[2]
    if not os.path.exists(path):
        sys.stderr.write(f"xattr: No such file: {path}\n"); sys.exit(1)
    h = db.get(path, {}).get(name)
    if h is None:
        sys.stderr.write(f"xattr: {path}: No such xattr: {name}\n"); sys.exit(1)
    b = bytes.fromhex(h)
    # macOS prints uppercase hex pairs, 16 per line
    for i in range(0, len(b), 16):
        print(" ".join(f"{x:02X}" for x in b[i:i+16]))
elif args[0] == "-wx":
    name, hexval, path = args[1], args[2], args[3]
    db.setdefault(path, {})[name] = hexval
    json.dump(db, open(store, "w"))
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = tmp_path / "xattr"
    fake.write_text(FAKE_XATTR)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    store = tmp_path / "store.json"
    monkeypatch.setattr(mp, "XATTR", str(fake))
    monkeypatch.setenv("FAKE_XATTR_STORE", str(store))
    monkeypatch.setenv("MARK_PUBLISHED_ANY_OS", "1")
    monkeypatch.setattr(mp, "SETTINGS_FILE", str(tmp_path / "no-settings.yml"))
    files = {}
    for n in ("a.mov", "b.mov", "c.mov"):
        p = tmp_path / n
        p.write_bytes(b"x")
        files[n] = str(p)
    log = tmp_path / "upload_log.json"
    return tmp_path, files, log


def set_tags(path, tags):
    mp.write_tags(path, tags)


def write_log(log, paths):
    log.write_text(json.dumps({p: {"video_id": f"ID{i}", "title": "t"} for i, p in enumerate(paths)}))


def test_encode_decode_roundtrip():
    tags = ["Approved for Publishing\n5", "Railfan"]
    assert mp.decode_tags(mp.encode_tags(tags)) == tags
    assert mp.encode_tags(tags).startswith(b"bplist00")


def test_retag():
    assert mp.retag(["Approved for Publishing\n5", "Railfan"], "Published", "green",
                    "Approved for Publishing") == ["Railfan", "Published\n2"]
    assert mp.retag(["Published\n2"], "Published", "green", "Approved for Publishing") == ["Published\n2"]
    assert mp.retag(["Approved for Publishing\n5"], "Published", "green", None) == \
        ["Approved for Publishing\n5", "Published\n2"]


def test_tags_logged_files_only(env, capsys):
    tmp, f, log = env
    set_tags(f["a.mov"], ["Approved for Publishing\n5", "BNSF"])
    set_tags(f["c.mov"], ["Approved for Publishing\n5"])
    write_log(log, [f["a.mov"], f["b.mov"]])  # c not uploaded
    assert mp.main(["--log", str(log)]) == 0
    assert mp.read_tags(f["a.mov"]) == ["BNSF", "Published\n2"]
    assert mp.read_tags(f["b.mov"]) == ["Published\n2"]          # had no tags at all
    assert mp.read_tags(f["c.mov"]) == ["Approved for Publishing\n5"]  # untouched
    assert "2 tagged, 0 already tagged" in capsys.readouterr().out


def test_idempotent(env, capsys):
    tmp, f, log = env
    write_log(log, [f["a.mov"]])
    mp.main(["--log", str(log)])
    capsys.readouterr()
    assert mp.main(["--log", str(log)]) == 0
    assert "0 tagged, 1 already tagged" in capsys.readouterr().out
    assert mp.read_tags(f["a.mov"]) == ["Published\n2"]


def test_dry_run_and_keep_approved(env, capsys):
    tmp, f, log = env
    set_tags(f["a.mov"], ["Approved for Publishing\n5"])
    write_log(log, [f["a.mov"]])
    mp.main(["--log", str(log), "--dry-run"])
    assert mp.read_tags(f["a.mov"]) == ["Approved for Publishing\n5"]
    assert "WOULD TAG" in capsys.readouterr().out
    mp.main(["--log", str(log), "--keep-approved"])
    assert mp.read_tags(f["a.mov"]) == ["Approved for Publishing\n5", "Published\n2"]


def test_missing_file_and_no_log(env, capsys):
    tmp, f, log = env
    write_log(log, [str(tmp / "moved.mov")])
    assert mp.main(["--log", str(log)]) == 0
    assert "MISSING moved.mov" in capsys.readouterr().out
    assert mp.main(["--log", str(tmp / "nope.json"), "--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_quiet_prints_nothing_when_nothing_changes(env, capsys):
    tmp, f, log = env
    write_log(log, [f["a.mov"]])
    mp.main(["--log", str(log), "--quiet"])
    capsys.readouterr()
    mp.main(["--log", str(log), "--quiet"])
    assert capsys.readouterr().out == ""



# ---------------------------------------------------------------- settings.yml

def write_settings(tmp, text):
    p = tmp / "settings.yml"
    p.write_text(text)
    return str(p)


def test_custom_tag_names_and_color(env, capsys):
    tmp, f, log = env
    set_tags(f["a.mov"], ["Ready for Publishing\n5", "BNSF"])
    write_log(log, [f["a.mov"]])
    st = write_settings(tmp, "finder_tags:\n  published: Posted\n  published_color: blue\n"
                             "  approved: Ready for Publishing\n")
    assert mp.main(["--log", str(log), "--settings", st]) == 0
    assert mp.read_tags(f["a.mov"]) == ["BNSF", "Posted\n4"]


def test_remove_approved_false(env):
    tmp, f, log = env
    set_tags(f["a.mov"], ["Approved for Publishing\n5"])
    write_log(log, [f["a.mov"]])
    st = write_settings(tmp, "finder_tags:\n  remove_approved: false\n")
    assert mp.main(["--log", str(log), "--settings", st]) == 0
    assert mp.read_tags(f["a.mov"]) == ["Approved for Publishing\n5", "Published\n2"]


def test_partial_settings_keep_defaults(env):
    tmp, f, log = env
    set_tags(f["a.mov"], ["Approved for Publishing\n5"])
    write_log(log, [f["a.mov"]])
    st = write_settings(tmp, "finder_tags:\n  published_color: red\n")
    mp.main(["--log", str(log), "--settings", st])
    assert mp.read_tags(f["a.mov"]) == ["Published\n6"]


@pytest.mark.parametrize("text,msg", [
    ("finder_tags:\n  published_color: teal\n", "must be one of"),
    ("finder_tags:\n  publish: X\n", "unknown finder_tags setting(s) publish"),
    ("finder_tags:\n  published: Same\n  approved: Same\n", "can't be the same"),
    ("finder_tags:\n  remove_approved: maybe\n", "true or false"),
    ("finder_tags:\n  published: ''\n", "must be a tag name"),
    ("finder_tags: Published\n", "must be a block"),
])
def test_bad_settings_stop_before_tagging(env, capsys, text, msg):
    tmp, f, log = env
    write_log(log, [f["a.mov"]])
    st = write_settings(tmp, text)
    assert mp.main(["--log", str(log), "--settings", st]) == 2
    assert msg in capsys.readouterr().out
    assert mp.read_tags(f["a.mov"]) == []


def test_example_settings_match_defaults():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples", "settings.example.yml")
    tags, problems = mp.load_settings(path)
    assert problems == [] and tags == mp.DEFAULT_TAGS


# ---------------------------------------------------------------- real macOS (runs in CI on macos-latest)

@pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/xattr"),
                    reason="needs macOS's /usr/bin/xattr")
def test_real_xattr_roundtrip_on_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "SETTINGS_FILE", str(tmp_path / "no-settings.yml"))
    p = tmp_path / "clip.mov"
    p.write_bytes(b"v")
    assert mp.read_tags(str(p)) == []                       # "No such xattr" path
    mp.write_tags(str(p), ["Approved for Publishing\n5", "BNSF"])
    assert mp.read_tags(str(p)) == ["Approved for Publishing\n5", "BNSF"]
    log = tmp_path / "upload_log.json"
    log.write_text(json.dumps({str(p): {"video_id": "X"}}))
    assert mp.main(["--log", str(log)]) == 0
    assert mp.read_tags(str(p)) == ["BNSF", "Published\n2"]
