"""FOI release export.

Given a meeting's Segments (each already carrying RedactionSpans), build ONE
self-contained HTML file with two artefacts:

  a) FOI RELEASE COPY   — the transcript with every redacted span replaced by
     a solid black bar sized to the length of the redacted text.
  b) SCHEDULE OF REDACTIONS — a numbered table of every redaction, keyed by
     the same reference numbers used in (a), citing the FOIA 2000 exemption.

The redacted surface text itself is never written into the output — only its
character count (for bar width) crosses into the HTML. That is the point of
the document: a redaction that leaks the redacted text (in a title attribute,
an HTML comment, alt text) is not a redaction.

HTML -> print-to-PDF is the target output path, so this file styles for A4
print rather than shipping a PDF library.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from shared.contracts import (
    Exemption,
    EXEMPTION_LABEL,
    EXEMPTION_STATUTE,
    RedactionSpan,
    Segment,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORT_DIR = REPO_ROOT / "exports"


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _bar_width_ch(char_count: int) -> float:
    """Width, in ch units, of the black bar that stands in for a redacted span.

    Measured from the characters actually blacked out rather than from
    RedactionSpan.surface, so a stale or mis-sized surface can never make the
    bar disagree with the text it covers.
    """
    return max(1.6, char_count * 0.62)


def _coerce_exemption(exemption) -> Optional[Exemption]:
    if isinstance(exemption, Exemption):
        return exemption
    try:
        return Exemption(exemption)
    except (ValueError, TypeError):
        return None


def _document_ref(title: str, segments: list[Segment]) -> str:
    """A stable-per-content reference number, so the same transcript always
    carries the same document ref even if the export is rebuilt."""
    basis = title + "|" + "|".join(seg.id for seg in segments)
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:8].upper()
    return f"FOI-{digest}"


def _covered_ranges(segment: Segment) -> list[tuple[int, int, RedactionSpan]]:
    """Clamped, non-overlapping ranges to black out, in document order.

    Offsets arrive from a language model and are not trusted: they can fall
    outside the text, run backwards, or overlap a neighbour. A span that is
    merely skipped leaves its characters in the release copy, so every span is
    clamped into the text and trimmed to start where the previous one ended.
    A span left with nothing of its own to cover is dropped, because an earlier
    bar already hides all of it.
    """
    length = len(segment.text)
    clamped: list[tuple[int, int, RedactionSpan]] = []
    for sp in segment.spans:
        try:
            start = min(max(int(sp.start), 0), length)
            end = min(max(int(sp.end), 0), length)
        except (TypeError, ValueError):
            continue
        if end > start:
            clamped.append((start, end, sp))

    clamped.sort(key=lambda item: (item[0], -item[1]))
    ranges: list[tuple[int, int, RedactionSpan]] = []
    cursor = 0
    for start, end, sp in clamped:
        start = max(start, cursor)
        if end <= start:
            continue
        ranges.append((start, end, sp))
        cursor = end
    return ranges


def _render_segment_html(segment: Segment, first_ref: int
                         ) -> tuple[str, list[tuple[RedactionSpan, int]]]:
    """Render one utterance's text, replacing each redacted span with a bar.

    Walks the text left to right; anything not covered by a span is
    HTML-escaped and copied through verbatim, anything covered by a span is
    replaced by a bar. The original characters of a redacted span never reach
    the returned string, and neither does RedactionSpan.surface.

    Returns the markup and the spans that earned a bar, each with the reference
    number printed beside it, so the schedule can be built from exactly the
    redactions the reader can see.
    """
    text = segment.text
    out: list[str] = []
    applied: list[tuple[RedactionSpan, int]] = []
    cursor = 0
    for start, end, sp in _covered_ranges(segment):
        if start > cursor:
            out.append(html.escape(text[cursor:start]))
        ref = first_ref + len(applied)
        width = _bar_width_ch(end - start)
        out.append(
            f'<span class="redbar" style="width:{width:.2f}ch" '
            f'role="img" aria-label="redacted text"></span>'
            f'<sup class="refnum" id="ref-{ref}">'
            f'<a href="#sched-{ref}">{ref}</a></sup>'
        )
        applied.append((sp, ref))
        cursor = end
    if cursor < len(text):
        out.append(html.escape(text[cursor:]))
    return "".join(out), applied


_CSS = r"""
:root {
  --ink: #111111;
  --rule: #999999;
  --paper: #ffffff;
  --muted: #444444;
  --band: #f2f2f2;
}
* { box-sizing: border-box; }
@page {
  size: A4;
  margin: 24mm 18mm 20mm 18mm;
}
body {
  background: var(--paper);
  color: var(--ink);
  font-family: Georgia, "Times New Roman", Times, serif;
  font-size: 11pt;
  line-height: 1.45;
  margin: 0;
  padding: 0 6mm;
}
.classification-bar {
  text-align: center;
  font-family: Arial, Helvetica, sans-serif;
  font-weight: bold;
  letter-spacing: 0.06em;
  font-size: 9pt;
  border: 1px solid var(--ink);
  padding: 2px 0;
  margin: 0 0 3mm 0;
}
header.masthead {
  border-bottom: 3px double var(--ink);
  padding-bottom: 2mm;
  margin-bottom: 3mm;
}
header.masthead .crest-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-family: Arial, Helvetica, sans-serif;
}
header.masthead .authority {
  font-size: 9pt;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
}
header.masthead h1 {
  font-size: 16pt;
  margin: 2mm 0 1mm 0;
}
header.masthead .doctype {
  font-size: 10.5pt;
  font-style: italic;
  color: var(--muted);
  margin: 0 0 2mm 0;
}
table.meta-table {
  width: 100%;
  border-collapse: collapse;
  font-family: Arial, Helvetica, sans-serif;
  font-size: 9pt;
}
table.meta-table td {
  border-top: 1px solid var(--rule);
  border-bottom: 1px solid var(--rule);
  padding: 2px 6px;
}
table.meta-table td.label {
  color: var(--muted);
  width: 32%;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-size: 8pt;
}
section.transcript { margin-top: 3mm; }
section.transcript h2, section.schedule h2 {
  font-family: Arial, Helvetica, sans-serif;
  font-size: 12pt;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  border-bottom: 1px solid var(--ink);
  padding-bottom: 1mm;
  margin-bottom: 2mm;
}
.utterance {
  display: flex;
  gap: 4mm;
  padding: 1mm 0;
  border-bottom: 1px solid #e5e5e5;
  break-inside: avoid;
}
.utterance .meta {
  flex: 0 0 34mm;
  font-family: Arial, Helvetica, sans-serif;
  font-size: 8.5pt;
  color: var(--muted);
}
.utterance .meta .ts { display: block; }
.utterance .meta .speaker {
  display: block;
  font-weight: bold;
  color: var(--ink);
  text-transform: uppercase;
  letter-spacing: 0.02em;
}
.utterance .text { flex: 1; }
.redbar {
  display: inline-block;
  height: 0.8em;
  background: #000000;
  border-radius: 1px;
  vertical-align: -0.05em;
}
sup.refnum {
  font-size: 0.68em;
  margin-left: 1px;
  line-height: 0;
}
sup.refnum a {
  color: var(--ink);
  text-decoration: none;
}
section.schedule {
  margin-top: 5mm;
}
section.schedule p.lede {
  font-size: 9pt;
  color: var(--muted);
  margin-top: 0;
  margin-bottom: 2mm;
}
table.schedule-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 8.5pt;
}
table.schedule-table tr { break-inside: avoid; }
table.schedule-table th, table.schedule-table td {
  border: 1px solid var(--rule);
  padding: 1.2mm 2mm;
  text-align: left;
  vertical-align: top;
}
table.schedule-table th {
  background: var(--band);
  font-family: Arial, Helvetica, sans-serif;
  font-size: 8pt;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
table.schedule-table td.num {
  text-align: center;
  font-weight: bold;
  width: 6%;
}
table.schedule-table td.ts { width: 10%; white-space: nowrap; }
table.schedule-table td.exemption { width: 16%; white-space: nowrap; }
footer.docfooter {
  margin-top: 10mm;
  padding-top: 3mm;
  border-top: 1px solid var(--rule);
  font-family: Arial, Helvetica, sans-serif;
  font-size: 8pt;
  color: var(--muted);
  text-align: center;
}
@media print {
  .classification-bar { border-color: #000; }
}
"""


def build_export(segments: list[Segment], title: str, classification: str) -> str:
    """Build the self-contained FOI release HTML for one meeting.

    Returns the full HTML document as a string. Never writes to disk itself
    — see write_and_open_export for that.
    """
    generated = datetime.now(timezone.utc)
    doc_ref = _document_ref(title, segments)

    ordered_spans: list[tuple[Segment, RedactionSpan]] = []
    transcript_rows = []
    for seg in segments:
        body, applied = _render_segment_html(seg, len(ordered_spans) + 1)
        ordered_spans.extend((seg, sp) for sp, _ in applied)
        transcript_rows.append(
            f'<div class="utterance">'
            f'<div class="meta"><span class="ts">{_format_timestamp(seg.t_start)}</span>'
            f'<span class="speaker">{html.escape(seg.speaker)}</span></div>'
            f'<div class="text">{body}</div>'
            f'</div>'
        )
    transcript_html = "\n".join(transcript_rows) if transcript_rows else (
        '<p><em>No utterances recorded for this session.</em></p>'
    )

    schedule_rows = []
    for i, (seg, sp) in enumerate(ordered_spans, start=1):
        exemption = _coerce_exemption(sp.exemption)
        section_code = exemption.value if exemption else str(sp.exemption)
        label = EXEMPTION_LABEL.get(exemption, "") if exemption else ""
        statute_desc, tier = EXEMPTION_STATUTE.get(exemption, ("Unclassified exemption", "qualified")) if exemption else ("Unclassified exemption", "qualified")
        if tier == "absolute":
            test_note = "Absolute exemption — no public interest test applies."
        else:
            test_note = (
                "Qualified — public interest test pending (s.2 FOIA 2000): weigh "
                "disclosure against maintaining the exemption."
            )
        exemption_cell = f"{html.escape(section_code)}" + (f" — {html.escape(label)}" if label else "")
        schedule_rows.append(f"""
          <tr id="sched-{i}">
            <td class="num">{i}</td>
            <td class="ts">{_format_timestamp(seg.t_start)}</td>
            <td class="exemption">{exemption_cell}</td>
            <td>{html.escape(statute_desc)}</td>
            <td>{html.escape(test_note)}</td>
          </tr>""")
    schedule_html = "\n".join(schedule_rows) if schedule_rows else (
        '<tr><td colspan="5"><em>No redactions were applied to this transcript.</em></td></tr>'
    )

    safe_title = html.escape(title)
    safe_classification = html.escape(classification)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_title} — FOI Release Copy</title>
<style>{_CSS}</style>
</head>
<body>
  <div class="classification-bar">FOI RELEASE COPY — OFFICIAL (redacted for disclosure under the Freedom of Information Act 2000)</div>

  <header class="masthead">
    <div class="crest-row">
      <span class="authority">Redline meeting record</span>
      <span class="authority">Ref: {html.escape(doc_ref)}</span>
    </div>
    <h1>{safe_title}</h1>
    <p class="doctype">FOI Release Copy &amp; Schedule of Redactions</p>
    <table class="meta-table">
      <tr><td class="label">Document reference</td><td>{html.escape(doc_ref)}</td></tr>
      <tr><td class="label">Original classification</td><td>{safe_classification}</td></tr>
      <tr><td class="label">Release status</td><td>OFFICIAL — redacted for public disclosure</td></tr>
      <tr><td class="label">Generated</td><td>{generated.strftime('%d %B %Y, %H:%M')} UTC</td></tr>
      <tr><td class="label">Redactions applied</td><td>{len(ordered_spans)}</td></tr>
    </table>
  </header>

  <section class="transcript">
    <h2>Transcript — FOI Release Copy</h2>
    {transcript_html}
  </section>

  <section class="schedule">
    <h2>Schedule of Redactions</h2>
    <p class="lede">Every numbered reference below corresponds to one redacted
    passage in the transcript above. Absolute exemptions require no further
    test; qualified exemptions require the public interest test noted.</p>
    <table class="schedule-table">
      <thead>
        <tr>
          <th>Ref</th>
          <th>Time</th>
          <th>Exemption</th>
          <th>Statutory basis</th>
          <th>Applied test</th>
        </tr>
      </thead>
      <tbody>
        {schedule_html}
      </tbody>
    </table>
  </section>

  <footer class="docfooter">
    End of document — {html.escape(doc_ref)} — produced by REDLINE, fully on-device, zero cloud egress.
  </footer>
</body>
</html>
"""


def write_and_open_export(html_str: str, filename_hint: str = "export", open_browser: bool = True) -> Path:
    """Write the export HTML under exports/ and open it in the browser.

    Never raises: a failure to open a browser (headless box, no display) is
    swallowed, since the file is already safely on disk at that point.
    """
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_hint = "".join(c if c.isalnum() or c in "-_" else "_" for c in filename_hint).strip("_")[:60] or "export"
    path = (EXPORT_DIR / f"{safe_hint}-{timestamp}.html").resolve()
    path.write_text(html_str, encoding="utf-8")
    if open_browser:
        try:
            webbrowser.open(path.as_uri())
        except Exception:
            pass
    return path


# ---------------------------------------------------------------------------
# Demo span synthesis for standalone testing. demo/seed_transcript.json ships
# with empty spans, so we build plausible ones locally rather than importing
# backend/redact/ (owned by another agent, being written concurrently).
# ---------------------------------------------------------------------------
_DEMO_REDACTION_CANDIDATES: list[tuple[str, Exemption]] = [
    ("Dr Sarah Whitfield's referral", Exemption.S40_2),
    ("Dr Sarah Whitfield", Exemption.S40_2),
    ("four zero zero, one two three, four five six four", Exemption.S38),
    ("a child open on the at-risk register", Exemption.S40_2),
    ("Ardent Systems", Exemption.S43_2),
    ("£2.4 million", Exemption.S43_2),
    ("is still in formulation", Exemption.S35),
    ("legal advice from the council's solicitor", Exemption.S42),
]


def _synthesize_demo_spans(segments: list[Segment]) -> list[Segment]:
    """Fill in RedactionSpans by exact substring match, longest phrase first
    so a nested shorter phrase (e.g. "Dr Sarah Whitfield" inside "Dr Sarah
    Whitfield's referral") never re-claims already-redacted characters."""
    candidates = sorted(_DEMO_REDACTION_CANDIDATES, key=lambda c: -len(c[0]))
    for seg in segments:
        claimed: list[tuple[int, int]] = []
        spans: list[RedactionSpan] = []
        for substring, exemption in candidates:
            search_from = 0
            while True:
                idx = seg.text.find(substring, search_from)
                if idx == -1:
                    break
                end = idx + len(substring)
                overlaps = any(idx < c_end and end > c_start for c_start, c_end in claimed)
                if not overlaps:
                    spans.append(RedactionSpan(
                        start=idx, end=end, exemption=exemption,
                        surface=substring, source="rule", confidence=1.0,
                    ))
                    claimed.append((idx, end))
                search_from = idx + 1
        spans.sort(key=lambda sp: sp.start)
        seg.spans = spans
        seg.redaction_state = "done" if spans else "pending"
    return segments


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a REDLINE FOI export from a transcript JSON file."
    )
    parser.add_argument("transcript", help="Path to a JSON file of Segment records")
    parser.add_argument("--title", default="Joint Procurement and Safeguarding Review")
    parser.add_argument("--classification", default="OFFICIAL-SENSITIVE")
    parser.add_argument("--no-open", action="store_true", help="Skip opening a browser")
    args = parser.parse_args()

    raw = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
    demo_segments = [Segment(**item) for item in raw]
    _synthesize_demo_spans(demo_segments)

    export_html = build_export(demo_segments, args.title, args.classification)
    out_path = write_and_open_export(
        export_html, filename_hint=args.title, open_browser=not args.no_open
    )

    total_spans = sum(len(s.spans) for s in demo_segments)
    print(f"Wrote export: {out_path}")
    print(f"Segments: {len(demo_segments)}  Redacted spans: {total_spans}")


if __name__ == "__main__":
    main()
