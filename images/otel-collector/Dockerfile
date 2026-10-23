# syntax=docker/dockerfile:1
# The OTel collector with vigil's config baked in. Baked, not bind-mounted,
# so it never depends on where the checkout lives (docs/runbook.md).
FROM otel/opentelemetry-collector-contrib:0.162.0@sha256:39923a8e431bd1f57be82411999d389fcfe40857492e4365456d97a4c1f74be6
COPY --from=vigil_configs otel-collector.yaml /etc/otelcol/config.yaml
CMD ["--config=/etc/otelcol/config.yaml"]
