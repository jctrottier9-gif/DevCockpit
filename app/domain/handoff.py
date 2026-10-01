from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID

from app.domain.prompt_dispatch import build_agent_session


class OrchestrationConflict(ValueError):
    """An explicit conflict, including stale commands or invalid provenance."""


class HandoffStatus(StrEnum):
    OPEN = 'OPEN'
    DECIDED = 'DECIDED'
    RESUME_PREPARED = 'RESUME_PREPARED'
    CANCELLED = 'CANCELLED'


class DecisionEffect(StrEnum):
    CONTINUE_IN_SCOPE = 'CONTINUE_IN_SCOPE'
    HOLD_FOR_AUTHORIZATION = 'HOLD_FOR_AUTHORIZATION'


def required(value: str, name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f'{name} must not be blank')


@dataclass(frozen=True)
class Handoff:
    handoff_id: UUID
    project_id: str
    work_item_id: str
    source_dispatch_id: UUID
    source_response_id: UUID | None
    question: str
    context: str
    context_snapshot: str
    request_dispatch_id: UUID
    creation_command_id: UUID
    created_by: str
    created_at: datetime
    target_role: str = 'ARCH'
    purpose: str = 'TECHNICAL_GUIDANCE'
    status: HandoffStatus = HandoffStatus.OPEN
    version: int = 1
    resume_dispatch_id: UUID | None = None
    covered_github_evidence: str | None = None
    resume_held_reason: str | None = None
    cancelled_by: str | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    cancellation_command_id: UUID | None = None

    def __post_init__(self):
        build_agent_session(self.project_id, self.target_role, self.work_item_id)
        if self.target_role != 'ARCH' or self.purpose != 'TECHNICAL_GUIDANCE':
            raise ValueError('Unsupported Handoff role or purpose')
        for name in ('question', 'context', 'context_snapshot', 'created_by'):
            required(getattr(self, name), name)
        if self.version < 1 or self.created_at.tzinfo is None:
            raise ValueError('Invalid version or timestamp')
        HandoffStatus(self.status)
        if (self.status == HandoffStatus.RESUME_PREPARED) != (self.resume_dispatch_id is not None):
            raise ValueError('Resume link must agree with Handoff state')
        cancellation = (self.cancelled_by, self.cancelled_at, self.cancel_reason, self.cancellation_command_id)
        if self.status == HandoffStatus.CANCELLED:
            if not all(cancellation) or not self.cancelled_by.strip() or not self.cancel_reason.strip():
                raise ValueError('Cancellation attribution, timestamp, reason and command required')
            if self.cancelled_at.tzinfo is None or self.cancelled_at < self.created_at:
                raise ValueError('Invalid cancellation timestamp')
        elif any(value is not None for value in cancellation):
            raise ValueError('Cancellation fields require CANCELLED state')

    @property
    def blocking(self):
        return self.status in (HandoffStatus.OPEN, HandoffStatus.DECIDED)

    def transition(self, status: HandoffStatus, expected_version: int, **changes):
        allowed = {
            HandoffStatus.OPEN: {HandoffStatus.DECIDED, HandoffStatus.CANCELLED},
            HandoffStatus.DECIDED: {HandoffStatus.RESUME_PREPARED, HandoffStatus.CANCELLED},
        }
        if expected_version != self.version:
            raise OrchestrationConflict('Handoff version changed; refresh before confirming')
        if status not in allowed.get(self.status, set()):
            raise OrchestrationConflict(f'Cannot transition {self.status} to {status}')
        if status == HandoffStatus.RESUME_PREPARED and not changes.get('resume_dispatch_id'):
            raise OrchestrationConflict('Resume dispatch required')
        if status == HandoffStatus.CANCELLED:
            for field in ('cancelled_by', 'cancel_reason'):
                required(changes.get(field), field)
            if not changes.get('cancelled_at') or not changes.get('cancellation_command_id'):
                raise ValueError('Cancellation timestamp and command required')
        return replace(self, status=status, version=self.version + 1, **changes)


@dataclass(frozen=True)
class Decision:
    decision_id: UUID
    source_handoff_id: UUID
    source_response_id: UUID
    summary: str
    effect: DecisionEffect
    accepted_by: str
    accepted_at: datetime
    acceptance_command_id: UUID
    decision_type: str = 'ARCHITECTURE_GUIDANCE'

    def __post_init__(self):
        required(self.summary, 'summary')
        required(self.accepted_by, 'accepted_by')
        DecisionEffect(self.effect)
        if self.decision_type != 'ARCHITECTURE_GUIDANCE' or self.accepted_at.tzinfo is None:
            raise ValueError('Invalid decision type or timestamp')


def utc_now():
    return datetime.now(timezone.utc)
