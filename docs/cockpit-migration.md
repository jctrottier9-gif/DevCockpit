# Hybrid cockpit migration and historical cockpit status

DC-070F closes the hybrid-cockpit initiative by making the DevCockpit dashboard the normal operator surface while keeping raw diagnostics available without preserving a second workflow engine.

## Canonical runtime boundary

DevCockpit remains the only runtime authority for roadmap parsing, scheduling, parallel DEV capacity, ResourceLocks, PromptDispatch/PromptDelivery, ChatGptPromptSend, architecture-gate authorization, GitHub/CI evidence, watchdog recovery and roadmap reconciliation.

The historical cockpit under `tchi99/RessourcePlanner/dev-cockpit` is a UX reference only. DevCockpit does not import its backend, call it at runtime or depend on it for delivery state.

This slice does **not** delete or deprecate files in RessourcePlanner. Any later inter-repository retirement must be explicitly approved and delivered separately in that repository.

## Normal operator paths

The hybrid dashboard is the primary path for day-to-day supervision:

- **Attention Center** surfaces actions and watch conditions.
- **Product Owner** opens the roadmap explorer and GitHub issue context.
- **Architecte** shows gates, explicit human authorization state and ADR details.
- **Reviewer** shows PR, current head, workflows and CI jobs.
- **DEV Pool** shows parallel WorkItems, capacity, locks, CI, companion state and watchdog evidence.
- **WorkItem / Orchestration** opens as a first-class contextual drawer. It no longer requires opening the legacy technical stack first.
- **Companion state** is shown from backend projections; the browser never becomes product authority.

The remaining **Diagnostics techniques** disclosure is intentionally secondary. It preserves Flow Analytics, raw scheduler/execution projections and imported-response diagnostics for troubleshooting without duplicating the primary orchestration workflow.

## Keyboard and responsive baseline

Context drawers are modal navigation surfaces:

- opening a drawer moves focus to its close control;
- Tab and Shift+Tab remain trapped inside the drawer;
- Escape closes the drawer;
- closing restores focus to the element that opened it.

Interactive controls use a visible focus ring. The shell, role cards, trajectory, Attention Center and technical diagnostics collapse to a single-column mobile layout at narrow widths. Loading, unavailable and empty states remain explicit instead of leaving an unexplained blank area.

## Acceptance scenarios

### Multi-DEV READY to merge / reconciliation

Use the DEV Pool to inspect each WorkItem independently. Open a WorkItem to reach its targeted Orchestration drawer. Reviewer remains the evidence surface for PR/head/workflow details. GitHub and the canonical roadmap remain authoritative for merge and reconciliation.

### Architecture gate

Attention Center or the Architecte drawer can expose an eligible gate. Authorization is still an explicit human command. An authorized ARCH/ASTRA dispatch remains manual-only in the Firefox companion against a ChatGPT tab already placed in Work mode.

### CI red then correction

Attention Center and DEV Pool expose the actionable state while Reviewer provides the current-head workflow/job evidence. A CI failure is routed back to the same logical DEV session; the UI does not infer a new execution.

### Stale DEV watchdog

DEV Pool surfaces stale/relaunch evidence derived by the backend. React does not calculate a competing inactivity state.

### Roadmap and GitHub details

The Product Owner drawer remains the normal roadmap navigation surface. Issue mapping and horizon meaning are backend-derived; “Ensuite” is navigation perspective, never execution authorization.

## Removal policy

Technical or historical surfaces may be removed only after their diagnostics/actions are demonstrably available through the canonical DevCockpit workflow. Cross-repository deletion of the RessourcePlanner cockpit is outside DC-070F and requires a separate explicit decision.
