"""
The mapping eval harness (Baler#180). No services, no model: the model
step is always a fake here. The tests over the real mock_hospital corpus
need the windrow submodule checked out (CI's unit job does).
"""

import json

import pytest

from baler import brick_vocab, mapping_eval
from baler.llm_classifier import ClassificationResult
from baler.mapping_eval import Corpus, CorpusEquip, CorpusPoint

needs_windrow = pytest.mark.skipif(
    not mapping_eval.windrow_available(), reason="windrow submodule not checked out (git submodule update --init windrow)"
)


@pytest.fixture(scope="module")
def vocab():
    return brick_vocab.load()


def test_every_ground_truth_class_is_a_real_brick_class(vocab):
    names = [c for classes in mapping_eval.POINT_TRUTH.values() for c in classes]
    names += [c for classes in mapping_eval.EQUIP_TRUTH.values() for c in classes]
    assert [n for n in names if vocab.canonical_class(n) != n] == []


def test_judge_treats_brick_equivalents_as_correct(vocab):
    # Discharge_ is Brick's alias of Supply_Air_Temperature_Sensor
    assert mapping_eval.judge(vocab, "Supply_Air_Temperature_Sensor", ("Discharge_Air_Temperature_Sensor",)) == "correct"


def test_judge_calls_an_ancestor_partial_and_anything_else_wrong(vocab):
    truth = ("Occupied_Target_Zone_Air_Temperature_Setpoint",)
    assert mapping_eval.judge(vocab, "Zone_Air_Temperature_Setpoint", truth) == "partial"
    assert mapping_eval.judge(vocab, "Zone_Air_Temperature_Sensor", truth) == "wrong"
    assert mapping_eval.judge(vocab, "Chilled_Water_Supply_Temp_Sensor", ("Chilled_Water_Supply_Temperature_Sensor",)) == "wrong"


def test_judge_accepts_any_listed_alternative(vocab):
    assert mapping_eval.judge(vocab, "Run_Status", ("Pump_Status", "Run_Status")) == "correct"


def test_judge_with_no_true_class_is_always_wrong(vocab):
    assert mapping_eval.judge(vocab, "Sensor", ()) == "wrong"


def _point(label, description, truth, tokens=("x",), style="verbose"):
    return CorpusPoint(
        topic_prefix="fbf/ahu-1", equip_kind="ahu", label=label, description=description,
        object_identifier="analog-value,1", units="degreesFahrenheit", tokens=tokens, style=style, truth=truth,
    )


def _tiny_corpus():
    """Three points the stand-in tagger tags by label: one the rules place
    exactly, one they only overlap (a PROJ fallback), one not at all."""
    tags_by_label = {
        "DAT": {"discharge": True, "air": True, "temp": True, "sensor": True},
        "CHWS": {"temp": True, "sensor": True},
        "MYSTERY": {},
    }
    points = [
        _point("DAT", "", ("Discharge_Air_Temperature_Sensor",)),
        _point("CHWS", "chw supply", ("Chilled_Water_Supply_Temperature_Sensor",)),
        _point("MYSTERY", "", ()),
    ]
    equip = [CorpusEquip("fbf/ahu-1", "ahu", ("Air_Handling_Unit",), ["DAT", "CHWS", "MYSTERY"])]
    return Corpus(name="tiny", points=points, equipment=equip, tagger=lambda p: dict(tags_by_label[p["label"]]))


def test_rules_only_puts_what_the_rules_cant_place_in_review(vocab):
    report = mapping_eval.evaluate(_tiny_corpus(), vocab=vocab)
    steps = {r.uri.rsplit("/", 1)[1]: (r.step, r.verdict) for r in report.points}
    assert steps == {"DAT": ("rules", "correct"), "CHWS": ("review", None), "MYSTERY": ("review", None)}
    s = mapping_eval.summarize(report, vocab)
    assert s["points"]["accuracy"] == round(1 / 3, 3)
    assert s["points"]["auto_accepted"] == 1 and s["points"]["auto_accepted_wrong"] == 0
    assert s["equipment"]["review"] == 1
    assert report.llm_calls == 0


def test_model_step_answers_are_scored_and_wrong_ones_counted(vocab):
    answers = {
        "fbf/ahu-1/CHWS": ClassificationResult("Chilled_Water_Supply_Temperature_Sensor", 0.9, "chw supply"),
        "fbf/ahu-1/MYSTERY": ClassificationResult("Zone_Air_Temperature_Sensor", 0.9, "a guess"),
    }

    def fake_llm(tags, label, description, known):
        return answers.get(label, ClassificationResult("Air_Handling_Unit", 0.9, "equip"))

    report = mapping_eval.evaluate(_tiny_corpus(), llm_classify=fake_llm, vocab=vocab)
    s = mapping_eval.summarize(report, vocab)
    assert s["points"]["steps"]["model"] == {"points": 2, "pct": 66.7, "correct": 1, "partial": 0, "wrong": 1, "accuracy": 0.5}
    assert s["points"]["auto_accepted_wrong"] == 1  # MYSTERY has no Brick class at all
    assert s["equipment"]["accuracy"] == 1.0
    assert report.llm_calls == 3  # the direct rule match never reaches the model
    assert "AUTO-ACCEPTED BUT WRONG" in mapping_eval.format_summary(s, report)


def test_low_confidence_model_answers_stay_in_review(vocab):
    report = mapping_eval.evaluate(_tiny_corpus(), llm_classify=lambda *a: ClassificationResult("Chiller", 0.2, "unsure"), vocab=vocab)
    assert [r.step for r in report.points] == ["rules", "review", "review"]


def test_compare_flags_only_regressions():
    base = {"points": {"accuracy": 0.5, "auto_accepted_wrong": 1}, "equipment": {"accuracy": 0.0, "auto_accepted_wrong": 0}}
    better = {"points": {"accuracy": 0.6, "auto_accepted_wrong": 0}, "equipment": {"accuracy": 0.1, "auto_accepted_wrong": 0}}
    worse = {"points": {"accuracy": 0.4, "auto_accepted_wrong": 2}, "equipment": {"accuracy": 0.0, "auto_accepted_wrong": 0}}
    assert mapping_eval.compare(better, base) == []
    assert len(mapping_eval.compare(worse, base)) == 2


def test_recorded_answers_replay_identically(vocab, tmp_path):
    calls = []

    def fake_llm(tags, label, description, known):
        calls.append(label)
        return ClassificationResult("Chilled_Water_Supply_Temperature_Sensor", 0.8, "r")

    answers: dict = {}
    first = mapping_eval.evaluate(_tiny_corpus(), llm_classify=mapping_eval._recording(fake_llm, answers), vocab=vocab)
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(answers))
    again = mapping_eval.evaluate(_tiny_corpus(), llm_classify=mapping_eval.replay(json.loads(path.read_text())), vocab=vocab)
    assert mapping_eval.summarize(first, vocab) == mapping_eval.summarize(again, vocab)
    assert len(calls) == 3


@needs_windrow
def test_mock_hospital_corpus_has_ground_truth_for_every_point():
    corpus = mapping_eval.load_mock_hospital()
    assert 400 <= len(corpus.points) <= 1000
    assert {e.kind for e in corpus.equipment} == set(mapping_eval.EQUIP_TRUTH)
    assert {p.style for p in corpus.points} == {"verbose", "underscore", "cryptic", "camel"}


@needs_windrow
def test_rules_only_run_on_mock_hospital_does_not_regress_against_the_baseline(vocab):
    summary = mapping_eval.summarize(mapping_eval.evaluate(mapping_eval.load_mock_hospital(), vocab=vocab), vocab)
    with open(mapping_eval.DEFAULT_BASELINE) as f:
        baseline = json.load(f)
    assert mapping_eval.compare(summary, baseline) == []
    assert summary["points"]["auto_accepted_wrong"] == 0


@needs_windrow
def test_cli_check_exits_zero_against_the_committed_baseline(capsys):
    assert mapping_eval.main(["--check", "--no-per-class"]) == 0
    assert "no regression" in capsys.readouterr().out


def _inventing_model(tags, label, description, known):
    """A stand-in for a model that glues a plausible class name out of the
    label (CHW-SUP-TEM-7 -> Chw_Sup_Tem_Sensor), confidently."""
    import re

    words = [w.capitalize() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", (label or "").rsplit("/", 1)[-1])]
    name = "_".join(words) or "Equipment"
    if not name.endswith(("Sensor", "Status", "Command", "Setpoint", "Equipment")):
        name += "_Sensor"
    return ClassificationResult(name, 0.9, "glued from the label")


def test_a_model_answer_that_is_not_a_brick_class_goes_to_review(vocab):
    report = mapping_eval.evaluate(_tiny_corpus(), llm_classify=_inventing_model, vocab=vocab)
    by_label = {r.uri.rsplit("/", 1)[1]: r for r in report.points}
    assert (by_label["CHWS"].step, by_label["CHWS"].predicted) == ("review", None)  # "Chws_Sensor"
    assert by_label["CHWS"].outcome == "fallback"  # keeps its PROJ class


@needs_windrow
def test_nothing_an_inventing_model_says_reaches_the_graph_unless_brick_has_it(vocab):
    """#181, measured on the whole corpus: before the check, this stand-in
    model's invented names were 30% of points, auto-accepted and wrong."""
    report = mapping_eval.evaluate(mapping_eval.load_mock_hospital(), llm_classify=_inventing_model, vocab=vocab)
    written = [r.predicted for r in report.points + report.equipment if r.step == "model"]
    assert written and all(vocab.canonical_class(c) == c for c in written)
    assert mapping_eval.summarize(report, vocab)["points"]["auto_accepted_wrong"] == 0
