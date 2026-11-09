# syntax=docker/dockerfile:1.7
# (1.7 for ADD of a git URL - see the openhay stage.)

# openhay (the Rust-core Haystack library haystack_api.py speaks the wire
# protocol through) has no PyPI release yet, so build its abi3 wheel from
# source in a throwaway Rust stage. bookworm here so the wheel's glibc
# floor is no newer than the runtime image's.
FROM rust:1-slim-bookworm AS openhay
# Pinned to an openhay commit; bump together with pyproject.toml's haystack extra.
ARG OPENHAY_REF=dd6fe357cba3e1de694dec3134f2c569ebaf9884
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip \
    && pip3 install --break-system-packages --no-cache-dir "maturin>=1.5,<2"
# BuildKit resolves the ref to a commit and caches this layer by that commit:
# a rebuild reuses the wheel until openhay actually moves, and a moved ref is
# always picked up. (A `git clone` RUN was cached by its text, so it served a
# stale openhay; the GitHub API workaround that replaced it was unauthenticated
# and rate-limited to 60 requests an hour - #23.)
ADD https://github.com/jacobnash/openhay.git#${OPENHAY_REF} /openhay
# Cargo's registry and build dir persist between builds on the same host, so
# a moved ref recompiles only what changed.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/openhay/target \
    cd /openhay/crates/openhay-py \
    && maturin build --release --interpreter python3 --out /wheels

FROM python:3.12-slim

WORKDIR /app
COPY --from=openhay /wheels /wheels
RUN pip install --no-cache-dir /wheels/openhay-*.whl
# Dependencies before code: this layer depends only on pyproject.toml, so a
# .py change doesn't reinstall every dependency (#23). The package is
# installed editable against a placeholder; the real src/ lands on top.
COPY pyproject.toml ./
RUN mkdir -p src/baler && touch src/baler/__init__.py \
    && pip install --no-cache-dir -e ".[validate,cx-gl36,cx-spec,llm,export]"
COPY src/ ./src/

# No ENTRYPOINT/CMD - each daemon service in docker-compose.yml supplies its
# own `command: python -m baler.<module>` against this one image.
# `validate` (pyshacl) is included here because validate_api.py runs as one
# of the daemons sharing this same image - the extras split in pyproject.toml
# is about the bare-metal/local dev install, not this containerized one.
# `cx-gl36` (pandas, ~75 MB) lets a commissioning project enable the vendored
# ConStrain items (settings.gl36) live; without it that PATCH answers 400.
# `export` (pyarrow) lets `python -m baler.export` run in a container too.
# `cx-spec` (openpyxl) reads XLSX schedules; `llm` (anthropic) is the spec
# PDF extraction and the commissioning agent in commissioning_api.
