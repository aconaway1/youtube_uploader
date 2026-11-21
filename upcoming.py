#!/usr/bin/env python3
"""Show what's scheduled to go public on the channel, day by day.

    python upcoming.py              # next 14 days
    python upcoming.py --days 30
    python upcoming.py --vids other.yml   # read schedule.skip from another file

Days without a Short (scheduled, or already published that day) are marked
"open"; those are the days the uploader's `schedule:` block fills. Days listed
in schedule.skip in video_summary.yml (if it exists) are marked "skipped". Long-form videos are listed but don't
fill a day. Costs about 1 quota unit per 25 videos on the channel.
"""
import argparse
import datetime
import os
import sys

import yaml

from googleapiclient.errors import HttpError

import uploader
import yt_common as yt


def render(channel, days, today=None, now=None, skip=None):
    """Lines of the day-by-day view."""
    now = now or datetime.datetime.now().astimezone()
    today = today or now.date()
    end = today + datetime.timedelta(days=days - 1)
    upcoming = yt.scheduled_videos(channel, now)

    # (when, video, published?) per day: what's scheduled, plus what already went
    # public in the window (in practice, earlier today).
    by_day = {}
    for v in upcoming:
        by_day.setdefault(yt.local_date(v["publishAt"]), []).append((v["publishAt"], v, False))
    for v in yt.published_between(channel, today, end):
        by_day.setdefault(yt.local_date(v["publishedAt"]), []).append((v["publishedAt"], v, True))

    width = max(len(_day(today + datetime.timedelta(days=i))) for i in range(days))
    skip = skip or {}
    lines = [f"Scheduled to go public, {_day(today)} through {_day(end)}:"]
    open_days = scheduled = skipped = 0
    for i in range(days):
        day = today + datetime.timedelta(days=i)
        entries = sorted(by_day.get(day, []), key=lambda e: yt._as_datetime(e[0]))
        label = _day(day).ljust(width)
        is_skip = uploader.is_skipped(day, skip)
        if not entries:
            if is_skip:
                skipped += 1
                lines.append(f"  {label}  skipped")
            else:
                open_days += 1
                lines.append(f"  {label}  open")
            continue
        for n, (when, v, published) in enumerate(entries):
            scheduled += bool(v.get("isShort")) and not published
            kind = "Short" if v.get("isShort") else "Video"
            time = yt._as_datetime(when).astimezone().strftime("%I:%M %p").lstrip("0")
            state = "published" if published else "scheduled"
            prefix = label if n == 0 else " " * width
            lines.append(f"  {prefix}  {time:>8}  {kind:5}  {state:9}  \"{v['title']}\"  "
                         f"https://youtu.be/{v['id']}")
        if is_skip:
            skipped += 1
            lines[-1] += "   (skip day)"
        elif not any(v.get("isShort") for _, v, _ in entries):
            open_days += 1
            lines[-1] += "   (no Short: open)"

    later = [v for v in upcoming if yt.local_date(v["publishAt"]) > end]
    lines.append("")
    lines.append(f"{scheduled} Short{'s' if scheduled != 1 else ''} scheduled, "
                 f"{open_days} open day{'s' if open_days != 1 else ''}"
                 + (f", {skipped} skipped day{'s' if skipped != 1 else ''}" if skipped else "")
                 + "."
                 + (f" {len(later)} more scheduled after {_day(end)}." if later else ""))
    return lines


def _day(d):
    return d.strftime("%a %b %d").replace(" 0", " ")


def main(argv=None, youtube=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14, help="how many days to show (default 14)")
    parser.add_argument("--vids", default="video_summary.yml",
                        help="YAML whose schedule.skip days to mark (default video_summary.yml, "
                             "if it exists)")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    skip = {}
    if os.path.exists(args.vids):
        with open(args.vids) as f:
            data = yaml.safe_load(f) or {}
        sched = data.get("schedule") if isinstance(data, dict) else None
        if isinstance(sched, dict) and sched.get("skip"):
            dates, weekdays, problems = uploader.parse_skip(sched["skip"])
            for p in problems:
                print(f"NOTE {args.vids}: {p}")
            skip = {"skip_dates": dates, "skip_weekdays": weekdays}

    youtube = youtube or yt.get_youtube()
    try:
        channel = yt.channel_videos(youtube)
    except HttpError as e:
        print(f"Couldn't read the channel's videos: HTTP {e.resp.status} {yt.http_error_reason(e)}")
        return 1
    print("\n".join(render(channel, args.days, skip=skip)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
