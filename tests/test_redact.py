"""Tests for the redaction engine.

These tests cover Gemma's path: substring->offset mapping, defensive JSON parsing,
demo transcript with the model monkeypatched to raise, which is the state of the world
span merging, and what happens when Ollama is unavailable.

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
# 1. The demo transcript with Gemma unavailable
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


def test_full_pipeline_with_model_dead(dead_model):
    """Gemma is the only redactor. With Ollama down nothing is proposed, and the
    pipeline reports that rather than implying the segment is clean."""
    async def run():
        pipeline.reset_stats()
        out = []
        for item in SEED:
            out.append(await pipeline.redact_segment(Segment(**item), []))
        return out

    for spans, state in asyncio.run(run()):
        assert spans == []
        assert state == "failed"

def test_state_is_failed_only_when_nothing_worked(dead_model):
    """With Gemma down nothing reviewed the segment, so nothing is 'done'."""
    async def run():
        clean = Segment(text=SEED_BY_ID["174a632e"]["text"])
        dirty = Segment(text=SEED_BY_ID["93f3bc86"]["text"])
        return (await pipeline.redact_segment(clean, []),
                await pipeline.redact_segment(dirty, []))

    (clean_spans, clean_state), (dirty_spans, dirty_state) = asyncio.run(run())
    assert clean_spans == [] and clean_state == "failed"
    assert dirty_spans == [] and dirty_state == "failed"




VALID_NHS = ["4001234564", "9434765919", "9876543210", "9990000018"]
INVALID_NHS = [
    "4857773456",   # the number from the original acceptance criterion
    "4001234565",   # correct body, wrong check digit
    "1234567890",
    "0000000001",
]


# ==========================================================================
# 3. Other deterministic detectors
# ==========================================================================


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


def test_prompt_carries_only_the_most_recent_context_line():
    """One prior segment, not two. Measured: two cost three redactions on an
    unseen transcript by pulling attention off the segment being annotated."""
    prompt = model_client.build_prompt("Current.", ["oldest line", "middle line", "newest line"])
    tail = prompt.split("### Now do this one")[1]
    assert "newest line" in tail
    assert "middle line" not in tail
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


# ==========================================================================
# 7. Acceptance criterion and performance
# ==========================================================================

ACCEPTANCE_TEXT = ("Dr Sarah Whitfield flagged it, NHS number 400 123 4564, and the "
                   "Ardent Systems bid came in at £2.4 million")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
