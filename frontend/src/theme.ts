export const THEME_STORAGE_KEY = 'devcockpit.theme'
export const THEME_MEDIA_QUERY = '(prefers-color-scheme: dark)'

export type ThemePreference = 'system' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

export type ThemeSnapshot = {
  preference: ThemePreference
  resolved: ResolvedTheme
}

export type ThemeController = {
  start: () => void
  stop: () => void
  setPreference: (preference: ThemePreference) => void
  getSnapshot: () => ThemeSnapshot
}

type ThemeStorage = Pick<Storage, 'getItem' | 'setItem'>
type ThemeMediaQuery = Pick<MediaQueryList, 'matches' | 'addEventListener' | 'removeEventListener'>

export function normalizeThemePreference(value: string | null | undefined): ThemePreference {
  return value === 'light' || value === 'dark' || value === 'system'
    ? value
    : 'system'
}

export function resolveTheme(
  preference: ThemePreference,
  systemPrefersDark: boolean,
): ResolvedTheme {
  if (preference === 'system') return systemPrefersDark ? 'dark' : 'light'
  return preference
}

export function readThemePreference(storage: ThemeStorage): ThemePreference {
  try {
    return normalizeThemePreference(storage.getItem(THEME_STORAGE_KEY))
  } catch {
    return 'system'
  }
}

export function persistThemePreference(
  storage: ThemeStorage,
  preference: ThemePreference,
) {
  try {
    storage.setItem(THEME_STORAGE_KEY, preference)
  } catch {
    // Theme preference is local-only; storage failures must not block the cockpit.
  }
}

export function createThemeController({
  storage,
  mediaQuery,
  applyTheme,
}: {
  storage: ThemeStorage
  mediaQuery: ThemeMediaQuery
  applyTheme: (snapshot: ThemeSnapshot) => void
}): ThemeController {
  let started = false
  let preference = readThemePreference(storage)
  let resolved = resolveTheme(preference, mediaQuery.matches)

  function publish() {
    resolved = resolveTheme(preference, mediaQuery.matches)
    const snapshot = { preference, resolved }
    applyTheme(snapshot)
    return snapshot
  }

  function handleSystemThemeChange() {
    if (preference === 'system') publish()
  }

  return {
    start() {
      if (started) return
      started = true
      mediaQuery.addEventListener('change', handleSystemThemeChange)
      publish()
    },
    stop() {
      if (!started) return
      started = false
      mediaQuery.removeEventListener('change', handleSystemThemeChange)
    },
    setPreference(nextPreference) {
      preference = normalizeThemePreference(nextPreference)
      persistThemePreference(storage, preference)
      publish()
    },
    getSnapshot() {
      return { preference, resolved }
    },
  }
}
