"""vigild's own spans (B23): one trace per session.

`vigil.session` is the root; `vigil.route`, `vigil.spawn`, `vigil.publish`
nest under it, and the worker's Claude Code spans join the same trace through
TRACEPARENT. The filter attributes go on every span. Phoenix groups by the
OpenInference `session.id` attribute and the `openinference.project.name`
resource attribute.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Span, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

Attr = str | int | float | bool


@dataclass
class SessionTrace:
    root: Span
    attrs: dict[str, Attr] = field(default_factory=dict)

    @property
    def traceparent(self) -> str:
        ctx = self.root.get_span_context()
        return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01"

    @property
    def trace_id(self) -> str:
        return f"{self.root.get_span_context().trace_id:032x}"

    def set(self, key: str, value: Attr) -> None:
        self.attrs[key] = value
        self.root.set_attribute(key, value)


class Telemetry:
    def __init__(self, endpoint: str | None = None, exporter: SpanExporter | None = None) -> None:
        resource = Resource.create(
            {"service.name": "vigild", "openinference.project.name": "vigil"}
        )
        self.provider = TracerProvider(resource=resource)
        if exporter is None and endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=5)
        if exporter is not None:
            self.provider.add_span_processor(BatchSpanProcessor(exporter))
        self.tracer = self.provider.get_tracer("vigild")

    @contextmanager
    def session(self, session_id: str, attrs: Mapping[str, Attr]) -> Iterator[SessionTrace]:
        base: dict[str, Attr] = {"session.id": session_id, **attrs}
        root = self.tracer.start_span(
            "vigil.session", attributes={**base, "openinference.span.kind": "AGENT"}
        )
        st = SessionTrace(root, dict(base))  # type: ignore[arg-type]
        try:
            yield st
        finally:
            root.end()

    @contextmanager
    def span(self, st: SessionTrace, name: str, attrs: Mapping[str, Any]) -> Iterator[Span]:
        ctx = trace.set_span_in_context(st.root)
        with self.tracer.start_as_current_span(
            name, context=ctx, attributes={**st.attrs, **attrs, "openinference.span.kind": "CHAIN"}
        ) as s:
            yield s  # type: ignore[misc]

    def flush(self) -> None:
        self.provider.force_flush(5000)
