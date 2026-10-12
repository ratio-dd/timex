from datetime import date

import pytest

from timex.time_data import load_summary


def summary(tmp_path, rows, **kwargs):
    path = tmp_path / 'events.csv'
    path.write_text('start_at,end_at,category\n' + '\n'.join(rows), encoding='utf-8')
    return load_summary(path, period_start=date(2026, 10, 5), period_end=date(2026, 10, 11), **kwargs)


def test_cross_day_timezone_clipping_and_duplicate(tmp_path):
    row = '2026-10-04T15:00:00Z,2026-10-04T17:00:00Z,sleep'
    result = summary(tmp_path, [row, row])
    assert result.category_minutes['sleep'] == 60
    assert result.tracked_minutes == 60
    assert result.untracked_minutes == 7 * 1440 - 60
    assert result.coverage == 60 / (7 * 1440)
    assert any('重复' in note for note in result.notes)


@pytest.mark.parametrize('row', [
    '2026-10-05T01:00:00,2026-10-05T02:00:00,sleep',
    '2026-10-05T01:00:00Z,2026-10-05T01:00:00Z,sleep',
    '2026-10-05T02:00:00Z,2026-10-05T01:00:00Z,sleep',
    ',2026-10-05T01:00:00Z,sleep',
    'bad,2026-10-05T01:00:00Z,sleep',
    '2026-10-05T00:00:00Z,2026-10-05T01:00:00Z,',
    '2026-10-05T00:00:00Z,2026-10-05T01:00:00Z,untracked',
    '2026-10-05T00:00:00Z,2026-10-05T01:00:00Z,sleep,extra',
])
def test_invalid_events_rejected(tmp_path, row):
    with pytest.raises(ValueError):
        summary(tmp_path, [row])


def test_overlaps_rejected_including_different_categories(tmp_path):
    with pytest.raises(ValueError, match='overlapping'):
        summary(tmp_path, ['2026-10-05T00:00:00Z,2026-10-05T02:00:00Z,sleep',
                           '2026-10-05T01:00:00Z,2026-10-05T03:00:00Z,work'])


def test_adjacent_intervals_and_mapping(tmp_path):
    result = summary(tmp_path, ['2026-10-05T00:00:00Z,2026-10-05T01:00:00Z,工作',
                               '2026-10-05T01:00:00Z,2026-10-05T02:00:00Z,unknown'],
                     category_map={'工作': 'work'})
    assert result.category_minutes['work'] == 60
    assert result.category_minutes['other'] == 60
    assert any('未知分类' in note for note in result.notes)


def test_no_records_does_not_infer_leisure(tmp_path):
    result = summary(tmp_path, [])
    assert result.coverage == 0
    assert result.category_minutes['leisure'] == 0
    assert result.untracked_minutes == 10080


def test_dst_period_uses_elapsed_time(tmp_path):
    path = tmp_path / 'events.csv'
    path.write_text('start_at,end_at,category\n2026-11-01T00:00:00-04:00,2026-11-02T00:00:00-05:00,sleep\n')
    result = load_summary(path, period_start=date(2026, 11, 1), period_end=date(2026, 11, 1),
                          timezone_name='America/New_York')
    assert result.tracked_minutes == 1500
    assert result.coverage == 1
