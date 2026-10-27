# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# The OTel collector with vigil's config baked in. Baked, not bind-mounted,
# so it never depends on where the checkout lives (docs/runbook.md).
FROM otel/opentelemetry-collector-contrib:0.162.0@sha256:39923a8e431bd1f57be82411999d389fcfe40857492e4365456d97a4c1f74be6
COPY --from=vigil_configs otel-collector.yaml /etc/otelcol/config.yaml
CMD ["--config=/etc/otelcol/config.yaml"]
