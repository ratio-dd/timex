"""Normalized CSV adapter; real aTimeLogger exports need a separate adapter."""
import csv
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import CATEGORIES, TimeSummary


def load_summary(path: str | Path, *, period_start: date, period_end: date,
                 timezone_name: str = "Asia/Shanghai", category_map: dict[str, str] | None = None) -> TimeSummary:
    if period_end < period_start:
        raise ValueError("invalid period")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("unknown timezone") from error
    start = datetime.combine(period_start, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(period_end + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    events = []
    seen = set()
    duplicates = 0
    mapped_other = 0
    mapping = category_map or {}
    if any(value not in CATEGORIES or value == "untracked" for value in mapping.values()):
        raise ValueError("mapping must target recorded categories")
    with open(path, newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["start_at", "end_at", "category"]:
            raise ValueError("normalized CSV requires start_at,end_at,category in that order")
        for line, row in enumerate(reader, 2):
            if None in row or any(not value or not value.strip() for value in row.values()):
                raise ValueError(f"line {line}: missing or extra value")
            try:
                a = datetime.fromisoformat(row["start_at"])
                b = datetime.fromisoformat(row["end_at"])
                if a.utcoffset() is None or b.utcoffset() is None:
                    raise ValueError("timestamps require explicit timezone")
                a, b = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
                if b <= a:
                    raise ValueError("duration must be positive")
            except ValueError as error:
                raise ValueError(f"line {line}: {error}") from error
            category = mapping.get(row["category"], row["category"])
            if category == "untracked":
                raise ValueError("untracked is derived, not an event category")
            if category not in CATEGORIES:
                category = "other"
                mapped_other += 1
            event = (a, b, category)
            if event in seen:
                duplicates += 1
                continue
            seen.add(event)
            if b > start and a < end:
                events.append((max(a, start), min(b, end), category))
    events.sort()
    totals = {category: 0.0 for category in CATEGORIES}
    last_end = start
    for a, b, category in events:
        if a < last_end:
            raise ValueError("overlapping intervals in selected window; refusing double counting")
        totals[category] += (b - a).total_seconds() / 60
        last_end = b
    tracked = sum(totals.values())
    capacity = (end - start).total_seconds() / 60
    totals["untracked"] = capacity - tracked
    notes = ["内部标准化 CSV；不是对真实 aTimeLogger 导出格式的假设。",
             "未记录区间计为 untracked，不视为休闲；分类可能存在偏差。"]
    if duplicates:
        notes.append(f"忽略 {duplicates} 条完全重复记录。")
    if mapped_other:
        notes.append(f"{mapped_other} 条未知分类映射为 other。")
    if not events:
        notes.append("所选范围无记录；不能推断实际活动或睡眠。")
    return TimeSummary(period_start=period_start, period_end=period_end,
                       timezone=timezone_name, category_minutes=totals,
                       tracked_minutes=tracked, untracked_minutes=totals["untracked"],
                       coverage=tracked / capacity, notes=notes)
