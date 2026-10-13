"""Safe diagnostics: schema locations and fixed codes, never rejected values."""
from pydantic import ValidationError

from .models import ValidationIssue


SEMANTIC_ERRORS = {
    "plan missing time semantics": ("time_semantics_required", "proposed_plan"),
    "plan ceiling below default": ("ceiling_below_default", "proposed_plan.maximum_minutes"),
    "plan missing adjustment trigger": ("adjustment_trigger_required", "proposed_plan.adjustment_conditions"),
    "evidence references are required": ("evidence_required", "evidence_refs"),
    "unknown evidence reference": ("unknown_evidence_reference", "evidence_refs"),
    "proposal exceeds requested investment": ("investment_exceeded", "suggested_minutes"),
    "plan exceeds requested investment": ("investment_exceeded", "proposed_plan"),
    "plan omitted user constraints": ("constraints_omitted", "proposed_plan.conditions"),
    "untraceable peer response": ("peer_quote_mismatch", "responses_to_others"),
    "revision requires explanation": ("explanation_required", "change_explanation"),
    "synthesis concealed disagreement": ("disagreement_omitted", "unresolved_conflicts"),
}
SCHEMA_CODES = {
    "json_invalid", "missing", "extra_forbidden", "model_type", "model_attributes_type",
    "list_type", "dict_type", "string_type", "string_too_short", "string_too_long",
    "int_type", "int_parsing", "int_from_float", "finite_number", "literal_error",
    "greater_than_equal", "less_than_equal", "too_short", "too_long", "value_error",
}


def evidence_paths(request):
    """Enumerate selectable field paths, not values or array indices."""
    paths = []

    def walk(value, prefix=""):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.append(path)
            if isinstance(child, dict):
                walk(child, path)
    walk(request)
    return paths


def safe_issues(error, schema):
    if not isinstance(error, ValidationError):
        code, path = SEMANTIC_ERRORS.get(str(error), ("invalid_contract", "$"))
        return [ValidationIssue(code=code, path=path)]

    # A validation location can contain arbitrary model-generated dictionary keys.
    # Only schema-defined property names may appear in diagnostics or repair prompts.
    allowed = set()

    def visit(value):
        if isinstance(value, dict):
            allowed.update(value.get("properties", {}))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema.model_json_schema())
    issues = []
    for item in error.errors(include_input=False, include_context=False, include_url=False)[:10]:
        path = ".".join("[]" if isinstance(part, int) else part if part in allowed else "<unknown>"
                        for part in item["loc"]) or "$"
        code = item["type"] if item["type"] in SCHEMA_CODES else "invalid_value"
        issues.append(ValidationIssue(code=code, path=path))
    return issues
