import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import './theme.css'
import { connect, WS_URL } from './ws.js'
import { FIXTURE_SEGMENTS } from './fixture_data.js'
import { PaneHeaders, SegmentRow, OverridePopover, WifiOff } from './panes.jsx'

const FIXTURE = new URLSearchParams(window.location.search).get('fixture') === '1'
const CLASSIFICATION = 'OFFICIAL-SENSITIVE'
const DEFAULT_TITLE = 'Joint Procurement & Safeguarding Review'
const EMPTY_STATS = { bytes_egress: 0, segments: 0, redactions: 0, latency_ms_p50: 0 }

const FIXTURE_STEP_MS = 2400
const FIXTURE_FINAL_MS = 1180
const STICK_THRESHOLD_PX = 90

function formatBytes (n) {
  const v = Number(n)
  if (!Number.isFinite(v) || v <= 0) return '0 B'
  if (v < 1024) return Math.round(v) + ' B'
  if (v < 1048576) return (v / 1024).toFixed(1) + ' KB'
  return (v / 1048576).toFixed(1) + ' MB'
}

export default function App () {
  const [title, setTitle] = useState(DEFAULT_TITLE)
  const [source, setSource] = useState('replay')
  const [running, setRunning] = useState(false)
  const [segments, setSegments] = useState([])
  const [stats, setStats] = useState(EMPTY_STATS)
  const [conn, setConn] = useState(FIXTURE ? 'fixture' : 'connecting')
  const [overrides, setOverrides] = useState({})
  const [target, setTarget] = useState(null)
  const [exportPath, setExportPath] = useState(null)
  const [minutes, setMinutes] = useState(null)

  const clientRef = useRef(null)
  const timersRef = useRef([])
  const scrollRef = useRef(null)
  const stickRef = useRef(true)

  const upsert = useCallback((seg) => {
    setSegments((prev) => {
      const i = prev.findIndex((s) => s.id === seg.id)
      if (i === -1) return prev.concat([seg])
      const next = prev.slice()
      const old = prev[i]
      const spans = (seg.spans && seg.spans.length) ? seg.spans : (old.spans || [])
      next[i] = { ...old, ...seg, spans }
      return next
    })
  }, [])

  const applyRedaction = useCallback((payload) => {
    setSegments((prev) => {
      const i = prev.findIndex((s) => s.id === payload.id)
      if (i === -1) return prev
      const next = prev.slice()
      next[i] = {
        ...prev[i],
        spans: payload.spans || [],
        redaction_state: payload.redaction_state || 'done'
      }
      return next
    })
  }, [])

  const resetSession = useCallback(() => {
    setSegments([])
    setStats(EMPTY_STATS)
    setOverrides({})
    setTarget(null)
    setExportPath(null)
    setMinutes(null)
    stickRef.current = true
  }, [])

  // ---- live backend -------------------------------------------------------

  useEffect(() => {
    if (FIXTURE) return undefined
    const client = connect({
      url: WS_URL,
      onStatus: setConn,
      onMessage: (msg) => {
        const p = msg.payload || {}
        switch (msg.type) {
          case 'segment.partial':
          case 'segment.final':
            upsert(p)
            break
          case 'segment.redacted':
            applyRedaction(p)
            break
          case 'session.stats':
            setStats({ ...EMPTY_STATS, ...p })
            break
          case 'minutes.ready':
            setMinutes(p)
            break
          case 'export.ready':
            setExportPath(p.path || 'export complete')
            break
          default:
            break
        }
      }
    })
    clientRef.current = client
    return () => { client.close(); clientRef.current = null }
  }, [upsert, applyRedaction])

  const send = useCallback((type, payload) => {
    if (FIXTURE) { console.log('[fixture] suppressed send', type, payload); return }
    if (clientRef.current) clientRef.current.send(type, payload)
  }, [])

  // ---- fixture playback ---------------------------------------------------

  const clearTimers = useCallback(() => {
    timersRef.current.forEach(clearTimeout)
    timersRef.current = []
  }, [])

  const runFixture = useCallback(() => {
    clearTimers()
    resetSession()
    setRunning(true)
    const at = (ms, fn) => timersRef.current.push(setTimeout(fn, ms))

    FIXTURE_SEGMENTS.forEach((seed, i) => {
      const base = i * FIXTURE_STEP_MS
      const words = seed.text.split(' ')
      const cut = (frac, min) => words.slice(0, Math.max(min, Math.round(words.length * frac))).join(' ')
      const partial = (text) => upsert({
        ...seed, text, spans: [], final: false, redaction_state: 'pending'
      })

      at(base, () => partial(cut(0.45, 3)))
      at(base + 620, () => partial(cut(0.8, 5)))
      at(base + FIXTURE_FINAL_MS, () => upsert({ ...seed, spans: [], final: true, redaction_state: 'pending' }))

      const lag = 520 + (i % 3) * 230
      at(base + FIXTURE_FINAL_MS + lag, () => {
        applyRedaction({ id: seed.id, spans: seed.spans, redaction_state: 'done' })
        setStats((s) => ({ ...s, latency_ms_p50: lag }))
      })
    })

    at(FIXTURE_SEGMENTS.length * FIXTURE_STEP_MS + 1200, () => {
      setRunning(false)
      setMinutes({
        decisions: ['Preferred bidder noted, subject to caveat', 'Safeguarding case minuted to confidential log'],
        actions: ['Circulate full case summary separately'],
        attendees: ['CLLR OKAFOR', 'DR WHITFIELD']
      })
    })
  }, [clearTimers, resetSession, upsert, applyRedaction])

  useEffect(() => {
    if (!FIXTURE) return undefined
    const t = setTimeout(runFixture, 700)
    return () => { clearTimeout(t); clearTimers() }
  }, [runFixture, clearTimers])

  useEffect(() => clearTimers, [clearTimers])

  // ---- actions ------------------------------------------------------------

  const onStart = () => {
    if (FIXTURE) { runFixture(); return }
    resetSession()
    setRunning(true)
    send('session.start', { title, classification: CLASSIFICATION, source })
  }

  const onStop = () => {
    setRunning(false)
    clearTimers()
    send('session.stop', {})
  }

  const onExport = () => {
    send('export.request', { format: 'html' })
    if (FIXTURE) setTimeout(() => setExportPath('exports/foi_release_2026-08-22.html'), 700)
  }

  const onPick = (e, segmentId, spanIndex, span) => {
    const r = e.currentTarget.getBoundingClientRect()
    setTarget({
      segmentId,
      spanIndex,
      key: segmentId + ':' + spanIndex,
      surface: span.surface || '',
      exemption: span.exemption,
      source: span.source,
      confidence: span.confidence,
      x: r.left + r.width / 2,
      y: r.bottom
    })
  }

  const onOverride = (action) => {
    if (!target) return
    send('redaction.override', {
      segment_id: target.segmentId,
      span_index: target.spanIndex,
      action
    })
    setOverrides((prev) => {
      const forSeg = { ...(prev[target.segmentId] || {}) }
      if (action === 'remove') forSeg[target.spanIndex] = true
      else delete forSeg[target.spanIndex]
      return { ...prev, [target.segmentId]: forSeg }
    })
    setTarget(null)
  }

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') setTarget(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // ---- scrolling ----------------------------------------------------------

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD_PX
  }

  useEffect(() => {
    const el = scrollRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [segments])

  // ---- derived ------------------------------------------------------------

  const tally = useMemo(() => {
    let redactions = 0
    const kinds = new Set()
    for (const seg of segments) {
      const removed = overrides[seg.id] || {}
      ;(seg.spans || []).forEach((sp, i) => {
        if (removed[i]) return
        redactions += 1
        kinds.add(sp.exemption)
      })
    }
    return {
      finals: segments.filter((s) => s.final).length,
      redactions,
      exemptions: kinds.size
    }
  }, [segments, overrides])

  const egressHot = Number(stats.bytes_egress) > 0

  return (
    <div className="app">
      <header className="hdr">
        <div className="brand">
          <span className="wm">REDLINE</span>
          <span className="sub">On-device disclosure control</span>
        </div>
        <div className="sess">
          <span className="k">Session</span>
          <span className="v">{title || 'Untitled session'}</span>
        </div>
        <div className="classif">{CLASSIFICATION}</div>
        <div className="offline">
          <WifiOff />
          <span className="lbl">NO<br />NETWORK</span>
        </div>
        <div className={'egress' + (egressHot ? ' hot' : '')}>
          <div className="k">Data sent externally</div>
          <div className="v">{formatBytes(stats.bytes_egress)}</div>
          <div className="note"><span className="dot" />{egressHot ? 'EGRESS DETECTED' : 'VERIFIED LOCAL'}</div>
        </div>
      </header>

      <div className="ctrl">
        <span className="lbl">Title</span>
        <input
          className="title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          spellCheck="false"
        />
        <span className="lbl">Source</span>
        <div className="seg">
          <button className={source === 'mic' ? 'on' : ''} onClick={() => setSource('mic')}>Mic</button>
          <button className={source === 'replay' ? 'on' : ''} onClick={() => setSource('replay')}>Replay</button>
        </div>
        {running
          ? <button className="btn" onClick={onStop}>Stop</button>
          : <button className="btn" onClick={onStart}>Start recording</button>}
        <div className={'conn ' + conn}>
          <span className="dot" />
          {FIXTURE ? 'FIXTURE MODE — NO BACKEND' : conn.toUpperCase()}
        </div>
        <button className="btn primary" onClick={onExport}>Export FOI response</button>
      </div>

      <PaneHeaders
        segmentCount={tally.finals}
        redactionCount={tally.redactions}
        exemptionCount={tally.exemptions}
      />

      <div className="scroll split-bg" ref={scrollRef} onScroll={onScroll}>
        {segments.length === 0 ? (
          <div className="empty">
            <div className="ebox">
              <div className="ek">{FIXTURE ? 'Demonstration starting' : 'No session recorded'}</div>
              <p className="ep">
                The internal minute and the FOI release are built side by side on this
                machine. Nothing leaves it.
              </p>
              {!FIXTURE && conn !== 'open'
                ? <p className="ep quiet">Recorder not reachable at {WS_URL}.</p>
                : null}
              {!FIXTURE
                ? <a className="btn" href="?fixture=1">Run the offline demonstration</a>
                : null}
            </div>
          </div>
        ) : (
          <div className="grid">
            {segments.map((seg, i) => (
              <SegmentRow
                key={seg.id}
                n={i + 1}
                seg={seg}
                overrides={overrides}
                openKey={target ? target.key : null}
                onPick={onPick}
              />
            ))}
            <div className="fill l" />
            <div className="fill r" />
          </div>
        )}
      </div>

      <footer className="status">
        <span>{FIXTURE ? 'SOURCE fixture (demo/seed_transcript.json)' : 'SOCKET ' + WS_URL}</span>
        <span>SEGMENTS <b>{tally.finals}</b></span>
        <span>REDACTIONS <b>{tally.redactions}</b></span>
        <span>REDACTION p50 <b>{Math.round(Number(stats.latency_ms_p50) || 0)} ms</b></span>
        {minutes
          ? <span>MINUTE <b>{(minutes.decisions || []).length}</b> decisions / <b>{(minutes.actions || []).length}</b> actions</span>
          : null}
        <span className="right">
          {exportPath ? 'EXPORTED ' + exportPath : 'INFERENCE gemma · localhost · no egress'}
        </span>
      </footer>

      <OverridePopover target={target} onAction={onOverride} onClose={() => setTarget(null)} />
    </div>
  )
}
