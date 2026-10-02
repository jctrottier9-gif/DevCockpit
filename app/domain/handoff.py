from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID

from app.domain.prompt_dispatch import PromptDispatchRole, build_agent_session


class OrchestrationConflict(ValueError):
    """An explicit conflict, including stale commands or invalid provenance."""


class HandoffStatus(StrEnum):
    OPEN = "OPEN"
    DECIDED = "DECIDED"
    RESUME_PREPARED = "RESUME_PREPARED"
    TRANSFERRED = "TRANSFERRED"
    RESOLVED_NO_RESUME = "RESOLVED_NO_RESUME"
    CANCELLED = "CANCELLED"


class HandoffPurpose(StrEnum):
    TECHNICAL_GUIDANCE = "TECHNICAL_GUIDANCE"
    PRODUCT_CLARIFICATION = "PRODUCT_CLARIFICATION"
    ROADMAP_REVIEW = "ROADMAP_REVIEW"


class DecisionType(StrEnum):
    ARCHITECTURE_GUIDANCE = "ARCHITECTURE_GUIDANCE"
    PRODUCT_CLARIFICATION = "PRODUCT_CLARIFICATION"
    SCOPE_DECISION = "SCOPE_DECISION"


class DecisionEffect(StrEnum):
    CONTINUE_IN_SCOPE = "CONTINUE_IN_SCOPE"
    HOLD_FOR_AUTHORIZATION = "HOLD_FOR_AUTHORIZATION"


_ALLOWED_PURPOSES = {
    (PromptDispatchRole.ARCH, HandoffPurpose.TECHNICAL_GUIDANCE),
    (PromptDispatchRole.PO, HandoffPurpose.PRODUCT_CLARIFICATION),
    (PromptDispatchRole.PO, HandoffPurpose.ROADMAP_REVIEW),
}

_ALLOWED_DECISIONS = {
    (PromptDispatchRole.ARCH, HandoffPurpose.TECHNICAL_GUIDANCE): {
        DecisionType.ARCHITECTURE_GUIDANCE,
    },
    (PromptDispatchRole.PO, HandoffPurpose.PRODUCT_CLARIFICATION): {
        DecisionType.PRODUCT_CLARIFICATION,
    },
    (PromptDispatchRole.PO, HandoffPurpose.ROADMAP_REVIEW): {
        DecisionType.SCOPE_DECISION,
    },
}


def required(value: str | None, name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must not be blank")


def validate_handoff_kind(role: PromptDispatchRole | str, purpose: HandoffPurpose | str) -> tuple[PromptDispatchRole, HandoffPurpose]:
    try:
        target_role = role if isinstance(role, PromptDispatchRole) else PromptDispatchRole(role)
        target_purpose = purpose if isinstance(purpose, HandoffPurpose) else HandoffPurpose(purpose)
    except ValueError as exc:
        raise ValueError("Unsupported Handoff role or purpose") from exc
    if (target_role, target_purpose) not in _ALLOWED_PURPOSES:
        raise ValueError("Unsupported Handoff role or purpose")
    return target_role, target_purpose


def validate_decision_kind(
    role: PromptDispatchRole | str,
    purpose: HandoffPurpose | str,
    decision_type: DecisionType | str,
) -> DecisionType:
    target_role, target_purpose = validate_handoff_kind(role, purpose)
    try:
        target_type = decision_type if isinstance(decision_type, DecisionType) else DecisionType(decision_type)
    except ValueError as exc:
        raise ValueError("Unsupported Decision type") from exc
    if target_type not in _ALLOWED_DECISIONS[(target_role, target_purpose)]:
        raise ValueError("Decision type is not allowed for this Handoff role/purpose")
    return target_type


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
    target_role: str = "ARCH"
    purpose: str = "TECHNICAL_GUIDANCE"
    status: HandoffStatus = HandoffStatus.OPEN
    version: int = 1
    resume_dispatch_id: UUID | None = None
    covered_github_evidence: str | None = None
    resume_held_reason: str | None = None
    predecessor_handoff_id: UUID | None = None
    context_decision_id: UUID | None = None
    cancelled_by: str | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    cancellation_command_id: UUID | None = None

    def __post_init__(self):
        role, purpose = validate_handoff_kind(self.target_role, self.purpose)
        build_agent_session(self.project_id, role, self.work_item_id)
        for name in ("question", "context", "context_snapshot", "created_by"):
            required(getattr(self, name), name)
        if self.version < 1 or self.created_at.tzinfo is None:
            raise ValueError("Invalid version or timestamp")
        HandoffStatus(self.status)
        if (self.status == HandoffStatus.RESUME_PREPARED) != (self.resume_dispatch_id is not None):
            raise ValueError("Resume link must agree with Handoff state")
        if self.predecessor_handoff_id == self.handoff_id:
            raise ValueError("Handoff cannot be its own predecessor")
        cancellation = (
            self.cancelled_by,
            self.cancelled_at,
            self.cancel_reason,
            self.cancellation_command_id,
        )
        if self.status == HandoffStatus.CANCELLED:
            if not all(cancellation) or not self.cancelled_by.strip() or not self.cancel_reason.strip():
                raise ValueError("Cancellation attribution, timestamp, reason and command required")
            if self.cancelled_at.tzinfo is None or self.cancelled_at < self.created_at:
                raise ValueError("Invalid cancellation timestamp")
        elif any(value is not None for value in cancellation):
            raise ValueError("Cancellation fields require CANCELLED state")

    @property
    def blocking(self) -> bool:
        return self.status in (HandoffStatus.OPEN, HandoffStatus.DECIDED)

    @property
    def role(self) -> PromptDispatchRole:
        return PromptDispatchRole(self.target_role)

    @property
    def handoff_purpose(self) -> HandoffPurpose:
        return HandoffPurpose(self.purpose)

    def transition(self, status: HandoffStatus, expected_version: int, **changes):
        allowed = {
            HandoffStatus.OPEN: {
                HandoffStatus.DECIDED,
                HandoffStatus.TRANSFERRED,
                HandoffStatus.CANCELLED,
            },
            HandoffStatus.DECIDED: {
                HandoffStatus.RESUME_PREPARED,
                HandoffStatus.RESOLVED_NO_RESUME,
                HandoffStatus.TRANSFERRED,
                HandoffStatus.CANCELLED,
            },
        }
        if expected_version != self.version:
            raise OrchestrationConflict("Handoff version changed; refresh before confirming")
        if status not in allowed.get(self.status, set()):
            raise OrchestrationConflict(f"Cannot transition {self.status} to {status}")
        if status == HandoffStatus.RESUME_PREPARED and not changes.get("resume_dispatch_id"):
            raise OrchestrationConflict("Resume dispatch required")
        if status == HandoffStatus.CANCELLED:
            for field in ("cancelled_by", "cancel_reason"):
                required(changes.get(field), field)
            if not changes.get("cancelled_at") or not changes.get("cancellation_command_id"):
                raise ValueError("Cancellation timestamp and command required")
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
    decision_type: str = "ARCHITECTURE_GUIDANCE"

    def __post_init__(self):
        required(self.summary, "summary")
        required(self.accepted_by, "accepted_by")
        DecisionEffect(self.effect)
        DecisionType(self.decision_type)
        if self.accepted_at.tzinfo is None:
            raise ValueError("Invalid decision timestamp")


@dataclass(frozen=True)
class WritebackRiskAuthorization:
    decision_id: UUID
    project_id: str
    accepted_by: str
    accepted_at: datetime
    acceptance_command_id: UUID

    def __post_init__(self):
        required(self.project_id, "project_id")
        required(self.accepted_by, "accepted_by")
        if self.accepted_at.tzinfo is None:
            raise ValueError("Invalid writeback risk authorization timestamp")


def utc_now():
    return datetime.now(timezone.utc)
