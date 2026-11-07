# Baler Notebooks (#90): JupyterLab with pandas (from scipy-notebook),
# phable (the Haystack client recommended in docs-site/pages/code/decisions.mdx,
# #95) and baler_fn, plus the sample function package. Built by the
# `jupyter` service in docker-compose.yml (profile "notebook").
#
# The base is the Jupyter Docker Stacks scipy-notebook; its images moved
# from Docker Hub (jupyter/scipy-notebook, frozen since 2023) to quay.io.
# Pinned to a dated tag: bump it on purpose, not by accident.
# BASE_IMAGE is overridable for a smaller local test build (minimal-notebook
# plus the pandas phable[pandas] pulls in); the default is what ships.
ARG BASE_IMAGE=quay.io/jupyter/scipy-notebook:2026-10-19
FROM ${BASE_IMAGE}

# phable is pure Python (no Rust toolchain, unlike openhay); its pandas
# and pyarrow extras are what Grid.to_pandas() needs.
RUN pip install --no-cache-dir "phable[pandas,pyarrow]==0.1.29" "requests>=2.31"

# baler_fn and the one Baler module it wraps (haystack_client.py), not the
# whole platform: --no-deps keeps Postgres/RDF/OpenTelemetry out of the
# notebook. requests (installed above) is all haystack_client needs.
COPY --chown=${NB_UID}:${NB_GID} pyproject.toml /tmp/baler/pyproject.toml
COPY --chown=${NB_UID}:${NB_GID} src/baler/haystack_client.py /tmp/baler/src/baler/haystack_client.py
COPY --chown=${NB_UID}:${NB_GID} src/baler_fn /tmp/baler/src/baler_fn
COPY --chown=${NB_UID}:${NB_GID} templates/function-project /tmp/baler-sample
RUN pip install --no-cache-dir --no-deps /tmp/baler /tmp/baler-sample \
    && rm -rf /tmp/baler /tmp/baler-sample

# Starter notebooks. /home/jovyan/work is the `notebooks` named volume, and
# Docker seeds an empty named volume from the image, so these appear on
# first start and are never overwritten afterwards.
COPY --chown=${NB_UID}:${NB_GID} notebooks/ /home/jovyan/work/

COPY --chmod=755 docker/notebook-start.sh /usr/local/bin/baler-notebook-start.sh
CMD ["/usr/local/bin/baler-notebook-start.sh"]
