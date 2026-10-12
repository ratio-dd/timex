"""Read-only aTimeLogger adapter; raw records stay in memory.

Uses the API fields verified in atimelogger-mcp 0.2.0. The CLI renders
interval timestamps to minutes without offsets, so exact Unix seconds from
the same read-only history endpoint are needed for coverage and DST.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
import json
import math
import os
import urllib.request
from zoneinfo import ZoneInfo

from .models import CATEGORIES, TimeSummary


def read_api(path: str, payload: dict | None = None):
    """Only required read endpoints, including history/statistics POSTs."""
    if path.split("?")[0] not in {
        "/api/users/me", "/api/types", "/api/intervals", "/api/statistics", "/api/activities",
    }:
        raise ValueError("unsupported read endpoint")
    token = os.environ.get("ATIMELOGGER_ACCESS_TOKEN")
    if not token:
        raise ValueError("missing ATIMELOGGER_ACCESS_TOKEN")
    request = urllib.request.Request(
        "https://app.atimelogger.pro" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except Exception:
        raise ValueError("aTimeLogger read failed; private response suppressed") from None


def type_mapping(types: list[dict], mapping: dict[str, str]):
    """Keys are paths or id:<stable ID>; nearest ancestor wins, ID breaks ties.

    ID mappings survive renames. Path mappings remain useful for initial setup.
    New descendants inherit group defaults; unmatched leaves are audited as other.
    """
    if any(v not in CATEGORIES or v == "untracked" for v in mapping.values()):
        raise ValueError("mapping must target recorded categories")
    by_id = {t["id"]: t for t in types}
    if len(by_id) != len(types):
        raise ValueError("duplicate type identifiers")
    resolved, audit = {}, []
    for t in types:
        if t.get("deleted") or t.get("group"):
            continue
        ancestors, ids, seen, node, orphaned = [], [], set(), t, False
        while node:
            if node["id"] in seen:
                raise ValueError("cyclic type hierarchy")
            seen.add(node["id"])
            ancestors.append(node["name"])
            ids.append(node["id"])
            parent = node.get("parentId")
            if parent and parent not in by_id:
                orphaned = True
                break
            node = by_id.get(parent)
        parts = list(reversed(ancestors))
        paths = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        candidates = [key for p, identifier in zip(paths, reversed(ids))
                      for key in (p, "id:" + identifier)]
        matches = [p for p in candidates if p in mapping]
        source = matches[-1] if matches else None
        category = mapping[source] if source else "other"
        resolved[t["id"]] = category
        audit.append({"path": paths[-1], "category": category,
                      "mapping_source": "stable_id" if source and source.startswith("id:") else source,
                      "defaulted": source is None, "orphaned_parent": orphaned})
    return resolved, audit


def aggregate_intervals(intervals: list[dict], *, period_start: date, period_end: date,
                        timezone_name: str, mapping: dict[str, str]):
    """Exclusive categories; cross-category overlap becomes other, not double time."""
    if period_end < period_start:
        raise ValueError("invalid period")
    if any(v not in CATEGORIES or v == "untracked" for v in mapping.values()):
        raise ValueError("invalid interval mapping")
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(period_start, time.min, zone).timestamp()
    end = datetime.combine(period_end + timedelta(days=1), time.min, zone).timestamp()
    changes = defaultdict(list)
    seen, duplicates, unknown, selected = {}, 0, 0, 0
    raw_seconds = 0.0
    for item in intervals:
        a, b, duration = item.get("from"), item.get("to"), item.get("duration")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (a, b, duration)):
            raise ValueError("invalid interval timestamps or duration")
        if b <= a or abs((b - a) - duration) > 1:
            raise ValueError("interval duration disagrees with timestamps")
        key = item.get("id")
        if not isinstance(key, str) or not key or not isinstance(item.get("typeId"), str):
            raise ValueError("missing interval identifiers")
        signature = (a, b, item["typeId"])
        if key in seen:
            if seen[key] != signature:
                raise ValueError("conflicting duplicate interval")
            duplicates += 1
            continue
        seen[key] = signature
        a, b = max(a, start), min(b, end)
        if a >= b:
            continue
        category = mapping.get(item["typeId"], "other")
        unknown += item["typeId"] not in mapping
        selected += 1
        raw_seconds += b - a
        changes[a].append((category, 1))
        changes[b].append((category, -1))
    # Local-midnight boundaries also split contributions into daily aggregates.
    boundaries = []
    day = period_start
    while day <= period_end + timedelta(days=1):
        boundaries.append(datetime.combine(day, time.min, zone).timestamp())
        day += timedelta(days=1)
    points = sorted(set(changes) | set(boundaries))
    totals = {c: 0.0 for c in CATEGORIES}
    daily = {str(period_start + timedelta(days=i)): {c: 0.0 for c in CATEGORIES}
             for i in range((period_end - period_start).days + 1)}
    active = Counter()
    overlap = ambiguous = 0.0
    for a, b in zip(points, points[1:]):
        for category, delta in changes[a]:
            active[category] += delta
        categories = {c for c, count in active.items() if count > 0}
        category = (next(iter(categories)) if len(categories) == 1
                    else "other" if categories else "untracked")
        seconds = b - a
        if sum(active.values()) > 1:
            overlap += seconds
        if len(categories) > 1:
            ambiguous += seconds
        totals[category] += seconds / 60
        local_day = str(datetime.fromtimestamp(a, zone).date())
        daily[local_day][category] += seconds / 60
    capacity = (end - start) / 60
    tracked = capacity - totals["untracked"]
    notes = [
        "来源：aTimeLogger 只读历史 API；使用精确 Unix 秒，按本地午夜裁剪完整日期。",
        "覆盖率来自区间并集；未记录不等于空闲，分类为零不等于实际行为不存在。",
        "父分组只用于映射，不计时；同类别重叠取并集，跨类别重叠保守归入 other。",
        "只统计历史区间，不外推进行中活动；原始流水、备注和标签不进入摘要。",
    ]
    if overlap:
        notes.append(f"重叠区间并集 {overlap / 60:.2f} 分钟，其中跨类别 {ambiguous / 60:.2f} 分钟归入 other。")
    if unknown:
        notes.append(f"{unknown} 条未知类别记录归入 other。")
    if not selected:
        notes.append("所选范围无记录；不能推断真实活动。")
    summary = TimeSummary(period_start=period_start, period_end=period_end,
                          timezone=timezone_name, category_minutes=totals,
                          tracked_minutes=tracked, untracked_minutes=totals["untracked"],
                          coverage=tracked / capacity, notes=notes)
    return summary, {"daily_category_minutes": daily, "interval_count": selected,
                     "duplicates_removed": duplicates, "unknown_type_intervals": unknown,
                     "raw_clipped_interval_seconds": raw_seconds,
                     "overlap_union_minutes": overlap / 60,
                     "cross_category_overlap_minutes": ambiguous / 60}


def collect_summary(*, mapping: dict[str, str], timezone_fallback: str | None = None,
                    period_end: date | None = None):
    """Last seven completed local days, bounded pagination, no stored raw records."""
    profile = read_api("/api/users/me")
    tz = profile.get("timeZone") or timezone_fallback
    if not tz:
        raise ValueError("account timezone missing; explicit fallback required")
    zone = ZoneInfo(tz)
    today = datetime.now(zone).date()
    last = period_end or today - timedelta(days=1)
    if last >= today:
        raise ValueError("select only completed local days")
    first = last - timedelta(days=6)
    resolved, audit = type_mapping(read_api("/api/types"), mapping)
    args = {"from": str(first), "to": str(last), "timezone": tz}
    intervals = []
    for page in range(10):
        data = read_api(f"/api/intervals?page={page}&size=20", args)
        if data.get("number") != page or not isinstance(data.get("content"), list):
            raise ValueError("unexpected history page")
        for day in data["content"]:
            if not isinstance(day.get("intervals"), list):
                raise ValueError("invalid history day")
            intervals.extend(day["intervals"])
        if data.get("last") is True:
            break
        if data.get("last") is not False:
            raise ValueError("missing pagination status")
    else:
        raise ValueError("history pagination limit reached")
    summary, diagnostics = aggregate_intervals(intervals, period_start=first, period_end=last,
                                               timezone_name=tz, mapping=resolved)
    stats = read_api("/api/statistics", dict(args, groupBy="DAY"))
    api_seconds = stats.get("total", {}).get("info", {}).get("total")
    if type(api_seconds) not in (int, float) or not math.isfinite(api_seconds) or api_seconds < 0:
        raise ValueError("invalid statistics total")
    diagnostics["api_statistic_seconds"] = api_seconds
    diagnostics["api_minus_raw_interval_seconds"] = api_seconds - diagnostics["raw_clipped_interval_seconds"]
    diagnostics["api_minus_union_seconds"] = api_seconds - summary.tracked_minutes * 60
    activities = read_api("/api/activities")
    diagnostics["active_or_paused_activity_count"] = sum(
        a.get("status") in {"RUNNING", "PAUSED"} for a in activities.get("activities", []))
    source = "account" if profile.get("timeZone") else "explicit fallback"
    summary.notes.append(f"时区来源：{source} ({tz})；最近 7 个完整本地日，不包含今天。")
    if any(x["orphaned_parent"] for x in audit):
        summary.notes.append("类别树有缺失父组；这些类别仅采用明确的类别规则，否则归入 other，不猜测父组。")
    if abs(diagnostics["api_minus_union_seconds"]) <= 1:
        summary.notes.append("API 统计总量与历史区间并集一致（容许 1 秒的日期边界差）；不采用原始区间时长相加。")
    else:
        summary.notes.append("API 统计总量与历史区间并集不同；采用历史区间并集，差额待核对。")
    return {"time_summary": summary.model_dump(mode="json"), "mapping_audit": audit,
            "diagnostics": diagnostics, "provenance": {
                "source": "https://app.atimelogger.pro", "reference_cli_version": "0.2.0",
                "fetched_at": datetime.now(timezone.utc).isoformat(), "timezone_source": source,
                "model_calls": 0, "raw_records_persisted": False}}
