"""Score the redaction prompt against both transcripts.

Prompt changes for this model are not monotonic: teaching it one distinction has
repeatedly cost another. Run this before and after any edit to backend/redact/model.py
instead of eyeballing one transcript.

    .venv/bin/python tools/score_redaction.py
"""
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.redact.model import annotate, warm

# (utterance index, substring that must be covered, expected section)
EXPECTED = {
    "demo/seed_transcript.json": [
        (2, "Sarah Whitfield", "s.40(2)"),
        (3, "four five six four", "s.40(2)"),
        (4, "Ardent Systems", "s.43(2)"),
        (4, "2.4 million", "s.43(2)"),
        (5, "formulation", "s.35"),
        (6, "legal advice", "s.42"),
        (7, "Sarah Whitfield", "s.40(2)"),
        (7, "four five six four", "s.40(2)"),
        (7, "Ardent Systems", "s.43(2)"),
        (7, "2.4 million", "s.43(2)"),
    ],
    "demo/unseen_regression.json": [
        (2, "Idris Bakare", "s.40(2)"),
        (2, "seven C", "s.40(2)"),          # the NINO must keep its trailing letter
        (3, "COPD", "s.38"),
        (5, "M14 5TQ", "s.40(2)"),
        (5, "three one eight", "s.40(2)"),  # phone
        (5, "okonjo", "s.40(2)"),           # email
        (7, "Pennine Damp Solutions", "s.43(2)"),
        (7, "forty thousand pounds", "s.43(2)"),
        (7, "thirty four dash fifty six", "s.40(2)"),
        (7, "two nine one four four zero", "s.40(2)"),
        (9, "investigation", "s.31"),
        (10, "identity", "s.41"),
        (11, "Counsel's opinion", "s.42"),
        (12, "still a draft", "s.35"),
    ],
}

# utterances that must produce NOTHING
TRAPS = {
    "demo/seed_transcript.json": [1, 8],
    "demo/unseen_regression.json": [1, 4, 6, 8],
}


async def score(path):
    segs = json.loads(Path(path).read_text())
    ctx, got, lat = [], {}, []
    for i, s in enumerate(segs, 1):
        t0 = time.perf_counter()
        spans, state = await annotate(s["text"], ctx[-1:])
        lat.append((time.perf_counter() - t0) * 1000)
        got[i] = spans
        if state != "done":
            print(f"    !! U{i} {state}")
        ctx.append(s["text"])

    hits = misses = 0
    for idx, needle, section in EXPECTED[path]:
        found = next((sp for sp in got.get(idx, [])
                      if needle.casefold() in sp.surface.casefold()), None)
        if found is None:
            misses += 1
            print(f"    MISS  U{idx} {needle!r}")
        elif found.exemption.value != section:
            misses += 1
            print(f"    WRONG U{idx} {needle!r} -> {found.exemption.value}, want {section}")
        else:
            hits += 1

    false_pos = 0
    for idx in TRAPS[path]:
        for sp in got.get(idx, []):
            false_pos += 1
            print(f"    FALSE U{idx} {sp.surface[:44]!r}")

    lat.sort()
    total = len(EXPECTED[path])
    print(f"  {path}: {hits}/{total} correct, {misses} missed/wrong, "
          f"{false_pos} false positives, p50 {lat[len(lat)//2]:.0f}ms")
    return hits, total, false_pos


async def main():
    await warm()
    th = tt = tf = 0
    for path in EXPECTED:
        h, t, f = await score(path)
        th += h; tt += t; tf += f
    print(f"\nTOTAL {th}/{tt} correct, {tf} false positives")
    return 0 if th == tt and tf == 0 else 1


sys.exit(asyncio.run(main()))
