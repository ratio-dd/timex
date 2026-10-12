from datetime import date, datetime
import json

import pytest

from timex.atimelogger import aggregate_intervals, collect_summary, type_mapping


def interval(identifier, start, finish, category="work", **extra):
    a, b = datetime.fromisoformat(start).timestamp(), datetime.fromisoformat(finish).timestamp()
    return dict(id=identifier, typeId=category, **{"from": a, "to": b}, duration=b-a, **extra)


def aggregate(items, start="2026-10-05", end="2026-10-11", zone="Asia/Shanghai"):
    return aggregate_intervals(items, period_start=date.fromisoformat(start),
                               period_end=date.fromisoformat(end), timezone_name=zone,
                               mapping={"work": "work", "sleep": "sleep"})


def test_union_overlap_clipping_dedup_and_private_fields():
    work = interval("1", "2026-10-04T23:00:00+08:00", "2026-10-05T02:00:00+08:00",
                    comment="PRIVATE NOTE", tags=["PRIVATE TAG"])
    items = [work, dict(work),
             interval("2", "2026-10-05T01:00:00+08:00", "2026-10-05T03:00:00+08:00"),
             interval("3", "2026-10-05T02:30:00+08:00", "2026-10-05T03:30:00+08:00", "sleep")]
    summary, audit = aggregate(items)
    assert summary.tracked_minutes == 210
    assert summary.category_minutes["work"] == 150
    assert summary.category_minutes["sleep"] == 30
    assert summary.category_minutes["other"] == 30
    assert audit["overlap_union_minutes"] == 90
    assert audit["duplicates_removed"] == 1
    assert audit["daily_category_minutes"]["2026-10-05"]["work"] == 150
    assert "PRIVATE" not in json.dumps([summary.model_dump(mode="json"), audit])


def test_dst_empty_window_and_unknown_type():
    summary, _ = aggregate([], "2026-10-31", "2026-11-06", "America/New_York")
    assert summary.untracked_minutes == 7 * 1440 + 60
    unknown = interval("1", "2026-10-05T01:00:00+08:00", "2026-10-05T02:00:00+08:00", "new")
    summary, audit = aggregate([unknown])
    assert summary.category_minutes["other"] == 60
    assert audit["unknown_type_intervals"] == 1


@pytest.mark.parametrize("change", [{"duration": 1}, {"to": float("nan")}, {"from": True}])
def test_bad_interval_is_rejected_without_values(change):
    item = interval("1", "2026-10-05T01:00:00+08:00", "2026-10-05T02:00:00+08:00")
    item.update(change)
    with pytest.raises(ValueError):
        aggregate([item])


def test_conflicting_duplicate_rejected():
    item = interval("1", "2026-10-05T01:00:00+08:00", "2026-10-05T02:00:00+08:00")
    with pytest.raises(ValueError, match="conflicting duplicate"):
        aggregate([item, dict(item, typeId="sleep")])


def test_hierarchy_rename_new_category_and_leaf_override():
    types = [dict(id="g", name="Renamed", group=True),
             dict(id="a", name="Same", parentId="g"),
             dict(id="b", name="New", parentId="g"),
             dict(id="c", name="Unmapped"),
             dict(id="d", name="Same")]
    resolved, audit = type_mapping(types, {"id:g": "work", "id:a": "learning", "Same": "leisure"})
    assert resolved == {"a": "learning", "b": "work", "c": "other", "d": "leisure"}
    assert next(x for x in audit if x["path"] == "Unmapped")["defaulted"]
    assert next(x for x in audit if x["path"] == "Renamed/New")["mapping_source"] == "stable_id"
    with pytest.raises(ValueError, match="cyclic"):
        type_mapping([dict(id="a", name="A", parentId="a")], {})
    resolved, audit = type_mapping([dict(id="a", name="A", parentId="missing")], {})
    assert resolved == {"a": "other"}
    assert audit[0]["orphaned_parent"] is True


def test_collect_requires_timezone_and_checks_paging(monkeypatch):
    import timex.atimelogger as adapter
    monkeypatch.setattr(adapter, "read_api", lambda *a: {})
    with pytest.raises(ValueError, match="timezone"):
        collect_summary(mapping={})
    calls = []
    def fake(path, payload=None):
        calls.append(path)
        if path == "/api/users/me": return {"timeZone": "Asia/Shanghai"}
        if path == "/api/types": return []
        if path.startswith("/api/intervals"):
            page = len([p for p in calls if p.startswith("/api/intervals")])-1
            return {"number": page, "content": [{"intervals": []}], "last": page == 1}
        if path == "/api/statistics": return {"total": {"info": {"total": 0}}}
        if path == "/api/activities": return {"activities": []}
        raise AssertionError(path)
    monkeypatch.setattr(adapter, "read_api", fake)
    result = collect_summary(mapping={}, period_end=date(2026, 10, 11))
    assert result["time_summary"]["untracked_minutes"] == 10080
    assert result["provenance"]["model_calls"] == 0
    assert len([p for p in calls if p.startswith("/api/intervals")]) == 2


def test_read_error_redacts_credentials_and_private_response(monkeypatch):
    import timex.atimelogger as adapter
    monkeypatch.setenv("ATIMELOGGER_ACCESS_TOKEN", "PRIVATE_TOKEN")
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_TOKEN PRIVATE_ACTIVITY")
    monkeypatch.setattr(adapter.urllib.request, "urlopen", fail)
    with pytest.raises(ValueError) as error:
        adapter.read_api("/api/types")
    assert "PRIVATE" not in str(error.value)
    assert error.value.__suppress_context__
    with pytest.raises(ValueError, match="unsupported"):
        adapter.read_api("/api/delete")
