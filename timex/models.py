from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["career", "curiosity", "life", "health"]
ROLES: tuple[Role, ...] = ("career", "curiosity", "life", "health")
CATEGORIES = ("work", "learning", "leisure", "sleep", "exercise", "social", "other", "untracked")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TimeSummary(Contract):
    period_start: date
    period_end: date
    timezone: str = "Asia/Shanghai"
    category_minutes: dict[str, float]
    tracked_minutes: float = Field(ge=0)
    untracked_minutes: float = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def derive_missing_fields(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            categories = dict(data.get("category_minutes", {}))
            if "untracked_minutes" in data:
                categories.setdefault("untracked", data["untracked_minutes"])
            data["category_minutes"] = categories
            if "coverage" not in data and "tracked_minutes" in data and "untracked_minutes" in data:
                tracked = float(data["tracked_minutes"])
                total = tracked + float(data["untracked_minutes"])
                data["coverage"] = tracked / total if total else 0
        return data

    @model_validator(mode="after")
    def check_totals(self):
        if self.period_end < self.period_start:
            raise ValueError("period_end precedes period_start")
        if any(key not in CATEGORIES or value < 0 for key, value in self.category_minutes.items()):
            raise ValueError("invalid category or negative minutes")
        recorded = sum(value for key, value in self.category_minutes.items() if key != "untracked")
        if abs(recorded - self.tracked_minutes) > 0.01:
            raise ValueError("category totals disagree with tracked_minutes")
        if abs(self.category_minutes.get("untracked", 0) - self.untracked_minutes) > 0.01:
            raise ValueError("untracked category disagrees with untracked_minutes")
        total = self.tracked_minutes + self.untracked_minutes
        try:
            zone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("unknown timezone") from error
        start = datetime.combine(self.period_start, time.min, zone).astimezone(timezone.utc)
        end = datetime.combine(self.period_end + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        if abs(total - (end - start).total_seconds() / 60) > 0.01:
            raise ValueError("summary totals must cover the full selected period")
        expected = self.tracked_minutes / total if total else 0
        if abs(expected - self.coverage) > 0.0001:
            raise ValueError("coverage disagrees with totals")
        return self


class DeliberationRequest(Contract):
    question: str = Field(min_length=1, max_length=2000)
    time_window_days: Literal[7, 30] = 7
    requested_minutes: int = Field(default=180, ge=0, le=1440)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    time_summary: TimeSummary

    @model_validator(mode="after")
    def check_window(self):
        days = (self.time_summary.period_end - self.time_summary.period_start).days + 1
        if days != self.time_window_days:
            raise ValueError("summary period must match time_window_days")
        if any(len(text) > 1000 for text in self.constraints):
            raise ValueError("constraint too long")
        return self


class InitialPosition(Contract):
    position: str
    suggested_minutes: int = Field(ge=0, le=1440)
    reasons: list[str]
    concerns: list[str]
    acceptable_conditions: list[str]
    evidence_refs: list[str]
    unknowns: list[str]


class Response(Contract):
    role: Role
    cited_position: str
    response: str


class RevisedPosition(Contract):
    revised_position: str
    suggested_minutes: int = Field(ge=0, le=1440)
    responses_to_others: list[Response] = Field(min_length=1)
    concessions: list[str]
    remaining_objections: list[str]
    change_explanation: str


class Plan(Contract):
    text: str
    suggested_minutes: int = Field(ge=0, le=1440)
    conditions: list[str]


class Synthesis(Contract):
    shared_ground: list[str]
    proposed_plan: Plan
    unresolved_conflicts: list[str]
    requires_user_decision: Literal[True] = True
    evidence_limitations: list[str]


class Usage(Contract):
    provider: str = "fake"
    model: str | None = None
    model_calls: int = 0
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_usd: float | None = Field(default=0.0, ge=0)
    budget_estimated_usd: float = Field(default=0.0, ge=0)
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None
    elapsed_seconds: float = 0.0
    calls: list["CallUsage"] = Field(default_factory=list)


class CallUsage(Contract):
    stage: str
    role: str
    reported_model: str | None = None
    status: str = "pending"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_input_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    budget_estimated_usd: float = Field(ge=0)
    elapsed_seconds: float = 0.0


class SingleAdvice(Contract):
    considerations: dict[Role, str]
    proposed_plan: Plan
    tradeoffs: list[str]
    evidence_refs: list[str]
    unknowns: list[str]
    requires_user_decision: Literal[True] = True

    @model_validator(mode="after")
    def all_interests(self):
        if set(self.considerations) != set(ROLES):
            raise ValueError("baseline must consider all four interests")
        return self


class CouncilResult(Contract):
    question: str
    round1: dict[Role, InitialPosition] = Field(default_factory=dict)
    round2: dict[Role, RevisedPosition] = Field(default_factory=dict)
    shared_ground: list[str] = Field(default_factory=list)
    proposed_plan: Plan | None = None
    unresolved_conflicts: list[str] = Field(default_factory=list)
    requires_user_decision: Literal[True] = True
    evidence_limitations: list[str] = Field(default_factory=list)
    usage: Usage
    status: Literal["complete", "partial"]
    errors: list[str] = Field(default_factory=list)
