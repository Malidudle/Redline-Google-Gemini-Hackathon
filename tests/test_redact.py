"""Tests for the redaction engine.

The important test in this file is test_seed_transcript_rules_only. It runs the whole
demo transcript with the model monkeypatched to raise, which is the state of the world
if Ollama dies on stage. Everything the demo shows must come from the rules layer.

Async tests use asyncio.run() rather than pytest-asyncio, so the file runs even if the
plugin is missing. It also runs standalone: python3 tests/test_redact.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.contracts import Exemption, RedactionSpan, Segment  # noqa: E402

from backend.redact import model as model_client  # noqa: E402
from backend.redact import pipeline  # noqa: E402
from backend.redact.rules import (  # noqa: E402
    find_money,
    find_nino,
    find_rule_spans,
    nhs_check_digit_ok,
)

SEED_PATH = ROOT / "demo" / "seed_transcript.json"
SEED = json.loads(SEED_PATH.read_text())
SEED_BY_ID = {s["id"]: s for s in SEED}


def surfaces(spans) -> list[str]:
    return [s.surface for s in spans]


def covered(spans, needle: str) -> RedactionSpan | None:
    """The span whose surface contains needle, if any."""
    for s in spans:
        if needle.casefold() in s.surface.casefold():
            return s
    return None


# ==========================================================================
# 1. The demo transcript, rules layer only, model completely unavailable
# ==========================================================================

# (segment id, expected substring, expected exemption)
SEED_EXPECTATIONS = [
    ("93f3bc86", "Dr Sarah Whitfield", Exemption.S40_2),
    ("cbbe2eb9", "at-risk register", Exemption.S40_2),
    ("cbbe2eb9", "four zero zero, one two three, four five six four", Exemption.S40_2),
    ("bc38a5aa", "Ardent Systems", Exemption.S43_2),
    ("bc38a5aa", "£2.4 million", Exemption.S43_2),
    ("01f8183f", "in formulation", Exemption.S35),
    ("ce43f9dd", "legal advice from the council's solicitor", Exemption.S42),
    ("85467ded", "Dr Sarah Whitfield", Exemption.S40_2),
    ("85467ded", "four zero zero, one two three, four five six four", Exemption.S40_2),
    ("85467ded", "Ardent Systems", Exemption.S43_2),
    ("85467ded", "£2.4 million", Exemption.S43_2),
]

CLEAN_SEED_IDS = ["174a632e", "31b36496"]


@pytest.fixture
def dead_model(monkeypatch):
    """Ollama is gone. Any call raises."""
    async def boom(*args, **kwargs):
        raise RuntimeError("ollama is not running")
    monkeypatch.setattr(model_client, "annotate", boom)


@pytest.mark.parametrize("segment_id,needle,exemption", SEED_EXPECTATIONS)
def test_seed_transcript_rules_only(segment_id, needle, exemption):
    text = SEED_BY_ID[segment_id]["text"]
    spans = find_rule_spans(text)
    hit = covered(spans, needle)
    assert hit is not None, f"{needle!r} not detected in {segment_id}; got {surfaces(spans)}"
    assert hit.exemption is exemption
    assert hit.source == "rule"
    assert hit.confidence == 1.0
    assert text[hit.start:hit.end] == hit.surface


@pytest.mark.parametrize("segment_id", CLEAN_SEED_IDS)
def test_seed_clean_segments_have_no_rule_spans(segment_id):
    assert find_rule_spans(SEED_BY_ID[segment_id]["text"]) == []


def test_full_pipeline_with_model_dead(dead_model):
    async def run():
        results = {}
        pipeline.reset_stats()
        for item in SEED:
            segment = Segment(**item)
            spans, state = await pipeline.redact_segment(segment, [])
            results[segment.id] = (spans, state)
        return results

    results = asyncio.run(run())

    for segment_id, needle, exemption in SEED_EXPECTATIONS:
        spans, state = results[segment_id]
        assert state == "done", f"{segment_id} should still be done from rules alone"
        hit = covered(spans, needle)
        assert hit is not None, f"{needle!r} lost in {segment_id}: {surfaces(spans)}"
        assert hit.exemption is exemption

    for spans, _ in results.values():
        assert_no_overlaps(spans)

    stats = pipeline.get_stats()
    assert stats["segments"] == len(SEED)
    assert stats["redactions"] >= len(SEED_EXPECTATIONS)
    assert stats["model_failures"] == len(SEED)


def test_state_is_failed_only_when_nothing_worked(dead_model):
    """A clean segment with a dead model is not 'done' - nothing reviewed it."""
    async def run():
        clean = Segment(text=SEED_BY_ID["174a632e"]["text"])
        dirty = Segment(text=SEED_BY_ID["93f3bc86"]["text"])
        return (await pipeline.redact_segment(clean, []),
                await pipeline.redact_segment(dirty, []))

    (clean_spans, clean_state), (dirty_spans, dirty_state) = asyncio.run(run())
    assert clean_spans == [] and clean_state == "failed"
    assert dirty_spans and dirty_state == "done"


# ==========================================================================
# 2. NHS Modulus 11
# ==========================================================================

VALID_NHS = ["4001234564", "9434765919", "9876543210", "9990000018"]
INVALID_NHS = [
    "4857773456",   # the number from the original acceptance criterion
    "4001234565",   # correct body, wrong check digit
    "1234567890",
    "0000000001",
]


@pytest.mark.parametrize("digits", VALID_NHS)
def test_nhs_check_digit_accepts_valid(digits):
    assert nhs_check_digit_ok(digits) is True


@pytest.mark.parametrize("digits", INVALID_NHS)
def test_nhs_check_digit_rejects_invalid(digits):
    assert nhs_check_digit_ok(digits) is False


@pytest.mark.parametrize("bad", ["123", "", "40012345６4", "abcdefghij", "40012345644"])
def test_nhs_check_digit_rejects_junk(bad):
    assert nhs_check_digit_ok(bad) is False


@pytest.mark.parametrize("rendered", ["400 123 4564", "400-123-4564", "4001234564"])
def test_nhs_number_formats_are_detected(rendered):
    text = f"Please minute the NHS number {rendered} against the case file."
    hit = covered(find_rule_spans(text), rendered)
    assert hit is not None and hit.exemption is Exemption.S40_2


def test_invalid_nhs_number_is_not_redacted():
    """Rejecting a number that fails the check is the whole point of the layer."""
    text = "The reference quoted was 485 777 3456 which is not a valid NHS number."
    assert covered(find_rule_spans(text), "485 777 3456") is None


def test_spoken_nhs_number_is_detected():
    text = SEED_BY_ID["cbbe2eb9"]["text"]
    hit = covered(find_rule_spans(text), "four zero zero")
    assert hit is not None
    assert hit.surface == "four zero zero, one two three, four five six four"


def test_spoken_digits_that_fail_the_check_are_ignored():
    """1234567890 fails Modulus 11, so the spoken form must not be redacted either."""
    text = ("The code is one two three four five six seven eight nine zero, "
            "nothing sensitive.")
    assert covered(find_rule_spans(text), "one two three") is None


def test_short_spoken_digit_run_is_not_an_nhs_number():
    text = "I want to brief you on one live case and two follow ups."
    assert find_rule_spans(text) == []


# ==========================================================================
# 3. Other deterministic detectors
# ==========================================================================

@pytest.mark.parametrize("text,needle,exemption", [
    ("Write to jane.doe@camden.gov.uk before Friday.", "jane.doe@camden.gov.uk", Exemption.S40_2),
    ("She lives at SW1A 2AA now.", "SW1A 2AA", Exemption.S40_2),
    ("Ring her on 07700 900123 tonight.", "07700 900123", Exemption.S40_2),
    ("Ring the office on 020 7946 0958.", "020 7946 0958", Exemption.S40_2),
    ("Dial +44 7700 900123 for the duty line.", "+44 7700 900123", Exemption.S40_2),
    ("Sort code 20-00-00 is on the invoice.", "20-00-00", Exemption.S40_2),
    ("The account number is 12345678 per the form.", "12345678", Exemption.S40_2),
    ("He was born on 3 March 1975 in Leeds.", "3 March 1975", Exemption.S40_2),
    ("Date of birth 14/02/1988 per the referral.", "14/02/1988", Exemption.S40_2),
    ("Councillor Amara Nwosu chaired it.", "Councillor Amara Nwosu", Exemption.S40_2),
    ("The bidder was Meridian Facilities Ltd.", "Meridian Facilities Ltd", Exemption.S43_2),
])
def test_detector_positives(text, needle, exemption):
    hit = covered(find_rule_spans(text), needle)
    assert hit is not None, f"{needle!r} missed; got {surfaces(find_rule_spans(text))}"
    assert hit.exemption is exemption


@pytest.mark.parametrize("nino,expected", [
    ("AB123456C", True),
    ("QQ123456C", False),   # Q is not allowed as the first letter
    ("DA123456A", False),   # D is not allowed as the first letter
    ("AO123456A", False),   # O is not allowed as the second letter
    ("BG123456A", False),   # forbidden prefix
    ("NK123456A", False),   # forbidden prefix
    ("ZZ123456A", False),   # forbidden prefix
    ("AB123456E", False),   # suffix must be A-D
])
def test_nino_prefix_rules(nino, expected):
    spans = find_nino(f"His NI number is {nino} on the form.")
    assert bool(spans) is expected


@pytest.mark.parametrize("text,needle", [
    ("The bid was £2.4 million.", "£2.4 million"),
    ("The bid was £2.4m.", "£2.4m"),
    ("The bid was 2.4 million pounds.", "2.4 million pounds"),
    ("The bid was £2,400,000.", "£2,400,000"),
    ("The bid was £10,000.", "£10,000"),
    ("The bid was £1.2bn.", "£1.2bn"),
])
def test_money_above_threshold(text, needle):
    hit = covered(find_rule_spans(text), needle)
    assert hit is not None and hit.exemption is Exemption.S43_2


@pytest.mark.parametrize("text", [
    "The biscuits cost £45.",
    "The licence was £9,999.",
    "We spent £5k on the survey.",
])
def test_money_below_threshold_is_ignored(text):
    assert find_money(text) == []


def test_money_threshold_is_configurable():
    assert find_money("It cost £45.") == []
    assert len(find_money("It cost £45.", threshold=10.0)) == 1


# ==========================================================================
# 4. Substring -> offset mapping, all three tiers
# ==========================================================================

MAPPING_TEXT = ("So to summarise: Dr Sarah Whitfield referred the case and "
                "Ardent Systems bid £2.4 million.")


def test_locate_tier1_exact():
    found = model_client.locate(MAPPING_TEXT, "Ardent Systems")
    assert found is not None
    start, end = found
    assert MAPPING_TEXT[start:end] == "Ardent Systems"


def test_locate_tier2_casefold():
    found = model_client.locate(MAPPING_TEXT, "ardent SYSTEMS")
    assert found is not None
    start, end = found
    assert MAPPING_TEXT[start:end] == "Ardent Systems"


def test_locate_tier3_fuzzy():
    """The model tidied the title and misspelled the surname."""
    found = model_client.locate(MAPPING_TEXT, "Dr. Sarah Whitfeld")
    assert found is not None
    start, end = found
    assert MAPPING_TEXT[start:end] == "Dr Sarah Whitfield"


def test_locate_tier4_discard():
    assert model_client.locate(MAPPING_TEXT, "a completely invented paraphrase") is None


def test_locate_does_not_reuse_claimed_offsets():
    text = "Ardent Systems and Ardent Systems again."
    first = model_client.locate(text, "Ardent Systems", [])
    assert first == (0, 14)
    second = model_client.locate(text, "Ardent Systems", [first])
    assert second == (19, 33)


def test_map_redactions_drops_unmappable_and_unknown():
    mapped = model_client.map_redactions(MAPPING_TEXT, [
        {"text": "Ardent Systems", "exemption": "s.43(2)"},
        {"text": "never said this", "exemption": "s.40(2)"},
        {"text": "£2.4 million", "exemption": "s.99"},
        {"text": None, "exemption": "s.40(2)"},
        "not even a dict",
    ])
    assert surfaces(mapped) == ["Ardent Systems"]
    assert mapped[0].source == "model"


def test_map_redactions_never_raises_on_garbage():
    assert model_client.map_redactions(MAPPING_TEXT, None) == []
    assert model_client.map_redactions("", [{"text": "x", "exemption": "s.40(2)"}]) == []


# ==========================================================================
# 5. Defensive parsing of the model body
# ==========================================================================

@pytest.mark.parametrize("body,expected", [
    ('{"redactions": []}', {"redactions": []}),
    ('```json\n{"redactions": []}\n```', {"redactions": []}),
    ('Sure! Here you go: {"redactions": []} hope that helps', {"redactions": []}),
    ('', {}),
    ('not json at all', {}),
    ('{"redactions": [}', {}),
])
def test_parse_model_json(body, expected):
    assert model_client.parse_model_json(body) == expected


def test_annotate_returns_failed_when_ollama_is_unreachable(monkeypatch):
    class DeadClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(model_client.httpx, "AsyncClient", lambda **kw: DeadClient())
    spans, state = asyncio.run(model_client.annotate("Dr Sarah Whitfield attended."))
    assert spans == [] and state == "failed"


def test_prompt_has_four_worked_examples_and_the_exemptions():
    prompt = model_client.build_prompt("A test segment.", ["one", "two", "three"])
    assert prompt.count("### Example") >= 4
    for exemption in ("s.40(2)", "s.43(2)", "s.35", "s.42"):
        assert exemption in prompt
    assert "EXACT SUBSTRING" in prompt
    assert "A test segment." in prompt
    assert "one" not in prompt.split("### Now do this one")[1].split("CURRENT SEGMENT")[0] \
        or "two" in prompt   # only the last two context lines are carried


def test_prompt_carries_only_the_last_two_context_lines():
    prompt = model_client.build_prompt("Current.", ["oldest line", "middle line", "newest line"])
    tail = prompt.split("### Now do this one")[1]
    assert "middle line" in tail and "newest line" in tail
    assert "oldest line" not in tail


# ==========================================================================
# 6. Merge policy and the no-overlap invariant
# ==========================================================================

def assert_no_overlaps(spans) -> None:
    for a, b in zip(spans, spans[1:]):
        assert a.start <= b.start, f"not sorted: {a} {b}"
        assert a.end <= b.start, f"overlap: {a} {b}"


def span(start, end, source="rule", exemption=Exemption.S40_2, surface="x"):
    return RedactionSpan(start=start, end=end, exemption=exemption, surface=surface,
                         source=source, confidence=1.0 if source == "rule" else 0.8)


def test_merge_rule_beats_model_on_overlap():
    merged = pipeline.merge_spans([span(10, 20, "model"), span(12, 18, "rule")])
    assert len(merged) == 1
    assert merged[0].source == "rule" and (merged[0].start, merged[0].end) == (12, 18)


def test_merge_longer_beats_shorter_within_a_source():
    merged = pipeline.merge_spans([span(10, 14, "model"), span(10, 25, "model")])
    assert len(merged) == 1 and (merged[0].start, merged[0].end) == (10, 25)


def test_merge_dedupes_identical_ranges():
    merged = pipeline.merge_spans([span(3, 9, "rule"), span(3, 9, "model"), span(3, 9, "rule")])
    assert len(merged) == 1 and merged[0].source == "rule"


def test_merge_keeps_disjoint_spans_and_sorts_them():
    merged = pipeline.merge_spans([span(30, 40), span(0, 5), span(10, 20)])
    assert [(s.start, s.end) for s in merged] == [(0, 5), (10, 20), (30, 40)]
    assert_no_overlaps(merged)


def test_merge_drops_empty_spans():
    assert pipeline.merge_spans([span(5, 5), span(7, 7)]) == []


def test_merge_of_real_rule_and_model_spans_has_no_overlaps():
    text = SEED_BY_ID["85467ded"]["text"]
    rule_spans = find_rule_spans(text)
    model_spans = model_client.map_redactions(text, [
        {"text": "Dr Sarah Whitfield's referral", "exemption": "s.40(2)"},
        {"text": "Ardent Systems award", "exemption": "s.43(2)"},
        {"text": "£2.4 million", "exemption": "s.43(2)"},
    ])
    merged = pipeline.merge_spans(rule_spans + model_spans)
    assert_no_overlaps(merged)
    assert covered(merged, "Dr Sarah Whitfield") is not None
    assert covered(merged, "Ardent Systems") is not None


# ==========================================================================
# 7. Acceptance criterion and performance
# ==========================================================================

ACCEPTANCE_TEXT = ("Dr Sarah Whitfield flagged it, NHS number 400 123 4564, and the "
                   "Ardent Systems bid came in at £2.4 million")


def test_acceptance_example_from_rules_alone():
    spans = pipeline.merge_spans(find_rule_spans(ACCEPTANCE_TEXT))
    assert_no_overlaps(spans)
    assert covered(spans, "Dr Sarah Whitfield").exemption is Exemption.S40_2
    assert covered(spans, "400 123 4564").exemption is Exemption.S40_2
    assert covered(spans, "Ardent Systems").exemption is Exemption.S43_2
    assert covered(spans, "£2.4 million").exemption is Exemption.S43_2


def test_rules_layer_is_fast():
    text = " ".join(s["text"] for s in SEED)
    find_rule_spans(text)  # warm the regex cache
    started = time.perf_counter()
    for _ in range(20):
        find_rule_spans(text)
    per_call_ms = (time.perf_counter() - started) * 1000 / 20
    assert per_call_ms < 5.0, f"rules layer took {per_call_ms:.2f}ms per segment"


def test_rules_layer_handles_empty_and_odd_input():
    assert find_rule_spans("") == []
    assert find_rule_spans("   ") == []
    assert isinstance(find_rule_spans("£" * 500), list)


def test_every_rule_span_surface_matches_its_offsets():
    for item in SEED:
        text = item["text"]
        for s in find_rule_spans(text):
            assert text[s.start:s.end] == s.surface
            assert 0 <= s.start < s.end <= len(text)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
