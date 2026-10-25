# Disposable dependency/fixture validation only; never copies account state.
ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONUTF8=1 LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ca-certificates xvfb openbox x11-utils x11vnc \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/rewards
COPY requirements.txt ./
RUN python3 -m venv .venv \
    && .venv/bin/python -m pip install --no-cache-dir -r requirements.txt \
    && .venv/bin/python -m playwright install --with-deps msedge
RUN apt-get update && apt-get install -y --no-install-recommends \
    xauth systemd dbus-x11 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 rewards \
    && chown -R rewards:rewards /opt/rewards
COPY --chown=rewards:rewards . .
USER rewards
RUN bash Rewards.sh setup && bash Rewards.sh doctor
ENV REWARDS_LINUX_RUNTIME_TESTS=1
CMD [".venv/bin/python", "-m", "unittest", "discover", "-s", "tests", "-v"]
