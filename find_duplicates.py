#!/usr/bin/env python3
"""List videos on the channel that are the same file uploaded more than once.

Groups videos whose uploaded files are byte-for-byte the same size (the same
file uploaded again, whatever its name or title). In each group the copy with
the most views is marked KEEP (ties go to the one published first); the rest
are listed with Studio links so you can delete them there.

This script only reads. It never deletes, edits or uploads anything.

    python find_duplicates.py

Costs about 1 quota unit per 25 videos on the channel, plus 1 per 50 duplicates.
"""
import argparse
import sys

from googleapiclient.errors import HttpError

import yt_common as yt


def duplicate_groups(channel):
    """[[video, ...], ...] for each file size shared by two or more videos."""
    by_size = {}
    for v in channel:
        if v.get("fileSize"):
            by_size.setdefault(v["fileSize"], []).append(v)
    return [g for _, g in sorted(by_size.items()) if len(g) > 1]


def add_stats(youtube, videos):
    """Fill in views and published time for the given videos (1 unit per 50)."""
    by_id = {v["id"]: v for v in videos}
    ids = list(by_id)
    for i in range(0, len(ids), 50):
        resp = youtube.videos().list(part="statistics,snippet", id=",".join(ids[i:i + 50])).execute()
        for item in resp.get("items", []):
            v = by_id[item["id"]]
            v["views"] = int(item.get("statistics", {}).get("viewCount", 0) or 0)
            v["published"] = item.get("snippet", {}).get("publishedAt")


def keep_order(group):
    """Most views first; ties go to the earliest published."""
    far_future = "9999"
    return sorted(group, key=lambda v: (-v.get("views", 0), v.get("published") or far_future))


def render(groups):
    extra = sum(len(g) - 1 for g in groups)
    if not groups:
        return ["No videos on the channel share an identical file."]
    lines = [f"{len(groups)} group{'s' if len(groups) != 1 else ''} of identical files, "
             f"{extra} extra cop{'y' if extra == 1 else 'ies'}. Delete the extras in Studio:", ""]
    for group in groups:
        lines.append(f"{group[0]['fileSize']:,} bytes")
        for n, v in enumerate(keep_order(group)):
            mark = "KEEP  " if n == 0 else "delete"
            when = ""
            if v.get("published"):
                when = yt._as_datetime(v["published"]).astimezone().strftime("%b %d, %Y").replace(" 0", " ")
            lines.append(f"  {mark}  {v['id']}  {v.get('views', 0):>7,} views  {when:>12}  "
                         f"\"{v['title']}\"")
            if n:
                lines.append(f"          https://studio.youtube.com/video/{v['id']}/edit")
        lines.append("")
    return lines


def main(argv=None, youtube=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)
    youtube = youtube or yt.get_youtube()
    try:
        groups = duplicate_groups(yt.channel_videos(youtube))
        add_stats(youtube, [v for g in groups for v in g])
    except HttpError as e:
        print(f"Couldn't read the channel: HTTP {e.resp.status} {yt.http_error_reason(e)}")
        return 1
    print("\n".join(render(groups)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
