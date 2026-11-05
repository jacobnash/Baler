"""
Canonical shapes for the JSON records that flow between the JSON-file
stores (rules.json, derivations.json, rules/*.yaml), the HTTP APIs, and
the modules that build/read them. Plain TypedDicts, not dataclasses or
pydantic models - these objects' entire life is JSON in, JSON out, so a
TypedDict costs nothing at runtime (dicts stay dicts, json.dump/json.load
need zero conversion code) while still letting mypy catch a typo'd or
missing key instead of a runtime KeyError three services away from where
it was introduced.

Field names/requiredness mirror the OpenAPI specs' own schemas
(derivation-api-openapi.yaml's Derivation/CreateDerivationRequest,
fault-api-openapi.yaml's Webhook/CreateWebhookRequest) rather than
re-deriving them - those are the documented contract, this is that same
contract checked by mypy instead of only by test_*_openapi.py's live
assert_matches_schema calls.

Every *Request TypedDict allows extra keys through at the dict level
(TypedDict doesn't enforce "no extra keys" - only mypy's structural
checks at typed call sites do) - this deliberately preserves
derivation_api.py's existing pass-through-unknown-fields behavior
(`**{k: v for k, v in body.items() if k not in reserved}`), not a gap.
"""

from typing import TypedDict


class MappingRule(TypedDict):
    """One entry from rules/haystack_to_brick.yaml or
    rules/haystack_equip_to_brick.yaml - see mapping.classify_point."""

    tags: list[str]
    brick_class: str


class _CreateDerivationRequestRequired(TypedDict):
    name: str
    fn_source: str
    test_cases: list[dict]


class CreateDerivationRequest(_CreateDerivationRequestRequired, total=False):
    """POST /derivations request body. kind selects which of the
    formula-only vs. rollup-only fields below apply - mirroring
    CreateDerivationRequest's own flat-with-description-only-distinction
    shape in derivation-api-openapi.yaml, not a oneOf split the spec
    itself doesn't use either."""

    kind: str  # "formula" | "rollup", default "formula"
    # formula only:
    select: str
    target_var: str
    input_vars: list[str]
    extra_vars: list[str]
    input_windows: dict[str, float]
    applies_to_topic_glob: str | None
    # rollup only:
    root_select: str
    part_relationship: str
    leaf_point_class: str
    # both kinds:
    window_seconds: float
    interval_seconds: float | None
    output: dict
    depends_on: list[str]


class Derivation(CreateDerivationRequest):
    """A persisted derivation (derivations.json), or a POST /derivations
    response - CreateDerivationRequest plus the id assigned at creation."""

    id: str


class _TraceEntryRequired(TypedDict):
    target: str


class TraceEntry(_TraceEntryRequired, total=False):
    """One row of a dry-run/derivation-evaluation trace
    (derivation_engine.py's return value) - either an error row or a
    computed-value row, never both; computed_value is `object`, not a
    narrower type, since a derivation's fn_source can return anything
    JSON-serializable."""

    derivation_id: str
    computed_value: object
    would_write_uri: str
    would_attach_to: str
    would_derive_from: list[str]
    error: str


class _TargetHealthRequired(TypedDict):
    target_uri: str
    consecutive_failures: int
    disabled: bool


class TargetHealth(_TargetHealthRequired, total=False):
    last_error: str | None


class _TestCaseResultRequired(TypedDict):
    passed: bool
    actual: object
    expected: object


class TestCaseResult(_TestCaseResultRequired, total=False):
    error: str | None


class _CreateWebhookRequestRequired(TypedDict):
    url: str
    secret: str | None


class CreateWebhookRequest(_CreateWebhookRequestRequired, total=False):
    filter: dict | None  # {"rule_id": str} today, or None/omitted for "every fault"


class _WebhookRequired(TypedDict):
    id: str


class WebhookHealth(TypedDict, total=False):
    """faults.list_webhook_health's per-webhook value - joined into a
    Webhook at GET /webhooks read time, not stored alongside the webhook
    itself (see faults.py's own module docstring on why webhook_health
    is a separate table from webhooks.json). A webhook with no row here
    is treated as not-disabled/0-failures, never as this shape absent."""

    disabled: bool
    consecutive_failures: int
    last_error: str | None


class Webhook(CreateWebhookRequest, _WebhookRequired, WebhookHealth):
    """A persisted webhook (webhooks.json) as returned by GET /webhooks -
    the WebhookHealth fields are absent on the POST /webhooks creation
    response (nothing to join yet)."""
