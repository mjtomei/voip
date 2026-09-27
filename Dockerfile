FROM node:22-bookworm-slim AS node
FROM ubuntu:24.04 AS base
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip build-essential cmake pkg-config libssl-dev \
    libpulse-dev pulseaudio-utils pipewire pipewire-pulse wireplumber dbus \
    tmux curl ca-certificates git patch espeak-ng libespeak-ng1 tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
WORKDIR /opt/voip
COPY requirements-voice.txt .
RUN python3 -m venv .tools/voice-venv && .tools/voice-venv/bin/pip install --no-cache-dir -r requirements-voice.txt
FROM base AS stt
COPY scripts/install-stt.py scripts/install-stt.py
RUN python3 scripts/install-stt.py

FROM base AS runtime
COPY . .
RUN python3 scripts/build-baresip.py && npm ci --prefix twilio && \
    chmod +x bin/voip container/entrypoint.sh && ln -s /opt/voip/bin/voip /usr/local/bin/voip
RUN useradd --uid 10001 --create-home voipapp && mkdir -p /opt/voip/state && chown voipapp:voipapp /opt/voip/state
USER voipapp
ENV HOME=/home/voipapp
ENV PATH="/opt/voip/.tools/voice-venv/bin:/opt/voip/.tools/bin:${PATH}" \
    VOIP_TOOLS_DIR=/opt/voip/.tools PYTHONDONTWRITEBYTECODE=1 \
    XDG_RUNTIME_DIR=/tmp/voip-runtime PULSE_SERVER=unix:/tmp/voip-runtime/pulse/native
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/voip/container/entrypoint.sh"]
CMD ["voip", "service", "run"]

FROM runtime AS test
COPY --from=stt /opt/voip/.tools/bin/whisper-cli /opt/voip/.tools/bin/whisper-cli
COPY --from=stt /opt/voip/.tools/share/whisper /opt/voip/.tools/share/whisper
CMD ["python3", "scripts/test-clean-install.py"]
