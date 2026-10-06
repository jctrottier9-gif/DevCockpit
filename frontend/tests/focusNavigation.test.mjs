import assert from 'node:assert/strict'
import test from 'node:test'
import { nextFocusIndex } from '../src/focusNavigation.ts'

test('keeps keyboard focus inside a drawer when tabbing forward', () => {
  assert.equal(nextFocusIndex(0, 3, false), 1)
  assert.equal(nextFocusIndex(2, 3, false), 0)
})

test('keeps keyboard focus inside a drawer when tabbing backward', () => {
  assert.equal(nextFocusIndex(2, 3, true), 1)
  assert.equal(nextFocusIndex(0, 3, true), 2)
})

test('chooses a deterministic target when focus starts outside the drawer', () => {
  assert.equal(nextFocusIndex(-1, 3, false), 0)
  assert.equal(nextFocusIndex(-1, 3, true), 2)
  assert.equal(nextFocusIndex(-1, 0, false), null)
})
