export const ACTIVE_PROJECT_STORAGE_KEY = 'devcockpit.activeProjectId'

type ProjectIdentity = {
  project_id: string
}

export function resolveActiveProjectId(
  projects: readonly ProjectIdentity[],
  preferredProjectId: string | null,
): string | null {
  const configuredProjectIds = projects
    .map(project => project.project_id)
    .filter(projectId => projectId.length > 0)

  if (preferredProjectId && configuredProjectIds.includes(preferredProjectId)) {
    return preferredProjectId
  }

  return [...configuredProjectIds].sort()[0] ?? null
}

export function isCurrentProjectLoad(
  requestProjectId: string,
  activeProjectId: string,
  requestGeneration: number,
  currentGeneration: number,
): boolean {
  return requestProjectId === activeProjectId && requestGeneration === currentGeneration
}
