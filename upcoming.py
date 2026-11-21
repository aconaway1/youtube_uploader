#!/usr/bin/env python3
"""Show what's scheduled to go public on the channel, day by day.

    python upcoming.py              # next 14 days
    python upcoming.py --days 30

Days without a Short (scheduled, or already published that day) are marked
"open"; those are the days the uploader's `schedule:` block fills. Long-form videos are listed but don't
fill a day. Costs about 1 quota unit per 25 videos on the channel.
"""
import argparse
import datetime
import sys

from googleapiclient.errors import HttpError

import yt_common as yt


def render(channel, days, today=None, now=None):
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
    lines = [f"Scheduled to go public, {_day(today)} through {_day(end)}:"]
    open_days = scheduled = 0
    for i in range(days):
        day = today + datetime.timedelta(days=i)
        entries = sorted(by_day.get(day, []), key=lambda e: yt._as_datetime(e[0]))
        label = _day(day).ljust(width)
        if not entries:
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
        if not any(v.get("isShort") for _, v, _ in entries):
            open_days += 1
            lines[-1] += "   (no Short: open)"

    later = [v for v in upcoming if yt.local_date(v["publishAt"]) > end]
    lines.append("")
    lines.append(f"{scheduled} Short{'s' if scheduled != 1 else ''} scheduled, "
                 f"{open_days} open day{'s' if open_days != 1 else ''}."
                 + (f" {len(later)} more scheduled after {_day(end)}." if later else ""))
    return lines


def _day(d):
    return d.strftime("%a %b %d").replace(" 0", " ")


def main(argv=None, youtube=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14, help="how many days to show (default 14)")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    youtube = youtube or yt.get_youtube()
    try:
        channel = yt.channel_videos(youtube)
    except HttpError as e:
        print(f"Couldn't read the channel's videos: HTTP {e.resp.status} {yt.http_error_reason(e)}")
        return 1
    print("\n".join(render(channel, args.days)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
