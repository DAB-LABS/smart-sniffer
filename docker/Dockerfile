FROM debian:trixie-slim

ARG TARGETARCH
ARG VERSION=latest
ENV BIN_NAME=smart-agent

ENV DEBIAN_FRONTEND=noninteractive
ENV PORT=9099
ENV TOKEN=
ENV INTERVAL=60s

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt update && \
    apt install -y --no-install-recommends \
    bash \
    tzdata \
    smartmontools \
    curl \
    gettext \
    ca-certificates && \
    update-ca-certificates

RUN mkdir /app
WORKDIR /app

COPY config.yaml.template entrypoint.sh download.sh /app/

RUN if [ "$TARGETARCH" != "amd64" ] && [ "$TARGETARCH" != "arm64" ]; then \
    echo "❌ Error: Unsupported architecture: $TARGETARCH. Only amd64 and arm64 are allowed."; \
    exit 1; \
else \
    bash download.sh ${BIN_NAME} ${TARGETARCH} ${VERSION}; \
    rm download.sh; \
    fi

ENTRYPOINT /app/entrypoint.sh
