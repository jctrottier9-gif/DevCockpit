from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Self

from app.domain.prompt_dispatch import PromptDispatch, PromptDispatchRole


class IdempotencyConflictError(ValueError):
    """Raised when one idempotency key is reused for different logical work."""


class PromptDispatchRepository(Protocol):
    def add(self, dispatch: PromptDispatch) -> None: ...

    def get(self, dispatch_id: object) -> PromptDispatch | None: ...

    def get_by_idempotency_key(self, idempotency_key: str) -> PromptDispatch | None: ...


class HandoffPolicyRepository(Protocol):
    def active(self, project_id: str, work_item_id: str) -> object | None: ...
    def covers(self, project_id: str, work_item_id: str, evidence_key: str) -> bool: ...


class PromptDispatchUnitOfWork(Protocol):
    prompt_dispatches: PromptDispatchRepository
    handoffs: HandoffPolicyRepository

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], PromptDispatchUnitOfWork]


@dataclass(frozen=True, slots=True)
class CreatePromptDispatchCommand:
    project_id: str
    work_item_id: str
    role: PromptDispatchRole | str
    prompt_text: str
    idempotency_key: str


def create_prompt_dispatch(
    command: CreatePromptDispatchCommand,
    *,
    uow_factory: UnitOfWorkFactory,
) -> PromptDispatch:
    with uow_factory() as uow:
        dispatch = create_prompt_dispatch_in_uow(command, uow=uow)
        uow.commit()
        return dispatch


def create_prompt_dispatch_in_uow(
    command: CreatePromptDispatchCommand, *, uow: PromptDispatchUnitOfWork,
) -> PromptDispatch:
    role = command.role if isinstance(command.role, PromptDispatchRole) else PromptDispatchRole(command.role)

    existing = uow.prompt_dispatches.get_by_idempotency_key(command.idempotency_key)
    if existing is not None:
        if not existing.represents_same_logical_prompt(
            project_id=command.project_id,
            work_item_id=command.work_item_id,
            role=role,
            prompt_text=command.prompt_text,
        ):
            raise IdempotencyConflictError(
                "idempotency_key is already associated with a different logical prompt"
            )
        return existing

    dispatch = PromptDispatch.prepare(
        project_id=command.project_id,
        work_item_id=command.work_item_id,
        role=role,
        prompt_text=command.prompt_text,
        idempotency_key=command.idempotency_key,
    )
    uow.prompt_dispatches.add(dispatch)
    return dispatch
