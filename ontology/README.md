# Vendored ontology data

`Brick-only.ttl` — [Brick Schema](https://brickschema.org) release
`v1.4.4`, downloaded from
`https://github.com/BrickSchema/Brick/releases/download/v1.4.4/Brick-only.ttl`
on 2026-08-25. BSD-3-Clause licensed (Brick Consortium, Inc.) — see
[Brick's LICENSE](https://github.com/BrickSchema/Brick/blob/v1.4.4/LICENSE).

Chosen over the release's other three Turtle variants (`Brick.ttl`,
`Brick+extensions.ttl`, `Brick+imports.ttl`) because it carries the same
embedded SHACL shapes (`sh:NodeShape`, one per class) without the
`+extensions`/`+imports` bloat this project doesn't need.

Pinned, not auto-fetched on install — same "hand-vendor, verify, commit"
pattern as `tests/fixtures/brick_subset.ttl`. Update manually if/when the
project bumps its Brick version.

Loaded by `src/baler/shacl_validate.py` (`load_shapes`) to
structurally validate the live entity graph against Brick's own SHACL
shapes — see `validate_api.py` (`POST /validate`).

`cx-expected-points.ttl` — Baler's own SHACL shapes (not vendored): the
points commissioning expects on an AHU, VAV, FCU, chiller, boiler and
pump (Baler#37). Valid SHACL for pyshacl as-is; read by
`src/baler/commissioning/templates.py`, which evaluates the same shapes
subclass-aware and with a Haystack-marker fallback. Edit to change what
"complete" means - see the conventions at the top of the file.
