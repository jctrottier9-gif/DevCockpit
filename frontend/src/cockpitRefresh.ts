export const DEFAULT_COCKPIT_REFRESH_INTERVAL_MS = 30_000
export const DEFAULT_RESUME_DEDUPE_MS = 1_000

export type CockpitRefreshReason = 'interval' | 'resume' | 'focus' | 'manual'
export type CockpitRefreshFailure = 'stale' | 'fatal'

type TimerHandle = ReturnType<typeof setTimeout>

type RefreshLoopOptions = {
  intervalMs: number
  refresh: (reason: CockpitRefreshReason) => Promise<void>
  initialVisible?: boolean
  resumeDedupeMs?: number
  now?: () => number
  setTimer?: (callback: () => void, delayMs: number) => TimerHandle
  clearTimer?: (handle: TimerHandle) => void
}

export type CockpitRefreshLoop = {
  start: () => void
  stop: () => void
  refreshNow: () => Promise<boolean>
  setVisible: (visible: boolean) => void
  notifyFocus: () => void
  isRefreshing: () => boolean
}

type CockpitSupervisionCycleOptions<T> = {
  evaluate: () => Promise<void>
  readSnapshot: () => Promise<T>
}

export async function runCockpitSupervisionCycle<T>(
  options: CockpitSupervisionCycleOptions<T>,
): Promise<T> {
  await options.evaluate()
  return options.readSnapshot()
}

export function resolveCockpitRefreshInterval(
  rawValue: string | null | undefined,
  fallback = DEFAULT_COCKPIT_REFRESH_INTERVAL_MS,
) {
  if (!rawValue) return fallback
  const parsed = Number(rawValue)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback
}

export function classifyCockpitRefreshFailure(
  requestProjectId: string,
  loadedProjectId: string | null,
): CockpitRefreshFailure {
  return loadedProjectId === requestProjectId ? 'stale' : 'fatal'
}

export function criticalCockpitSourceFailure(snapshot: {
  sources?: {
    roadmap?: {
      status?: string
      code?: string | null
    }
  }
}): string | null {
  const roadmap = snapshot.sources?.roadmap
  if (roadmap?.status !== 'unavailable') return null
  return 'Roadmap source unavailable: ' + (roadmap.code || 'GITHUB_UNAVAILABLE')
}

export function resolveRefreshedSelection(
  currentKey: string | null,
  availableKeys: readonly string[],
  preferredKey?: string | null,
): string | null {
  if (currentKey && availableKeys.includes(currentKey)) return currentKey
  if (preferredKey && availableKeys.includes(preferredKey)) return preferredKey
  return availableKeys[0] ?? null
}

export function createCockpitRefreshLoop(options: RefreshLoopOptions): CockpitRefreshLoop {
  const intervalMs = options.intervalMs > 0
    ? options.intervalMs
    : DEFAULT_COCKPIT_REFRESH_INTERVAL_MS
  const resumeDedupeMs = options.resumeDedupeMs ?? DEFAULT_RESUME_DEDUPE_MS
  const now = options.now ?? Date.now
  const setTimer = options.setTimer ?? ((callback, delayMs) => setTimeout(callback, delayMs))
  const clearTimer = options.clearTimer ?? (handle => clearTimeout(handle))

  let started = false
  let visible = options.initialVisible ?? true
  let inFlight = false
  let timer: TimerHandle | null = null
  let lastStartedAt = Number.NEGATIVE_INFINITY

  function clearScheduled() {
    if (timer === null) return
    clearTimer(timer)
    timer = null
  }

  function schedule() {
    clearScheduled()
    if (!started || !visible || inFlight) return
    timer = setTimer(() => {
      timer = null
      void run('interval')
    }, intervalMs)
  }

  async function run(reason: CockpitRefreshReason, bypassResumeDedupe = false) {
    if (!started) return false
    if (!visible && reason !== 'manual') return false
    if (inFlight) return false

    const startedAt = now()
    if (
      !bypassResumeDedupe
      && (reason === 'resume' || reason === 'focus')
      && startedAt - lastStartedAt < resumeDedupeMs
    ) {
      schedule()
      return false
    }

    clearScheduled()
    inFlight = true
    lastStartedAt = startedAt
    try {
      await options.refresh(reason)
    } finally {
      inFlight = false
      schedule()
    }
    return true
  }

  return {
    start() {
      if (started) return
      started = true
      schedule()
    },
    stop() {
      started = false
      clearScheduled()
    },
    refreshNow() {
      return run('manual', true)
    },
    setVisible(nextVisible: boolean) {
      if (visible === nextVisible) return
      visible = nextVisible
      if (!started) return
      if (!visible) {
        clearScheduled()
        return
      }
      void run('resume')
    },
    notifyFocus() {
      if (!started || !visible) return
      void run('focus')
    },
    isRefreshing() {
      return inFlight
    },
  }
}
