import { createContext, useContext, type ReactNode } from 'react'

const CockpitRefreshContext = createContext(0)

export function CockpitRefreshProvider({
  version,
  children,
}: {
  version: number
  children: ReactNode
}) {
  return <CockpitRefreshContext.Provider value={version}>
    {children}
  </CockpitRefreshContext.Provider>
}

export function useCockpitRefreshVersion() {
  return useContext(CockpitRefreshContext)
}
