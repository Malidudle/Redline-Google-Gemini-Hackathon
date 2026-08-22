// WebSocket client for the REDLINE backend. Never throws at the call site:
// if the backend is absent the page keeps working and the client keeps retrying.

export const WS_URL = 'ws://localhost:8000/ws'

const MAX_BACKOFF_MS = 4000
const MAX_QUEUED = 32

export function connect({ url = WS_URL, onMessage, onStatus } = {}) {
  let sock = null
  let timer = null
  let attempt = 0
  let stopped = false
  let queue = []

  const status = (s) => { try { onStatus && onStatus(s) } catch (e) { console.error(e) } }

  function open() {
    if (stopped) return
    status('connecting')
    let next
    try {
      next = new WebSocket(url)
    } catch (e) {
      schedule()
      return
    }
    sock = next

    next.onopen = () => {
      attempt = 0
      status('open')
      const pending = queue
      queue = []
      pending.forEach(raw)
    }

    next.onmessage = (ev) => {
      let msg
      try { msg = JSON.parse(ev.data) } catch (e) { return }
      if (!msg || typeof msg.type !== 'string') return
      try { onMessage && onMessage(msg) } catch (e) { console.error(e) }
    }

    next.onerror = () => {}

    next.onclose = () => {
      if (sock === next) sock = null
      status('closed')
      schedule()
    }
  }

  function schedule() {
    if (stopped || timer) return
    const delay = Math.min(MAX_BACKOFF_MS, 400 * Math.pow(2, attempt))
    attempt += 1
    timer = setTimeout(() => { timer = null; open() }, delay)
  }

  function raw(text) {
    try { sock && sock.send(text) } catch (e) { console.error(e) }
  }

  open()

  return {
    send(type, payload = {}) {
      let text
      try { text = JSON.stringify({ type, payload }) } catch (e) { return }
      if (sock && sock.readyState === 1) raw(text)
      else if (queue.length < MAX_QUEUED) queue.push(text)
    },
    close() {
      stopped = true
      if (timer) { clearTimeout(timer); timer = null }
      try { sock && sock.close() } catch (e) {}
      sock = null
    }
  }
}
