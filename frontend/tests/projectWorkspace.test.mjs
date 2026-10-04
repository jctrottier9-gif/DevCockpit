import assert from 'node:assert/strict'
import test from 'node:test'
import {
  isCurrentProjectLoad,
  resolveActiveProjectId,
} from '../src/projectWorkspace.ts'

const projects = [
  { project_id: 'TaskPlanner' },
  { project_id: 'DevCockpit' },
  { project_id: 'RessourcePlanner' },
]

test('keeps a valid locally preferred project', () => {
  assert.equal(resolveActiveProjectId(projects, 'RessourcePlanner'), 'RessourcePlanner')
})

test('falls back deterministically when the preference is missing or invalid', () => {
  assert.equal(resolveActiveProjectId(projects, null), 'DevCockpit')
  assert.equal(resolveActiveProjectId(projects, 'RemovedProject'), 'DevCockpit')
})

test('preserves the single-project behavior', () => {
  assert.equal(resolveActiveProjectId([{ project_id: 'OnlyProject' }], null), 'OnlyProject')
})

test('returns null when no configured project exists', () => {
  assert.equal(resolveActiveProjectId([], 'RemovedProject'), null)
})

test('rejects a late response from the previous project after A to B', () => {
  assert.equal(isCurrentProjectLoad('A', 'B', 1, 2), false)
  assert.equal(isCurrentProjectLoad('B', 'B', 2, 2), true)
})

test('rejects an older A response after A to B to A', () => {
  assert.equal(isCurrentProjectLoad('A', 'A', 1, 3), false)
  assert.equal(isCurrentProjectLoad('A', 'A', 3, 3), true)
})
