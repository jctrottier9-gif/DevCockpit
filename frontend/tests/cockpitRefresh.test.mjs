import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createCockpitRefreshLoop,
  DEFAULT_COCKPIT_REFRESH_INTERVAL_MS,
  resolveCockpitRefreshInterval,
} from '../src/cockpitRefresh.ts'

function fakeTimer() {
  let nextId = 1
  const callbacks = new Map()

  return {
    setTimer(callback) {
      const id = nextId++
      callbacks.set(id, callback)
      return id
    },
    clearTimer(id) {
      callbacks.delete(id)
    },
    fireNext() {
      const entry = callbacks.entries().next().value
      assert.ok(entry, 'expected a scheduled refresh')
      const [id, callback] = entry
      callbacks.delete(id)
      callback()
    },
    count() {
      return callbacks.size
    },
  }
}

async function flush() {
  await Promise.resolve()
  await Promise.resolve()
}

test('uses 30 seconds by default and accepts a positive frontend override', () => {
  assert.equal(resolveCockpitRefreshInterval(undefined), DEFAULT_COCKPIT_REFRESH_INTERVAL_MS)
  assert.equal(resolveCockpitRefreshInterval('15000'), 15000)
  assert.equal(resolveCockpitRefreshInterval('0'), DEFAULT_COCKPIT_REFRESH_INTERVAL_MS)
  assert.equal(resolveCockpitRefreshInterval('invalid'), DEFAULT_COCKPIT_REFRESH_INTERVAL_MS)
})

test('runs one coordinated periodic refresh and reschedules after completion', async () => {
  const timer = fakeTimer()
  const reasons = []
  const loop = createCockpitRefreshLoop({
    intervalMs: 30_000,
    refresh: async reason => { reasons.push(reason) },
    setTimer: callback => timer.setTimer(callback),
    clearTimer: id => timer.clearTimer(id),
  })

  loop.start()
  assert.equal(timer.count(), 1)
  timer.fireNext()
  await flush()

  assert.deepEqual(reasons, ['interval'])
  assert.equal(timer.count(), 1)
  loop.stop()
})

test('pauses while hidden and refreshes immediately when visibility returns', async () => {
  const timer = fakeTimer()
  const reasons = []
  let now = 10_000
  const loop = createCockpitRefreshLoop({
    intervalMs: 30_000,
    refresh: async reason => { reasons.push(reason) },
    setTimer: callback => timer.setTimer(callback),
    clearTimer: id => timer.clearTimer(id),
    now: () => now,
  })

  loop.start()
  loop.setVisible(false)
  assert.equal(timer.count(), 0)

  now += 5_000
  loop.setVisible(true)
  await flush()

  assert.deepEqual(reasons, ['resume'])
  assert.equal(timer.count(), 1)
  loop.stop()
})

test('deduplicates focus immediately after a visibility resume', async () => {
  const timer = fakeTimer()
  const reasons = []
  let now = 20_000
  const loop = createCockpitRefreshLoop({
    intervalMs: 30_000,
    refresh: async reason => { reasons.push(reason) },
    setTimer: callback => timer.setTimer(callback),
    clearTimer: id => timer.clearTimer(id),
    now: () => now,
  })

  loop.start()
  loop.setVisible(false)
  now += 2_000
  loop.setVisible(true)
  await flush()
  loop.notifyFocus()
  await flush()

  assert.deepEqual(reasons, ['resume'])
  loop.stop()
})

test('never starts a second refresh while the current refresh is in flight', async () => {
  const timer = fakeTimer()
  let release
  const reasons = []
  const blocked = new Promise(resolve => { release = resolve })
  const loop = createCockpitRefreshLoop({
    intervalMs: 30_000,
    refresh: async reason => {
      reasons.push(reason)
      await blocked
    },
    setTimer: callback => timer.setTimer(callback),
    clearTimer: id => timer.clearTimer(id),
  })

  loop.start()
  const first = loop.refreshNow()
  assert.equal(loop.isRefreshing(), true)
  const second = await loop.refreshNow()

  assert.equal(second, false)
  assert.deepEqual(reasons, ['manual'])

  release()
  assert.equal(await first, true)
  loop.stop()
})
