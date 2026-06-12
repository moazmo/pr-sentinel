# Multi-stage build: deps compiled in a full image, runtime stays slim.
# Released tags reference the prebuilt image on GHCR (see .github/workflows/release.yml)
# so users never pay this build at PR time.
FROM python:3.14-slim@sha256:d7a925f9eb9639a93e455b9f12c167569358818c0f62b51b88edbc8fcf34c421 AS builder

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.14-slim@sha256:d7a925f9eb9639a93e455b9f12c167569358818c0f62b51b88edbc8fcf34c421

COPY --from=builder /install /usr/local

# GitHub Actions runs Docker actions as root in the runner's workspace;
# no checkout is required — PR Sentinel talks to the API only.
ENTRYPOINT ["pr-sentinel"]
