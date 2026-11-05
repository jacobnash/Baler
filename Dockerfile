# openhay (the Rust-core Haystack library haystack_api.py speaks the wire
# protocol through) has no PyPI release yet, so build its abi3 wheel from
# source in a throwaway Rust stage. bookworm here so the wheel's glibc
# floor is no newer than the runtime image's.
FROM rust:1-slim-bookworm AS openhay
ARG OPENHAY_REF=claude/gracious-lamport-2xeam9
RUN apt-get update && apt-get install -y --no-install-recommends git python3 python3-pip \
    && pip3 install --break-system-packages --no-cache-dir "maturin>=1.5,<2" \
    && git clone --depth 1 --branch "$OPENHAY_REF" https://github.com/jacobnash/openhay /openhay \
    && cd /openhay/crates/openhay-py \
    && maturin build --release --interpreter python3 --out /wheels

FROM python:3.12-slim

WORKDIR /app
COPY --from=openhay /wheels /wheels
RUN pip install --no-cache-dir /wheels/openhay-*.whl
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e ".[validate]"

# No ENTRYPOINT/CMD - each daemon service in docker-compose.yml supplies its
# own `command: python -m timberdoodle.<module>` against this one image.
# `validate` (pyshacl) is included here because validate_api.py runs as one
# of the daemons sharing this same image - the extras split in pyproject.toml
# is about the bare-metal/local dev install, not this containerized one.
