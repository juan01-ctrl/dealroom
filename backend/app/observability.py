"""Opt-in Langfuse observations with explicitly selected application metadata."""

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

from langfuse import Langfuse
from langfuse.span_filter import is_langfuse_span
from opentelemetry.sdk.trace import TracerProvider

from .configuration import ConfigurationError

logger = logging.getLogger(__name__)
_client = ContextVar("langfuse_client", default=None)


def langfuse_config(env):
    if env.get("ENABLE_LANGFUSE") != "1":
        return None
    public_key = env.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = env.get("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        raise ConfigurationError("Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to enable Langfuse.")
    try:
        url = urlsplit(env.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"))
        if (
            not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
            or (
                url.scheme != "https"
                and not (
                    url.scheme == "http"
                    and url.hostname in {"localhost", "127.0.0.1", "::1"}
                    and env.get("APP_ENV", "local") == "local"
                )
            )
        ):
            raise ValueError
        _ = url.port
    except ValueError:
        raise ConfigurationError(
            "Set a valid HTTPS LANGFUSE_BASE_URL (local loopback may use HTTP)."
        ) from None
    return {
        "public_key": public_key,
        "secret_key": secret_key,
        "base_url": url.geturl().rstrip("/"),
        "environment": env.get("LANGFUSE_TRACING_ENVIRONMENT") or env.get("APP_ENV", "local"),
        "release": env.get("RAILWAY_GIT_COMMIT_SHA") or env.get("LANGFUSE_RELEASE"),
    }


@contextmanager
def use_langfuse(client):
    token = _client.set(client)
    try:
        yield
    finally:
        _client.reset(token)


def _warn(exc):
    logger.warning("Langfuse instrumentation unavailable (%s)", type(exc).__name__)


@contextmanager
def configured_langfuse():
    config = langfuse_config(os.environ)
    client = None
    if config is not None:
        try:
            client = Langfuse(
                **config,
                tracer_provider=TracerProvider(),
                should_export_span=lambda span: (
                    is_langfuse_span(span) and span.name.startswith("dealroom.")
                ),
                timeout=3,
                sample_rate=1.0,
            )
        except Exception as exc:  # noqa: BLE001 - export must not affect job execution
            _warn(exc)
    with use_langfuse(client):
        try:
            yield
        finally:
            if client is not None:
                try:
                    client.shutdown()
                except Exception as exc:  # noqa: BLE001 - export must not affect exit
                    _warn(exc)


class Observation:
    def __init__(self, span=None):
        self._span = span

    @property
    def trace_id(self):
        return self._span.trace_id if self._span is not None else None

    def update(self, **fields):
        if self._span is not None:
            try:
                self._span.update(**fields)
            except Exception as exc:  # noqa: BLE001 - telemetry must not affect business state
                _warn(exc)

    def score(self, name, passed):
        if self._span is not None:
            try:
                self._span.score(name=name, value=int(passed), data_type="BOOLEAN")
            except Exception as exc:  # noqa: BLE001 - telemetry must not affect evaluation outcomes
                _warn(exc)


@contextmanager
def observe(name, *, as_type="span", **fields):
    client = _client.get()
    context = None
    span = None
    if client is not None:
        try:
            context = client.start_as_current_observation(
                name=f"dealroom.{name}", as_type=as_type, **fields
            )
            span = context.__enter__()
        except Exception as exc:  # noqa: BLE001 - telemetry must not affect job execution
            context = None
            _warn(exc)
    observation = Observation(span)
    try:
        yield observation
    except Exception as exc:
        observation.update(level="ERROR", status_message=type(exc).__name__)
        raise
    finally:
        if context is not None:
            try:
                # Never forward exceptions with provider text or private content to OpenTelemetry.
                context.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - telemetry must not affect job execution
                _warn(exc)
