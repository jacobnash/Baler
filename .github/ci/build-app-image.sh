#!/usr/bin/env bash
# CI only (#27): build the one app image every compose service runs, once,
# with a GitHub Actions layer cache, and tag it under each service's compose
# image name so `docker compose up --no-build` uses it.
#
# Why not `docker compose up --build` with cache_from/cache_to: compose builds
# each of the ~13 `build: .` services as its own image, so even on a full
# cache hit the docker-container builder exports and loads 13 identical
# tarballs (~15-40 s of a ~50-130 s build step). One build, one --load.
# The third-party images are pulled at the same time.
#
# Needs a docker-container buildx builder and the Actions runtime token
# (docker/setup-buildx-action, crazy-max/ghaction-github-runtime in ci.yml).
#
# Usage: build-app-image.sh [--write-cache]
#   --write-cache  also export the layer cache (one job per run is enough:
#                  the image is the same in every job)
#
# Cache: BuildKit keys each layer by its inputs (base image digest, the
# openhay commit the ADD resolves to, pyproject.toml, src/), so a hit is
# always the right layer; `scope` only names the cache entry. mode=max keeps
# the openhay Rust stage's layers too, which the final image doesn't contain.
# PR runs read main's cache; what a PR run writes is visible to that PR only.
set -euo pipefail

cache_to=()
if [ "${1:-}" = "--write-cache" ]; then
  cache_to=(--cache-to "type=gha,scope=baler-app,mode=max,ignore-error=true")
fi

# Docker Hub sometimes answers a manifest HEAD with an error for a few
# seconds (it failed main's integration (2) on 4880f5a resolving
# rust:1-slim-bookworm, which skipped that commit's deploy). Retry a few
# times with backoff; a real breakage still fails after the last attempt.
retry() {
  local attempt=1 max=4 delay=5
  until "$@"; do
    if [ "$attempt" -ge "$max" ]; then
      echo "::error::failed after $max attempts: $*"
      return 1
    fi
    echo "::warning::attempt $attempt failed, retrying in ${delay}s: $*"
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
}

retry docker compose pull --quiet --ignore-buildable &
pull=$!

retry docker buildx build --load --provenance=false -t baler-app:ci \
  --cache-from type=gha,scope=baler-app "${cache_to[@]}" .

wait "$pull"

# Compose names a build-only service's image <project>-<service>.
docker compose config --format json | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
for name, svc in cfg["services"].items():
    if "build" in svc:
        print(svc.get("image") or cfg["name"] + "-" + name)
' | while read -r image; do
  docker tag baler-app:ci "$image"
  echo "tagged $image"
done
