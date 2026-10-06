import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createThemeController,
  normalizeThemePreference,
  readThemePreference,
  resolveTheme,
  THEME_STORAGE_KEY,
} from '../src/theme.ts'

function fakeStorage(initialValue = null) {
  const values = new Map()
  if (initialValue !== null) values.set(THEME_STORAGE_KEY, initialValue)

  return {
    getItem(key) {
      return values.get(key) ?? null
    },
    setItem(key, value) {
      values.set(key, value)
    },
  }
}

function fakeMediaQuery(initialMatches) {
  let matches = initialMatches
  const listeners = new Set()

  return {
    get matches() {
      return matches
    },
    addEventListener(type, listener) {
      if (type === 'change') listeners.add(listener)
    },
    removeEventListener(type, listener) {
      if (type === 'change') listeners.delete(listener)
    },
    setMatches(nextMatches) {
      matches = nextMatches
      for (const listener of listeners) listener({ matches })
    },
  }
}

test('normalizes preference values and resolves system light or dark', () => {
  assert.equal(normalizeThemePreference('light'), 'light')
  assert.equal(normalizeThemePreference('dark'), 'dark')
  assert.equal(normalizeThemePreference('system'), 'system')
  assert.equal(normalizeThemePreference('unexpected'), 'system')
  assert.equal(resolveTheme('system', false), 'light')
  assert.equal(resolveTheme('system', true), 'dark')
  assert.equal(resolveTheme('light', true), 'light')
  assert.equal(resolveTheme('dark', false), 'dark')
})

test('persists an explicit preference and restores it for the next controller', () => {
  const storage = fakeStorage()
  const mediaQuery = fakeMediaQuery(false)
  const applied = []
  const controller = createThemeController({
    storage,
    mediaQuery,
    applyTheme: snapshot => applied.push(snapshot),
  })

  controller.start()
  controller.setPreference('dark')
  controller.stop()

  assert.equal(storage.getItem(THEME_STORAGE_KEY), 'dark')
  assert.equal(readThemePreference(storage), 'dark')
  assert.deepEqual(applied.at(-1), { preference: 'dark', resolved: 'dark' })

  const restored = createThemeController({
    storage,
    mediaQuery,
    applyTheme: () => {},
  })
  restored.start()
  assert.deepEqual(restored.getSnapshot(), { preference: 'dark', resolved: 'dark' })
  restored.stop()
})

test('system preference follows OS changes while an explicit theme remains stable', () => {
  const storage = fakeStorage('system')
  const mediaQuery = fakeMediaQuery(false)
  const applied = []
  const controller = createThemeController({
    storage,
    mediaQuery,
    applyTheme: snapshot => applied.push(snapshot),
  })

  controller.start()
  assert.deepEqual(applied.at(-1), { preference: 'system', resolved: 'light' })

  mediaQuery.setMatches(true)
  assert.deepEqual(applied.at(-1), { preference: 'system', resolved: 'dark' })

  controller.setPreference('light')
  const applicationCount = applied.length
  mediaQuery.setMatches(false)
  mediaQuery.setMatches(true)

  assert.equal(applied.length, applicationCount)
  assert.deepEqual(controller.getSnapshot(), { preference: 'light', resolved: 'light' })
  controller.stop()
})
