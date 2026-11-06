"""
Commissioning (Cx) - Baler#32, plan in todo/commissioning-plan.md.

Verifies what Baler already knows about a site (the Brick graph, its
Haystack/Xeto reading, point history) against what commissioning expects
of it, as dated, reproducible runs inside a per-project, per-phase
lifecycle. Slice 1 (#37): scoped inventory, SHACL expected-point
templates (ontology/cx-expected-points.ttl), rung-1 existence, and
projects/phases/runs persisted in cx_* tables. Served by
baler.commissioning_api on :8009 (gateway /cx/).

Every graph read goes through baler.scope (inside the SPARQL), every
history read through timeseries' shared batch helpers, and every
human/agent action lands in cx_events.
"""
