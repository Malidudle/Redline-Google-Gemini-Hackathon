import React from 'react'

// Transcribed verbatim from shared/contracts.py. Keys are Exemption values.
export const EXEMPTION_LABEL = {
  's.40(2)': 'personal data',
  's.41': 'provided in confidence',
  's.43(2)': 'commercial interests',
  's.36': 'conduct of public affairs',
  's.38': 'health and safety',
  's.31': 'law enforcement',
  's.42': 'legal privilege',
  's.35': 'policy formulation'
}

export const EXEMPTION_COLOUR = {
  's.40(2)': '#C2410C',
  's.41': '#7C3AED',
  's.43(2)': '#0F766E',
  's.36': '#B45309',
  's.38': '#BE123C',
  's.31': '#1D4ED8',
  's.42': '#4D7C0F',
  's.35': '#9333EA'
}

export const EXEMPTION_STATUTE = {
  's.40(2)': ['Personal information', 'absolute'],
  's.41': ['Information provided in confidence', 'absolute'],
  's.43(2)': ['Commercial interests', 'qualified'],
  's.36': ['Prejudice to effective conduct of public affairs', 'qualified'],
  's.38': ['Health and safety', 'qualified'],
  's.31': ['Law enforcement', 'qualified'],
  's.42': ['Legal professional privilege', 'qualified'],
  's.35': ['Formulation of government policy', 'qualified']
}

const FALLBACK_COLOUR = '#111315'

export function colourFor (exemption) {
  return EXEMPTION_COLOUR[exemption] || FALLBACK_COLOUR
}

export function tint (hex, alpha) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex || '')
  if (!m) return 'rgba(0,0,0,0.08)'
  const n = parseInt(m[1], 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

export function timecode (seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0))
  return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0')
}

// Walk the spans and cut the text into alternating plain / redacted parts.
// Overlapping or out-of-range spans are dropped rather than allowed to corrupt the text.
export function buildParts (text, spans) {
  const usable = (spans || [])
    .map((sp, i) => ({ sp, i }))
    .filter(({ sp }) =>
      sp && Number.isFinite(sp.start) && Number.isFinite(sp.end) &&
      sp.start >= 0 && sp.end <= text.length && sp.end > sp.start)
    .sort((a, b) => a.sp.start - b.sp.start)

  const parts = []
  let cursor = 0
  for (const { sp, i } of usable) {
    if (sp.start < cursor) continue
    if (sp.start > cursor) parts.push({ kind: 'plain', text: text.slice(cursor, sp.start) })
    parts.push({ kind: 'red', text: text.slice(sp.start, sp.end), span: sp, index: i })
    cursor = sp.end
  }
  if (cursor < text.length) parts.push({ kind: 'plain', text: text.slice(cursor) })
  return parts
}

const TOKEN_STAGGER_MS = 20

function RedactedSpan ({ segmentId, part, open, removed, compact, onPick }) {
  const exemption = part.span.exemption
  const colour = colourFor(exemption)
  const style = { '--exc': colour, '--exbg': tint(colour, 0.1) }
  const pick = (e) => { e.stopPropagation(); onPick(e, segmentId, part.index, part.span) }

  if (removed) {
    return (
      <span className="rx" style={style}>
        <span className="unredacted" onClick={pick} title="Redaction removed by operator">{part.text}</span>
      </span>
    )
  }

  const pieces = part.text.split(/(\s+)/).filter((p) => p.length > 0)
  const wordIdx = []
  pieces.forEach((p, i) => { if (!/^\s+$/.test(p)) wordIdx.push(i) })
  const firstWord = wordIdx[0]
  const lastWord = wordIdx[wordIdx.length - 1]

  let order = 0
  const chipDelay = 230 + wordIdx.length * TOKEN_STAGGER_MS

  return (
    <span className={'rx' + (open ? ' open' : '')} style={style}>
      {pieces.map((piece, i) => {
        if (/^\s+$/.test(piece)) return <span key={i}>{piece}</span>
        const barStyle = {
          left: i === firstWord ? '-0.12em' : '-0.34em',
          right: i === lastWord ? '-0.12em' : '-0.34em',
          animationDelay: (order++ * TOKEN_STAGGER_MS) + 'ms'
        }
        return (
          <span key={i} className="tok" onClick={pick}>
            {piece}
            <span className="bar" style={barStyle} aria-hidden="true" />
          </span>
        )
      })}
      <span className="chip" style={{ animationDelay: chipDelay + 'ms' }} onClick={pick}>
        <span className="code">{exemption}</span>
        {compact ? null : <span className="lab">{EXEMPTION_LABEL[exemption] || 'exempt'}</span>}
      </span>
    </span>
  )
}

function Utterance ({ segmentId, seg, redact, removed, openKey, onPick }) {
  if (!redact || !seg.spans || seg.spans.length === 0) {
    return (
      <div className={'utt' + (seg.final ? '' : ' interim')}>
        {seg.text}
      </div>
    )
  }
  const parts = buildParts(seg.text, seg.spans)
  // The statute label is spelled out on its first use in a segment; later bars
  // covered by the same exemption carry the code alone, to keep the line readable.
  const seen = new Set()
  return (
    <div className="utt">
      {parts.map((part, i) => {
        if (part.kind === 'plain') return <span key={i}>{part.text}</span>
        const compact = seen.has(part.span.exemption)
        seen.add(part.span.exemption)
        return (
          <RedactedSpan
            key={i}
            segmentId={segmentId}
            part={part}
            compact={compact}
            open={openKey === segmentId + ':' + part.index}
            removed={!!removed[part.index]}
            onPick={onPick}
          />
        )
      })}
    </div>
  )
}

function releaseTag (seg) {
  if (!seg.final) return null
  if (seg.redaction_state === 'failed') return { text: 'REVIEW REQUIRED', cls: 'tag' }
  if (seg.redaction_state === 'pending') return { text: 'ANALYSING', cls: 'tag scanning' }
  return null
}

// Renders one grid row: the internal cell and the release cell, in that order.
export function SegmentRow ({ n, seg, overrides, openKey, onPick }) {
  const removed = overrides[seg.id] || {}
  const num = String(n).padStart(3, '0')
  const tag = releaseTag(seg)
  return (
    <>
      <div className="cell l">
        <div className="gut">{num}</div>
        <div className="body">
          <div className="meta">
            <span className="spk">{seg.speaker}</span>
            <span className="tc">{timecode(seg.t_start)}</span>
          </div>
          <Utterance segmentId={seg.id} seg={seg} redact={false} removed={removed} openKey={openKey} onPick={onPick} />
        </div>
      </div>
      <div className="cell r">
        <div className="gut">{num}</div>
        <div className="body">
          <div className="meta">
            <span className="spk">{seg.speaker}</span>
            <span className="tc">{timecode(seg.t_start)}</span>
            {tag ? <span className={tag.cls}>{tag.text}</span> : null}
          </div>
          <Utterance segmentId={seg.id} seg={seg} redact removed={removed} openKey={openKey} onPick={onPick} />
        </div>
      </div>
    </>
  )
}

export function PaneHeaders ({ segmentCount, redactionCount, exemptionCount }) {
  return (
    <div className="panehead">
      <div className="ph">
        <h2>Internal Record</h2>
        <span className="tally">{segmentCount || ''}</span>
      </div>
      <div className="ph">
        <h2>FOI Release</h2>
        <span className="tally">
          {redactionCount ? redactionCount + (redactionCount === 1 ? ' redaction' : ' redactions') : ''}
        </span>
      </div>
    </div>
  )
}

export function OverridePopover ({ target, onAction, onClose }) {
  if (!target) return null
  const colour = colourFor(target.exemption)
  const statute = EXEMPTION_STATUTE[target.exemption] || ['Exempt information', 'qualified']
  const style = {
    '--exc': colour,
    left: Math.min(Math.max(12, target.x - 164), Math.max(12, window.innerWidth - 340)),
    top: Math.min(target.y + 14, Math.max(12, window.innerHeight - 250))
  }
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="pop" style={style} onClick={(e) => e.stopPropagation()}>
        <div className="ptitle">Withheld material &mdash; operator review</div>
        <div className="surface">{target.surface}</div>
        <div className="law">
          <b>FOIA 2000 {target.exemption}</b> &mdash; {statute[0]}. {statute[1] === 'absolute'
            ? 'Absolute exemption; no public interest test.'
            : 'Qualified exemption; public interest test applied.'}
          <br />
          Detected by {target.source === 'model' ? 'Gemma, on-device' : 'deterministic rule'}
          {Number.isFinite(target.confidence) ? ' · confidence ' + target.confidence.toFixed(2) : ''}
        </div>
        <div className="acts">
          <button className="btn" onClick={() => onAction('keep')}>Keep redacted</button>
          <button className="btn ghost" onClick={() => onAction('remove')}>Release text</button>
        </div>
      </div>
    </>
  )
}

