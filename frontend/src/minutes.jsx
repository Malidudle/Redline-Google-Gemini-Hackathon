import React, { useEffect, useState } from 'react'
import './minutes.css'

// The model may return a bare string or an object. Fixture playback uses strings,
// the Gemma schema returns objects, and both have to render the same way.
function entries (list) {
  return (list || [])
    .map((item) => (typeof item === 'string' ? { text: item } : item))
    .filter((item) => item && typeof item === 'object' && String(item.text || '').trim())
}

function names (list) {
  return (list || []).map((n) => String(n || '').trim()).filter(Boolean)
}

function Section ({ label, count, children }) {
  return (
    <section className="mn-sec">
      <div className="mn-sechead">
        <span className="mn-lbl">{label}</span>
        <span className="mn-count">{count}</span>
      </div>
      {children}
    </section>
  )
}

function Nil ({ children }) {
  return <p className="mn-nil">{children}</p>
}

function Item ({ n, text, notes }) {
  return (
    <div className="mn-item">
      <span className="mn-n">{String(n).padStart(2, '0')}</span>
      <div className="mn-txt">
        <p className="mn-line">{text}</p>
        {notes.length ? (
          <p className="mn-notes">
            {notes.map((note, i) => (
              <span key={i} className="mn-note">
                <span className="mn-k">{note[0]}</span>
                <span className="mn-v">{note[1]}</span>
              </span>
            ))}
          </p>
        ) : null}
      </div>
    </div>
  )
}

function Elapsed () {
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setSeconds((s) => s + 1), 1000)
    return () => clearInterval(id)
  }, [])
  return <span className="mn-elapsed">{String(seconds).padStart(3, '0')}s</span>
}

function Pending ({ segmentCount, modelTag }) {
  return (
    <div className="mn-state">
      <div className="mn-sechead">
        <span className="mn-lbl">Generating</span>
        <Elapsed />
      </div>
      <p className="mn-body-txt">
        {segmentCount
          ? `Reading ${segmentCount} unredacted segments of the internal record.`
          : 'Reading the unredacted internal record.'}
      </p>
      <p className="mn-body-txt">
        {modelTag ? `The ${modelTag} model is` : 'The minutes model is'} large and runs on
        this machine. A first pass usually takes between 5 and 30 seconds. Nothing is
        sent off the device.
      </p>
    </div>
  )
}

export default function MinutesPanel ({
  open,
  pending,
  minutes,
  segmentCount,
  modelTag,
  onClose
}) {
  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const data = minutes || {}
  const attendees = names(data.attendees)
  const decisions = entries(data.decisions)
  const actions = entries(data.actions)
  const unresolved = entries(data.unresolved)
  const error = String(data.error || '')
  const summary = String(data.summary || '').trim()
  const topics = names(data.topics)
  const empty = !error && !summary && !attendees.length && !decisions.length &&
    !actions.length && !unresolved.length
  const usedModel = String(data.model || modelTag || '')

  return (
    <>
      <div className="mn-scrim" onClick={onClose} />
      <aside className="mn" role="dialog" aria-label="Internal minute">
        <div className="mn-bar">
          <h2>Internal minute</h2>
          <button className="mn-close" onClick={onClose}>Dismiss</button>
        </div>

        <div className="mn-prov">
          <span>Source · unredacted internal record</span>
          <span>{usedModel ? usedModel + ' · on device' : 'on device'}</span>
        </div>

        <div className="mn-scroll">
          {pending ? <Pending segmentCount={segmentCount} modelTag={modelTag} /> : null}

          {!pending && error ? (
            <div className="mn-state">
              <div className="mn-sechead"><span className="mn-lbl">Not generated</span></div>
              <p className="mn-body-txt">
                The local model did not return a minute. The transcript is unaffected.
              </p>
              <p className="mn-err">{error}</p>
            </div>
          ) : null}

          {!pending && !error && empty ? (
            <div className="mn-state">
              <div className="mn-sechead"><span className="mn-lbl">No items</span></div>
              <p className="mn-body-txt">
                The model found no attendees, decisions, actions, or unresolved items in
                this transcript.
              </p>
            </div>
          ) : null}

          {!pending && !error && !empty ? (
            <>
              {summary ? (
                <Section label="Summary">
                  <p className="mn-body-txt mn-summary">{summary}</p>
                  {topics.length
                    ? <p className="mn-topics">{topics.join(' · ')}</p>
                    : null}
                </Section>
              ) : null}

              <Section label="Attendees" count={attendees.length}>
                {attendees.length
                  ? (
                    <ul className="mn-att">
                      {attendees.map((a, i) => <li key={i}>{a}</li>)}
                    </ul>
                  )
                  : <Nil>No attendee was named.</Nil>}
              </Section>

              <Section label="Decisions" count={decisions.length}>
                {decisions.length
                  ? decisions.map((d, i) => (
                    <Item
                      key={i}
                      n={i + 1}
                      text={d.text}
                      notes={d.decided_by ? [['Decided by', d.decided_by]] : []}
                    />
                  ))
                  : <Nil>No decision was recorded.</Nil>}
              </Section>

              <Section label="Actions" count={actions.length}>
                {actions.length
                  ? actions.map((a, i) => (
                    <Item
                      key={i}
                      n={i + 1}
                      text={a.text}
                      notes={[
                        ['Owner', a.owner || 'unassigned'],
                        ...(a.due_date ? [['Due', a.due_date]] : [])
                      ]}
                    />
                  ))
                  : <Nil>No action was assigned.</Nil>}
              </Section>

              <Section label="Unresolved" count={unresolved.length}>
                {unresolved.length
                  ? unresolved.map((u, i) => (
                    <Item key={i} n={i + 1} text={u.text} notes={[]} />
                  ))
                  : <Nil>Nothing was left open.</Nil>}
              </Section>

              <p className="mn-foot">
                Drafted by a local model from the unredacted record. Check it against the
                transcript before it is entered in the minute book.
              </p>
            </>
          ) : null}
        </div>
      </aside>
    </>
  )
}
