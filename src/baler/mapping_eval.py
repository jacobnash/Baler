"""
Mapping eval harness (Baler#180): scores the tagging ladder against a site
whose answers we know, so a change to the rules, the lexicon or the model
step comes with a number instead of a guess.

The corpus is Windrow's `mock_hospital` site, read from the `windrow`
submodule (the pinned commit, so the score only moves when the submodule
or Baler moves). Its generator knows every point's true kind (the token
list it renders into one of four naming styles), and this module maps each
kind to the Brick class a person would assign (POINT_TRUTH, EQUIP_TRUTH).
The pipeline then runs offline, the way a real BACnet job flows, with no
BACnet, MQTT or Oxigraph:

  1. Windrow's `bacnet_tagger.tag_point` over each point's label, object
     identifier and description, plus the `writable` and `unit` tags
     `connection_manager._tag_points` adds;
  2. Baler's `ingest.ingest_tags` into an in-memory Store;
  3. `mapping.classify_point`, then `model_step.run` on what the rules
     left, as `autotag` does (points, then equipment).

Each point ends on one ladder step: `rules` (a direct rule match,
auto-accepted), `model` (both model asks agreed and nothing vetoed) or
`review` (a mapping proposal: a person decides). The model step runs even
without a model: its proposals carry the deterministic candidates, and
the report scores those too (is the truth in the top 1 / top 3 a reviewer
is shown?).
Memory and site conventions (#185, #186) don't exist yet; their steps
appear here once they do.

A verdict per auto-accepted point:

  correct  the predicted class is the true class or a Brick equivalent
           (Discharge_Air_Temperature_Sensor = Supply_Air_Temperature_Sensor)
  partial  the predicted class is a Brick ancestor of the true class: true,
           but less specific (Zone_Air_Temperature_Setpoint for an occupied
           zone setpoint)
  wrong    anything else, including a class Brick doesn't have

"Auto-accepted but wrong" is the number that must stay near zero: those
are the points nobody looks at. Accuracy is exact-class correct over all
points, so a point left for review counts against it.

  python -m baler.mapping_eval                     # rules only, no key
  python -m baler.mapping_eval --check tests/fixtures/mapping_eval_baseline.json
  python -m baler.mapping_eval --llm --llm-record answers.json   # costs money
  python -m baler.mapping_eval --llm-replay answers.json         # free re-run
  python -m baler.mapping_eval --simulate inventing              # no key, see SIMULATED

CI runs the `--check` form: deterministic, rules only, a few seconds.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from rdflib import RDF, Literal, URIRef

from baler import brick_vocab, ingest, mapping, mapping_proposals, model_step
from baler.store import TD, Store

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_WINDROW_DIR = os.path.join(REPO_ROOT, "windrow")
DEFAULT_BASELINE = os.path.join(REPO_ROOT, "tests", "fixtures", "mapping_eval_baseline.json")

# Ladder steps in order. A point's step is the one that decided it.
STEPS = ("rules", "model", "review")
AUTO_STEPS = ("rules", "model")

# mock_hospital point kind (its token list) -> the Brick 1.4.4 classes a
# person would accept, preferred first. Every name is checked against the
# vendored ontology by tests/test_mapping_eval.py. An empty tuple means
# Brick has no class for it: the only right outcome is review.
POINT_TRUTH: dict[tuple[str, ...], tuple[str, ...]] = {
    # AHU
    ("discharge", "air", "temp"): ("Discharge_Air_Temperature_Sensor",),
    ("return", "air", "temp"): ("Return_Air_Temperature_Sensor",),
    ("mixed", "air", "temp"): ("Mixed_Air_Temperature_Sensor",),
    ("outside", "air", "temp"): ("Outside_Air_Temperature_Sensor",),
    ("supply", "fan", "status"): ("Fan_Status",),  # Brick has no Supply_Fan_Status
    ("supply", "fan", "cmd"): ("Fan_Command",),  # nor Supply_Fan_Command
    ("static", "pressure"): ("Supply_Air_Static_Pressure_Sensor",),  # AHU duct static
    # VAV
    ("zone", "air", "temp"): ("Zone_Air_Temperature_Sensor",),
    ("zone", "temp", "occ", "sp"): ("Occupied_Target_Zone_Air_Temperature_Setpoint",),
    ("zone", "temp", "unocc", "sp"): ("Unoccupied_Target_Zone_Air_Temperature_Setpoint",),
    ("damper", "cmd"): ("Damper_Position_Command",),
    ("damper", "pos"): ("Damper_Position_Sensor",),
    # A VAV's airflow is its discharge airflow; Brick has no Zone_Air_Flow_Sensor.
    ("zone", "airflow"): ("Supply_Air_Flow_Sensor",),
    ("zone", "co2"): ("Zone_CO2_Level_Sensor",),
    ("zone", "humidity"): ("Zone_Air_Humidity_Sensor",),
    # Chiller
    ("chw", "sup", "temp"): ("Chilled_Water_Supply_Temperature_Sensor",),
    ("chw", "ret", "temp"): ("Chilled_Water_Return_Temperature_Sensor",),
    ("cond", "wtr", "temp"): ("Condenser_Water_Temperature_Sensor",),
    ("chiller", "run", "status"): ("Run_Status",),
    ("chiller", "start", "cmd"): ("Start_Stop_Command",),
    ("kw", "input"): ("Electric_Power_Sensor",),
    # Boiler
    ("hw", "sup", "temp"): ("Hot_Water_Supply_Temperature_Sensor",),
    ("hw", "ret", "temp"): ("Hot_Water_Return_Temperature_Sensor",),
    ("boiler", "run", "status"): ("Run_Status",),
    ("boiler", "start", "cmd"): ("Start_Stop_Command",),
    ("firing", "rate"): (),  # no Brick 1.4.4 class for a burner firing rate
    # Pumps (chilled and hot water share the vocabulary). Brick's
    # Pump_Status/Pump_Command and the generic run/start-stop classes sit in
    # unrelated branches; either is a fair answer.
    ("pump", "run", "status"): ("Pump_Status", "Run_Status"),
    ("pump", "start", "cmd"): ("Pump_Command", "Start_Stop_Command"),
    ("pump", "spd", "fdbk"): ("Motor_Speed_Sensor",),
    # Meter
    ("kw", "demand"): ("Electric_Power_Sensor",),
    ("kwh", "total"): ("Electric_Energy_Sensor",),
    # Exhaust fan
    ("fan", "status"): ("Fan_Status",),
    ("fan", "cmd"): ("Fan_Command",),
}

# mock_hospital equipment prefix -> Brick class. Its meters read kW/kWh.
EQUIP_TRUTH: dict[str, tuple[str, ...]] = {
    "ahu": ("Air_Handling_Unit",),
    "vav": ("Variable_Air_Volume_Box",),
    "chiller": ("Chiller",),
    "boiler": ("Boiler",),
    "chw-pump": ("Chilled_Water_Pump",),
    "hw-pump": ("Hot_Water_Pump",),
    "meter": ("Electrical_Meter",),
    "ef": ("Exhaust_Fan",),
}

# connection_manager.COMMANDABLE_TYPES, copied rather than imported: that
# module pulls in bacpypes3, which this offline harness doesn't need.
_COMMANDABLE_TYPES = {
    "analog-output", "analog-value", "binary-output", "binary-value",
    "multi-state-output", "multi-state-value",
}


@dataclass
class CorpusPoint:
    topic_prefix: str
    equip_kind: str
    label: str
    description: str
    object_identifier: str
    units: str | None
    tokens: tuple[str, ...]
    style: str
    truth: tuple[str, ...]
    initial_value: Any = None

    @property
    def topic(self) -> str:
        return f"{self.topic_prefix}/{self.label}"


@dataclass
class CorpusEquip:
    topic_prefix: str
    kind: str
    truth: tuple[str, ...]
    point_labels: list[str]


@dataclass
class Corpus:
    name: str
    points: list[CorpusPoint]
    equipment: list[CorpusEquip]
    # (point dict) -> Haystack tags dict, as Windrow publishes them.
    tagger: Callable[[dict], dict]


@dataclass
class Result:
    uri: str
    kind: str  # "point" | "equip"
    truth: list[str]
    tags: list[str]
    outcome: str  # classify_point's direct/fallback/miss, or model
    step: str  # rules/model/review
    predicted: str | None  # the Brick class written, auto-accepted steps only
    verdict: str | None  # correct/partial/wrong, auto-accepted steps only
    style: str = ""
    has_description: bool = False
    candidates: list[str] = field(default_factory=list)  # shown to a reviewer, best first
    refused_because: str | None = None


@dataclass
class Report:
    corpus: str
    mode: str
    points: list[Result] = field(default_factory=list)
    equipment: list[Result] = field(default_factory=list)
    llm_calls: int = 0
    patterns: int = 0


# --- corpus ---------------------------------------------------------------


def _load_module(path: str, name: str) -> ModuleType:
    """By file path, not `import windrow...`: the submodule is the pinned
    corpus, and an installed Windrow (editable from some other checkout)
    would otherwise shadow it - windrow is a namespace package."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"can't load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def windrow_available(windrow_dir: str = DEFAULT_WINDROW_DIR) -> bool:
    return os.path.exists(os.path.join(windrow_dir, "src", "windrow", "mock_hospital.py"))


def _windrow_commit(windrow_dir: str) -> str:
    try:
        out = subprocess.run(["git", "-C", windrow_dir, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_mock_hospital(windrow_dir: str = DEFAULT_WINDROW_DIR, seed: int | None = None) -> Corpus:
    """mock_hospital's site, with each point's true kind captured from the
    generator itself: `_render_label` is wrapped to record the token list
    and naming style it was called with, one call per point, in the same
    order generate_site emits points. The wrapper calls the original, so
    the seeded output is unchanged."""
    src = os.path.join(windrow_dir, "src", "windrow")
    if not windrow_available(windrow_dir):
        raise FileNotFoundError(
            f"{src}/mock_hospital.py not found: check out the windrow submodule (git submodule update --init windrow)"
        )
    hospital: Any = _load_module(os.path.join(src, "mock_hospital.py"), "_baler_eval_mock_hospital")
    tagger = _load_module(os.path.join(src, "bacnet_tagger.py"), "_baler_eval_bacnet_tagger")
    rules = tagger.load_rules(os.path.join(src, "rules", tagger.DEFAULT_RULES))

    rendered: list[tuple[tuple[str, ...], str]] = []
    original_render = hospital._render_label

    def recording_render(tokens, style, seq):
        rendered.append((tuple(tokens), style))
        return original_render(tokens, style, seq)

    hospital._render_label = recording_render
    try:
        seed = hospital.SEED if seed is None else seed
        site_spec, equipment = hospital.generate_site(seed)
    finally:
        hospital._render_label = original_render

    if len(rendered) != len(site_spec):
        raise RuntimeError(f"captured {len(rendered)} labels for {len(site_spec)} points: mock_hospital changed shape")

    points: list[CorpusPoint] = []
    equips: list[CorpusEquip] = []
    i = 0
    for equip in equipment:
        prefix = equip["topic_prefix"]
        kind = prefix.split("/", 1)[1].rsplit("-", 1)[0]
        if kind not in EQUIP_TRUTH:
            raise KeyError(f"no ground truth for mock_hospital equipment kind {kind!r}: add it to EQUIP_TRUTH")
        for p in equip["points"]:
            spec_obj = site_spec[i]
            tokens, style = rendered[i]
            i += 1
            if tokens not in POINT_TRUTH:
                raise KeyError(f"no ground truth for mock_hospital point kind {tokens!r}: add it to POINT_TRUTH")
            points.append(CorpusPoint(
                topic_prefix=prefix, equip_kind=kind, label=p["label"], description=p["description"],
                object_identifier=p["object_identifier"], units=spec_obj["units"], tokens=tokens, style=style,
                truth=POINT_TRUTH[tokens], initial_value=spec_obj.get("initial_value"),
            ))
        equips.append(CorpusEquip(prefix, kind, EQUIP_TRUTH[kind], [p["label"] for p in equip["points"]]))

    def windrow_tags(point: dict) -> dict:
        # connection_manager._tag_points, minus the MQTT publish.
        tags: dict = dict(tagger.tag_point(point, rules))
        object_type = str(point.get("object_identifier", "")).split(",", 1)[0].strip()
        if object_type in _COMMANDABLE_TYPES and not tags.get("sensor"):
            tags["writable"] = True
        unit = point.get("unit") or tagger.haystack_unit(point.get("units"))
        if unit:
            tags["unit"] = unit
        return tags

    name = f"mock_hospital (windrow {_windrow_commit(windrow_dir)}, seed {seed})"
    return Corpus(name=name, points=points, equipment=equips, tagger=windrow_tags)


# --- scoring --------------------------------------------------------------


def _same_class(vocab: brick_vocab.Vocab, a: str, b: str) -> bool:
    return a == b or (b in vocab.descendants(a) and a in vocab.descendants(b))


def judge(vocab: brick_vocab.Vocab, predicted: str, truth: tuple[str, ...] | list[str]) -> str:
    """correct / partial / wrong - see the module docstring."""
    if any(_same_class(vocab, predicted, t) for t in truth):
        return "correct"
    if any(t in vocab.descendants(predicted) for t in truth):
        return "partial"
    return "wrong"


class _CountingModel:
    def __init__(self, model, report: Report):
        self.model, self.report, self.name = model, report, model.name

    def ask(self, prompt: str) -> dict:
        self.report.llm_calls += 1
        return self.model.ask(prompt)


def evaluate(
    corpus: Corpus,
    model: model_step.Model | None = None,
    vocab: brick_vocab.Vocab | None = None,
    point_rules_path: str = mapping.DEFAULT_RULES_PATH,
    equip_rules_path: str = mapping.EQUIP_RULES_PATH,
    mode: str | None = None,
) -> Report:
    vocab = vocab or brick_vocab.load()
    point_rules = mapping.load_rules(point_rules_path)
    equip_rules = mapping.load_rules(equip_rules_path)
    store = Store()
    report = Report(corpus=corpus.name, mode=mode or (f"rules + model ({model.name})" if model else "rules only"))

    values: dict[str, Any] = {}
    for point in corpus.points:
        tags = corpus.tagger({
            "label": point.label, "object_identifier": point.object_identifier,
            "description": point.description, "units": point.units,
        })
        uri = ingest.ingest_tags(store, point.topic, tags)
        # ingest_reading's graph write: the first reading gives the point its
        # source topic, which is the label the model step sees.
        store.add_many([(uri, RDF.type, TD.RawPoint), (uri, TD.sourceTopic, Literal(point.topic))])
        values[str(uri)] = point.initial_value
    for equip in corpus.equipment:
        # Windrow's BACnet equip/tags carries no marker tags (only
        # windrowSite/windrowInstance, and only on a registered edge).
        equip_uri = ingest.ingest_equip_tags(store, equip.topic_prefix, {})
        for label in equip.point_labels:
            ingest.link_point_to_equip(store, ingest.topic_to_point_uri(f"{equip.topic_prefix}/{label}"), equip_uri)

    entities: list[tuple[URIRef, str, tuple[str, ...], CorpusPoint | None]] = [
        (ingest.topic_to_point_uri(p.topic), "point", p.truth, p) for p in corpus.points
    ] + [(ingest.topic_prefix_to_equip_uri(e.topic_prefix), "equip", e.truth, None) for e in corpus.equipment]
    results: dict[str, Result] = {}
    unresolved = []
    for uri, kind, truth, source in entities:
        tag_list = sorted(mapping.read_tags(store, uri))
        outcome, brick_class = mapping.classify_point(store, uri, point_rules if kind == "point" else equip_rules)
        r = Result(uri=str(uri), kind=kind, truth=list(truth), tags=tag_list, outcome=outcome, step="review", predicted=None, verdict=None)
        if source is not None:
            r.style, r.has_description = source.style, bool(source.description)
        if outcome == "direct":
            r.step, r.predicted, r.verdict = "rules", brick_class, judge(vocab, brick_class, truth)
        else:
            unresolved.append((uri, kind))
        results[str(uri)] = r

    # One reading per point is all the corpus has (the generator's initial
    # value): enough to tell on/off from analog, not a distribution.
    def stats(uris: list[str]) -> dict:
        return {u: {"samples": 1, "boolean": isinstance(values[u], str)} | ({} if isinstance(values[u], str) else {"min": values[u], "max": values[u]})
                for u in uris if u in values and values[u] is not None}

    counted = _CountingModel(model, report) if model is not None else None
    decisions = model_step.run(store, unresolved, counted, proposals=mapping_proposals.MemoryProposals(), stats=stats, vocab=vocab)
    report.patterns = len({d.pattern for d in decisions})
    for d in decisions:
        r = results[d.uri]
        r.candidates = [c["brick_class"] for c in d.candidates]
        r.refused_because = d.refused_because
        if d.accepted is not None:
            r.step, r.outcome, r.predicted, r.verdict = "model", "model", d.accepted, judge(vocab, d.accepted, r.truth)

    report.points = [results[str(u)] for u, k, _, _ in entities if k == "point"]
    report.equipment = [results[str(u)] for u, k, _, _ in entities if k == "equip"]
    return report


# --- summary --------------------------------------------------------------


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 3) if d else None


def summarize_results(results: list[Result], vocab: brick_vocab.Vocab) -> dict:
    total = len(results)
    verdicts = Counter(r.verdict for r in results if r.step in AUTO_STEPS)
    auto = sum(verdicts.values())
    steps = {}
    for step in STEPS:
        decided = [r for r in results if r.step == step]
        v = Counter(r.verdict for r in decided)
        steps[step] = {
            "points": len(decided),
            "pct": _pct(len(decided), total),
            "correct": v["correct"],
            "partial": v["partial"],
            "wrong": v["wrong"],
            # Of what this step auto-accepted, how much was exactly right.
            "accuracy": _ratio(v["correct"], len(decided)) if step in AUTO_STEPS else None,
        }

    # Per class: rows are Brick classes (preferred spelling). Recall is over
    # points whose true class is the row; precision over auto-accepted
    # predictions of the row. A point with no true class adds no recall row.
    def label(c: str) -> str:
        return vocab.preferred_name(c)

    support: Counter = Counter()
    hit_truth: Counter = Counter()
    predicted: Counter = Counter()
    hit_pred: Counter = Counter()
    for r in results:
        if r.truth:
            support[label(r.truth[0])] += 1
            if r.verdict == "correct":
                hit_truth[label(r.truth[0])] += 1
        if r.predicted is not None:
            predicted[label(r.predicted)] += 1
            if r.verdict == "correct":
                hit_pred[label(r.predicted)] += 1
    per_class = {
        c: {
            "support": support[c],
            "predicted": predicted[c],
            "precision": _ratio(hit_pred[c], predicted[c]),
            "recall": _ratio(hit_truth[c], support[c]) if support[c] else None,
        }
        for c in sorted(set(support) | set(predicted))
    }

    # What a reviewer is shown: is the true class among the proposal's
    # candidates? Over review points that have a true class.
    reviewed = [r for r in results if r.step == "review" and r.truth]
    in_top = {k: sum(any(judge(vocab, c, r.truth) == "correct" for c in r.candidates[:k]) for r in reviewed) for k in (1, 3)}

    return {
        "total": total,
        "review_truth_in_top1": _ratio(in_top[1], len(reviewed)),
        "review_truth_in_top3": _ratio(in_top[3], len(reviewed)),
        "accuracy": _ratio(verdicts["correct"], total),
        "correct": verdicts["correct"],
        "partial": verdicts["partial"],
        "auto_accepted": auto,
        "auto_accepted_pct": _pct(auto, total),
        "auto_accepted_wrong": verdicts["wrong"],
        "auto_accepted_wrong_pct": _pct(verdicts["wrong"], total),
        "review": total - auto,
        "review_pct": _pct(total - auto, total),
        "steps": steps,
        "per_class": per_class,
    }


def _breakdown(results: list[Result], key: Callable[[Result], str]) -> dict:
    groups: dict[str, list[Result]] = {}
    for r in results:
        groups.setdefault(key(r), []).append(r)
    return {
        k: {
            "total": len(rs),
            "accuracy": _ratio(sum(r.verdict == "correct" for r in rs), len(rs)),
            "auto_accepted_pct": _pct(sum(r.step in AUTO_STEPS for r in rs), len(rs)),
        }
        for k, rs in sorted(groups.items())
    }


def summarize(report: Report, vocab: brick_vocab.Vocab | None = None) -> dict:
    vocab = vocab or brick_vocab.load()
    return {
        "corpus": report.corpus,
        "mode": report.mode,
        "llm_calls": report.llm_calls,
        "patterns": report.patterns,
        "points": summarize_results(report.points, vocab),
        "points_by_style": _breakdown(report.points, lambda r: r.style),
        "points_by_description": _breakdown(report.points, lambda r: "with description" if r.has_description else "no description"),
        "equipment": summarize_results(report.equipment, vocab),
    }


def _fmt_ratio(x: float | None) -> str:
    return "   -" if x is None else f"{100 * x:5.1f}%"


def format_summary(summary: dict, report: Report | None = None, per_class: bool = True) -> str:
    lines = [f"Mapping eval: {summary['corpus']}, {summary['mode']}"]
    lines.append(f"model-step patterns: {summary['patterns']}, model calls: {summary['llm_calls']}")
    for section in ("points", "equipment"):
        s = summary[section]
        lines += [
            "",
            f"{section.upper()} ({s['total']})",
            f"  accuracy (exact class)        {_fmt_ratio(s['accuracy'])}",
            f"  auto-accepted                 {s['auto_accepted_pct']:5.1f}%  ({s['auto_accepted']})",
            f"  auto-accepted but wrong       {s['auto_accepted_wrong_pct']:5.1f}%  ({s['auto_accepted_wrong']})",
            f"  auto-accepted, less specific  {_pct(s['partial'], s['total']):5.1f}%  ({s['partial']})",
            f"  left for review               {s['review_pct']:5.1f}%  ({s['review']})",
            f"    truth is the top candidate  {_fmt_ratio(s['review_truth_in_top1'])}",
            f"    truth in the top 3          {_fmt_ratio(s['review_truth_in_top3'])}",
            "",
            "  step      points      %  correct  partial  wrong  accuracy",
        ]
        for step, st in s["steps"].items():
            lines.append(
                f"  {step:<8}{st['points']:>8}{st['pct']:>7.1f}{st['correct']:>9}{st['partial']:>9}{st['wrong']:>7}  {_fmt_ratio(st['accuracy'])}"
            )
        if per_class:
            lines += ["", f"  {'class':<50}{'support':>8}{'pred':>6}{'precision':>11}{'recall':>8}"]
            for c, row in s["per_class"].items():
                lines.append(f"  {c:<50}{row['support']:>8}{row['predicted']:>6}{_fmt_ratio(row['precision']):>11}{_fmt_ratio(row['recall']):>8}")
    for title, key in (("BY NAMING STYLE", "points_by_style"), ("BY DESCRIPTION", "points_by_description")):
        lines += ["", title, f"  {'':<18}{'points':>7}{'accuracy':>10}{'auto':>8}"]
        for k, row in summary[key].items():
            lines.append(f"  {k:<18}{row['total']:>7}{_fmt_ratio(row['accuracy']):>10}{row['auto_accepted_pct']:>7.1f}%")
    if report is not None:
        wrong = [r for r in report.points + report.equipment if r.verdict == "wrong"]
        if wrong:
            lines += ["", "AUTO-ACCEPTED BUT WRONG"]
            for r in wrong:
                lines.append(f"  {r.uri}: {r.step} said {r.predicted}, truth {' | '.join(r.truth) or '(no Brick class)'}")
    return "\n".join(lines)


# --- baseline -------------------------------------------------------------


def compare(summary: dict, baseline: dict) -> list[str]:
    """Regressions against a recorded baseline: exact accuracy down, or
    auto-accepted-but-wrong up, for points or equipment. An improvement
    isn't a failure; update the baseline in the PR that makes it, so the
    diff shows the before and after."""
    problems = []
    for section in ("points", "equipment"):
        now, then = summary[section], baseline[section]
        if (now["accuracy"] or 0) < (then["accuracy"] or 0):
            problems.append(f"{section}: accuracy fell from {then['accuracy']} to {now['accuracy']}")
        if now["auto_accepted_wrong"] > then["auto_accepted_wrong"]:
            problems.append(f"{section}: auto-accepted but wrong rose from {then['auto_accepted_wrong']} to {now['auto_accepted_wrong']}")
    return problems


# --- model step: real, recorded, replayed, simulated -----------------------


class _Recording:
    def __init__(self, model, answers: dict):
        self.model, self.answers, self.name = model, answers, model.name

    def ask(self, prompt: str) -> dict:
        reply = self.model.ask(prompt)
        self.answers[_prompt_key(prompt)] = reply
        return reply


def _prompt_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


class Replay:
    """A model step answering from a recorded run (--llm-record), keyed by
    the exact prompt: free to re-run after a change downstream of the
    model. A change to the prompt itself (evidence, candidates) misses."""

    def __init__(self, answers: dict, name: str = "replay"):
        self.answers, self.name = answers, name

    def ask(self, prompt: str) -> dict:
        key = _prompt_key(prompt)
        if key not in self.answers:
            raise KeyError(f"no recorded answer for prompt {key}: record again against this version")
        return self.answers[key]


def _prompt_field(prompt: str, name: str) -> list[str]:
    for line in prompt.splitlines():
        if line.startswith(name + ": "):
            return [x.strip() for x in line[len(name) + 2:].split(",") if x.strip() and x.strip() != "(none found)"]
    return []


class Simulated:
    """Stand-ins for a model, for exercising the ladder without a key. They
    say nothing about how good a real model is; they show what the ladder
    does with a given kind of answer.

      inventing  glues a class name out of the first example label
                 (CHW-SUP-TEM-7 -> Chw_Sup_Tem_Sensor), sure of itself
      echo       answers the first three candidates in the order the prompt
                 lists them - the re-ask lists them reversed, so it never
                 agrees with itself
      ranker     answers the deterministic ranker's top candidates whatever
                 their order in the prompt, so it always agrees with itself:
                 what auto-accept would do if a model just trusted the ranker
    """

    KINDS = ("inventing", "echo", "ranker")

    def __init__(self, kind: str):
        if kind not in self.KINDS:
            raise ValueError(f"simulated model must be one of {', '.join(self.KINDS)}")
        self.kind, self.name = kind, f"simulated:{kind}"

    def ask(self, prompt: str) -> dict:
        cands = _prompt_field(prompt, "Candidate Brick classes")
        if self.kind == "inventing":
            first = (_prompt_field(prompt, "Examples of the pattern") or [""])[0].split(";")[0]
            leaf = first.split("(")[0].strip().rstrip("/").rsplit("/", 1)[-1]
            words = [w.capitalize() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", leaf)]
            name = "_".join(words) or "Equipment"
            if not name.endswith(("Sensor", "Status", "Command", "Setpoint", "Equipment")):
                name += "_Sensor"
            picks = [name]
        elif self.kind == "echo":
            picks = cands[:3]
        else:
            if not prompt.startswith("Pick the Brick"):  # the reworded ask lists them reversed
                cands = list(reversed(cands))
            picks = cands[:3]
        return {"candidates": [{"brick_class": c, "reason": self.kind} for c in picks], "confidence": 0.9}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Baler's tagging ladder against Windrow's mock_hospital site.")
    parser.add_argument("--windrow", default=DEFAULT_WINDROW_DIR, help="Windrow checkout (default: the submodule)")
    parser.add_argument("--seed", type=int, default=None, help="mock_hospital seed (default: its own, 42)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--llm", action="store_true", help="enable the model step (BALER_LLM_PROVIDER's model; costs money)")
    group.add_argument("--llm-replay", metavar="FILE", help="enable the model step from answers recorded with --llm-record")
    group.add_argument("--simulate", choices=Simulated.KINDS, help="enable the model step with a stand-in model (no key; see Simulated)")
    parser.add_argument("--llm-record", metavar="FILE", help="with --llm: save the model's answers for --llm-replay")
    parser.add_argument("--json", metavar="FILE", help="write the summary as JSON")
    parser.add_argument("--check", metavar="BASELINE", nargs="?", const=DEFAULT_BASELINE, help="exit 1 on a regression against this baseline JSON")
    parser.add_argument("--no-per-class", action="store_true", help="leave the per-class table out of the text report")
    args = parser.parse_args(argv)

    model: model_step.Model | None = None
    answers: dict = {}
    if args.llm:
        from baler import llm_classifier

        model = llm_classifier.model_from_env()
        if model is None:
            parser.error("--llm needs a model: BALER_LLM_PROVIDER is none (set ANTHROPIC_API_KEY, or BALER_LLM_BASE_URL and BALER_LLM_MODEL)")
        if args.llm_record:
            model = _Recording(model, answers)
    elif args.llm_replay:
        with open(args.llm_replay) as f:
            model = Replay(json.load(f), name=f"replayed from {os.path.basename(args.llm_replay)}")
    elif args.simulate:
        model = Simulated(args.simulate)
    if args.llm_record and not args.llm:
        parser.error("--llm-record needs --llm")

    corpus = load_mock_hospital(args.windrow, args.seed)
    report = evaluate(corpus, model=model)
    summary = summarize(report)
    print(format_summary(summary, report, per_class=not args.no_per_class))

    if args.llm_record:
        with open(args.llm_record, "w") as f:
            json.dump(answers, f, indent=2, sort_keys=True)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(summary, f, indent=2, sort_keys=True)
            f.write("\n")
    if args.check:
        with open(args.check) as f:
            problems = compare(summary, json.load(f))
        if problems:
            print("\nREGRESSION against " + args.check, file=sys.stderr)
            for p in problems:
                print("  " + p, file=sys.stderr)
            return 1
        print(f"\nno regression against {os.path.relpath(args.check)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

