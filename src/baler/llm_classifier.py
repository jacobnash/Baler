"""
LLM fallback classifier for points/equip the rule engine (mapping.py) can't
place - a chillers/boilers/meters problem, not a temp-sensor problem: those
equipment families have zero rule coverage in rules/haystack_to_brick.yaml
and rules/haystack_equip_to_brick.yaml today.

Deliberately its own module, not folded into mapping.py: mapping.py has zero
dependency on `anthropic` and keeps working (and keeps its existing tests
green) whether or not this module is ever imported. See
mapping.classify_point_with_fallback for the integration point - it takes
this module's classify_with_llm as an injected callable, never imports it
directly.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from baler import tracing

tracer = tracing.get_tracer(__name__)

MODEL = "claude-haiku-4-5"


class LLMClassificationError(Exception):
    """Raised when the model's response can't be parsed into
    _ClassificationSchema (response.parsed_output is None) - a real,
    if rare, outcome the Anthropic SDK's own types allow for. Distinct
    from a network failure, but treated the same way (retry, then skip)
    by callers like autotag.run(), since a re-prompt of the same input
    can succeed where the first parse attempt didn't."""


class _ClassificationSchema(BaseModel):
    brick_class: str
    confidence: float
    reasoning: str


@dataclass
class ClassificationResult:
    brick_class: str
    confidence: float
    reasoning: str


def _prompt(tags: set[str], label: str | None, description: str | None, existing_brick_classes: list[str]) -> str:
    return f"""A building-automation point could not be classified by an exact tag-set rule match.
Guess the correct Brick class for it from context.

Tags already attached (Haystack markers, may be empty): {sorted(tags) or "(none)"}
Raw source topic / label: {label or "(none)"}

Brick classes already known in this deployment's rule files (prefer reusing one of
these over inventing a new name, if one genuinely fits):
{", ".join(existing_brick_classes) if existing_brick_classes else "(none known yet)"}

Respond with your best-guess Brick class name (PascalCase_With_Underscores, Brick
convention, e.g. Chilled_Water_Supply_Temperature_Sensor or Chiller), a confidence
between 0 and 1, and one sentence of reasoning."""


def classify_with_llm(
    tags: set[str],
    label: str | None,
    description: str | None,
    existing_brick_classes: list[str],
) -> ClassificationResult:
    import anthropic

    with tracer.start_as_current_span("llm_classifier.classify_with_llm") as span:
        span.set_attribute("label", label or "")
        span.set_attribute("tag_count", len(tags))

        client = anthropic.Anthropic()
        response = client.messages.parse(
            model=MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": _prompt(tags, label, description, existing_brick_classes)}],
            output_format=_ClassificationSchema,
        )
        parsed = response.parsed_output
        if parsed is None:
            raise LLMClassificationError(f"model returned no parsed output for tags={sorted(tags)}, label={label!r}")

        span.set_attribute("brick_class", parsed.brick_class)
        span.set_attribute("confidence", parsed.confidence)
        return ClassificationResult(brick_class=parsed.brick_class, confidence=parsed.confidence, reasoning=parsed.reasoning)


_PROPOSED_MARKER = "# llm-proposed rules (human-review pending - move an entry above this comment, into the `rules:` list, to activate it)"


def propose_rule(rules_path: str, tags: set[str], brick_class: str) -> bool:
    """Appends a tag-set -> brick_class rule proposal to a clearly separated
    comment block at the end of a rules YAML file, deduping against every
    existing rule (promoted or previously proposed) by exact tag set.
    Written as YAML *comments*, not a second `rules:` key - a second
    top-level `rules:` mapping in the same document would parse (last key
    wins) and silently replace the promoted list with just the proposals.
    Returns True if a new proposal was appended, False if one already
    existed (nothing to do)."""
    import yaml

    with tracer.start_as_current_span("llm_classifier.propose_rule") as span:
        span.set_attribute("brick_class", brick_class)
        with open(rules_path) as f:
            text = f.read()

        existing_tag_sets = {frozenset(r["tags"]) for r in yaml.safe_load(text)["rules"]}
        if _PROPOSED_MARKER in text:
            proposed_lines = [line.removeprefix("# ") for line in text.split(_PROPOSED_MARKER, 1)[1].splitlines() if line.startswith("# ")]
            proposed_rules = yaml.safe_load("\n".join(proposed_lines)) or []
            existing_tag_sets |= {frozenset(r["tags"]) for r in proposed_rules}

        if frozenset(tags) in existing_tag_sets:
            span.set_attribute("outcome", "duplicate")
            return False

        entry = yaml.safe_dump([{"tags": sorted(tags), "brick_class": brick_class}], default_flow_style=None)
        commented = "\n".join(f"# {line}" if line else "#" for line in entry.splitlines())
        with open(rules_path, "a") as f:
            if _PROPOSED_MARKER not in text:
                f.write(f"\n{_PROPOSED_MARKER}\n")
            f.write(commented + "\n")
        span.set_attribute("outcome", "appended")
        return True
