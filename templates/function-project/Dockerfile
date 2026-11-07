# Your project's container: the Baler notebook image with this package
# installed on top. compose.baler-sample.yml builds it; it works for both
# a notebook (the image's default command) and a scheduled worker (a
# `command:` override).
#
# FROM the image the base docker-compose.yml's `jupyter` service builds.
# Through compose, `additional_contexts` builds that one first; with plain
# `docker build`, run `docker compose --profile notebook build jupyter` once.
FROM baler-notebook:local

# The source stays at /opt/baler-project so `python -m baler_fn check
# /opt/baler-project/baler.toml` can compare the manifest with what got
# installed.
COPY --chown=${NB_UID}:${NB_GID} . /opt/baler-project
RUN pip install --no-cache-dir /opt/baler-project
