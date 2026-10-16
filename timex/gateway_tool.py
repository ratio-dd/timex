"""Read-only Gateway Lambda tool over the existing synthetic CSV aggregator."""
from datetime import date, timedelta
import os
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from .models import Contract
from .time_data import load_summary


class SummaryQuery(Contract):
    period_end: date
    time_window_days: Literal[7, 30]
    timezone: str = 'Asia/Shanghai'


def lambda_handler(event, context):
    custom = getattr(getattr(context, 'client_context', None), 'custom', {}) or {}
    if (custom.get('bedrockAgentCoreToolName') != 'time-summary___get_time_summary'
            or not os.environ.get('TIMEX_GATEWAY_ID')
            or custom.get('bedrockAgentCoreGatewayId') != os.environ['TIMEX_GATEWAY_ID']):
        return {'error': 'invalid_tool_context'}
    try:
        query = SummaryQuery.model_validate(event)
        summary = load_summary(Path(__file__).resolve().parents[1] / 'examples/demo_events.csv',
                               period_start=query.period_end - timedelta(days=query.time_window_days - 1),
                               period_end=query.period_end, timezone_name=query.timezone)
    except (ValidationError, ValueError, OSError):
        return {'error': 'invalid_summary_query'}
    summary.notes.append('Gateway 演示合成数据；不是私人 aTimeLogger 账户数据。')
    return {'summary': summary.model_dump(mode='json'), 'source': 'synthetic_csv', 'read_only': True}
