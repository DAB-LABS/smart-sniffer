FROM debian:trixie-slim

ARG RELEASE=0.4.28
ENV BIN_NAME=smart-agent

ENV DEBIAN_FRONTEND=noninteractive
ENV PORT=9099
ENV TOKEN=
ENV INTERVAL=60

RUN mkdir /app
WORKDIR /app

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
    update-ca-certificates && \
    curl -sSfL -o ${BIN_NAME} https://github.com/DAB-LABS/smart-sniffer/releases/download/v${RELEASE}/smartha-agent-linux-amd64 && \
    chmod +x ${BIN_NAME}

RUN cat <<'EOF' > config.yaml.template
port: ${PORT}
token: ${TOKEN}
scan_interval: ${INTERVAL}s
EOF

CMD ["sh", "-c", "envsubst < /app/config.yaml.template > /app/config.yaml && exec /app/smart-agent"]
