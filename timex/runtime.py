"""Thin HTTP adapter: deployment settings are trusted, invocation data is not."""
import json
import os
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from .config import Limits, ModelSettings
from .graph import run_council
from .models import Contract, DeliberationRequest


class RuntimeInvocation(Contract):
    request: DeliberationRequest
    live: bool = Field(default=False, strict=True)
    summary_source: Literal['request', 'gateway'] = 'request'
    use_memory: bool = Field(default=False, strict=True)

    @model_validator(mode='after')
    def server_preference_only(self):
        if self.request.preference_max_minutes is not None:
            raise ValueError('Memory preference must come from the server')
        return self


def _load_api_key():
    """Fetch only the deployment-selected secret, never a caller-selected ARN."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    secret_arn = os.environ.get("TIMEX_DEEPSEEK_SECRET_ARN")
    if not secret_arn:
        raise ValueError("missing server credential")
    import boto3

    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    value = json.loads(response["SecretString"])["OPENAI_API_KEY"]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid server credential")
    os.environ["OPENAI_API_KEY"] = value


def handle_invocation(payload):
    """Return the existing council JSON and an HTTP status; never load .env."""
    if isinstance(payload, dict) and 'operation' in payload:
        from .memory import MemoryCommand, execute
        try:
            command = MemoryCommand.model_validate(payload)
        except ValidationError:
            return {"status": "error", "errors": ["invalid_memory_command"]}, 400
        try:
            return execute(command), 200
        except Exception:
            return {"status": "error", "errors": ["memory_unavailable"]}, 503
    try:
        invocation = RuntimeInvocation.model_validate(payload)
    except ValidationError:
        return {"status": "error", "errors": ["invalid_request"]}, 400
    if invocation.live and os.environ.get("TIMEX_ALLOW_LIVE") != "1":
        return {"status": "error", "errors": ["live_disabled"]}, 403
    try:
        # A dry run stays fake even when this server has a paid provider configured.
        settings = ModelSettings.from_env(provider=None if invocation.live else "fake")
        settings.authorize(invocation.live)
        limits = Limits.from_env()
    except (ValueError, TypeError):
        return {"status": "error", "errors": ["invalid_server_configuration"]}, 503
    if invocation.live:
        try:
            _load_api_key()
        except Exception:
            # AWS/SDK exceptions can contain credential details or private bodies.
            return {"status": "error", "errors": ["credential_unavailable"]}, 503
    context = {}
    if invocation.summary_source == 'gateway':
        try:
            from .gateway import fetch_summary
            summary, provenance = fetch_summary(invocation.request)
            invocation.request = DeliberationRequest.model_validate(
                invocation.request.model_dump() | {'time_summary': summary})
            context['time_summary'] = provenance
        except Exception:
            return {"status": "error", "errors": ["gateway_unavailable"]}, 503
    if invocation.use_memory:
        try:
            from .memory import apply_preference
            invocation.request, context['memory'] = apply_preference(invocation.request)
        except Exception:
            return {"status": "error", "errors": ["memory_unavailable"]}, 503
    try:
        result = run_council(invocation.request, settings=settings, limits=limits, live=invocation.live)
    except Exception:
        return {"status": "error", "errors": ["execution_failed"]}, 500
    # HTTP success is distinct from a complete experiment: preserve partial results.
    body = result.model_dump(mode="json")
    if context:
        body['context_sources'] = context
    return body, 200


def create_app():
    from bedrock_agentcore import BedrockAgentCoreApp
    from starlette.responses import JSONResponse

    app = BedrockAgentCoreApp(debug=False)

    @app.entrypoint
    def invoke(payload):
        body, status = handle_invocation(payload)
        return JSONResponse(body, status_code=status)

    return app
