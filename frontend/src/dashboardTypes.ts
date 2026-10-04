export type Project = {
  project_id: string
  repository_full_name: string
  roadmap_issue_number: number
}

export type WorkItem = {
  key: string
  type: string
  status: string
  parent: string
  lane: string
  title: string
  replaces?: string | null
  depends_on?: string[]
}

export type Diagnostic = {
  code: string
  message: string
  line_number?: number | null
}

export type RoadmapResponse = {
  project: Project
  source: {
    status: 'available' | 'unavailable'
    issue_number?: number
    updated_at?: string | null
    code?: string
  }
  pipeline: null | {
    valid: boolean
    version?: number | null
    work_items: WorkItem[]
    diagnostics: Diagnostic[]
    active_ready_item: WorkItem | null
  }
}

export type ImportedResponse = {
  response_id: string
  delivery_id: string
  session: string
  project_id: string
  work_item_id: string
  role: string
  imported_at: string
  text: string
}

export type SchedulerItem = {
  key: string
  canonical_status: string
  dependencies: string[]
  unsatisfied_dependencies: string[]
  scheduler_state: string
  reason: string
  expected_role: string | null
  next_action: string
}

export type SchedulerResponse = {
  project?: Project
  source: { status: 'available' | 'unavailable'; code?: string }
  scheduler: null | {
    valid: boolean
    pipeline_version: number | null
    executable_candidates: string[]
    diagnostics: Diagnostic[]
    work_items: SchedulerItem[]
  }
}

export type ParallelExecutionItem = {
  role: string
  agent_session: string
  scheduler: {
    state: string
    reason: string
    dependencies: string[]
    unsatisfied_dependencies: string[]
  }
  slot_state: string
  active: boolean
  waiting_for_capacity: boolean
  waiting_for_resource_lock: boolean
  inhibition_reason: string | null
  interaction: null | {
    dispatch_id: string | null
    dispatch_status: string | null
    delivery_id: string | null
    delivery_status: string | null
    send_state: string | null
    send_attempt_count: number | null
    send_error_code: string | null
    send_confirmed_at: string | null
    imported_response_available: boolean
  }
  watchdog: null | {
    branch_last_activity_at: string
    threshold_seconds: number
    send_confirmed_at: string | null
    stale_due: boolean
    relaunch_prepared: boolean
  }
  resource_locks: {
    required: { surface: string; mode: string }[]
    held: {
      lock_id: string
      surface: string
      mode: string
      state: string
      work_item_id: string
      agent_session: string
      lease_expires_at: string
      version: number
      released_at: string | null
      release_reason: string | null
    }[]
    records: {
      lock_id: string
      surface: string
      mode: string
      state: string
      work_item_id: string
      agent_session: string
      lease_expires_at: string
      version: number
      released_at: string | null
      release_reason: string | null
    }[]
    conflict: null | {
      surface: string
      requested_mode: string
      holder_work_item_id: string
      holder_agent_session: string
      holder_mode: string
      holder_state: string
      reason: string
    }
    recovery_state: string | null
  }
  work_item: WorkItem | null
  execution_state: string
  next_action: string
  branch: string | null
  pull_request: null | {
    number: number
    title: string
    url: string | null
    mergeable: boolean | null
    merged: boolean
  }
  head_sha: string | null
  ci: null | {
    state: string
    observed_runs: number
    failed_jobs: string[]
  }
  diagnostics: Diagnostic[]
}

export type ParallelExecutionsResponse = {
  project: Project
  source: { status: 'available' | 'unavailable'; code?: string }
  capacity: null | {
    limit: number
    used: number
    available: number
  }
  executable_candidates: string[]
  executions: ParallelExecutionItem[]
}

export type CockpitSource = {
  status: 'available' | 'unavailable'
  code: string | null
  updated_at: string | null
  revision: string | null
  diagnostics: Diagnostic[]
}

export type CockpitHorizonItem = {
  key: string
  title: string
  type: string
  status: string
  lane: string
  scheduler_state: string | null
  expected_role: string | null
  next_action: string | null
}

export type CockpitRoleSummary = {
  role: 'PO' | 'ARCH' | 'REVIEWER'
  label: string
  state: 'ACTION' | 'WATCH' | 'CLEAR'
  action_count: number
  watch_count: number
  primary_work_item_id: string | null
  headline: string
  detail: string
}

export type CockpitOverview = {
  project: Project
  observed_at: string
  sources: {
    roadmap: CockpitSource
    executions: CockpitSource
    attention: CockpitSource
  }
  attention: {
    state: 'ACTION' | 'WATCH' | 'CLEAR' | 'UNAVAILABLE'
    action_count: number
    watch_count: number
  }
  roles: CockpitRoleSummary[]
  horizons: {
    now: CockpitHorizonItem | null
    parallel: CockpitHorizonItem[]
    next: CockpitHorizonItem | null
  }
  dev_pool: {
    capacity_limit: number | null
    capacity_used: number | null
    capacity_available: number | null
    candidates: number
    active: number
    waiting_for_capacity: number
    waiting_for_resource_lock: number
    items: {
      work_item_id: string
      agent_session: string
      slot_state: string
      execution_state: string
      ci_state: string | null
    }[]
  }
}
