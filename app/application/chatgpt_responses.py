from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from enum import StrEnum
import re
import unicodedata
from typing import Callable, Protocol, Self
from uuid import UUID

from app.domain.chatgpt_response import ImportedChatGptResponse
from app.domain.prompt_delivery import PromptDelivery
from app.domain.prompt_dispatch import PromptDispatch


class ChatGptResponseImportError(ValueError):
    """Base error for response-import failures."""


class UnknownPromptDeliveryError(ChatGptResponseImportError):
    pass


class ResponseSessionMismatchError(ChatGptResponseImportError):
    pass


class ResponseIdConflictError(ChatGptResponseImportError):
    pass


class ResponseCorrelationError(ChatGptResponseImportError):
    pass


class ResponseEchoesPromptError(ChatGptResponseImportError):
    pass


class PromptDeliveryReadRepository(Protocol):
    def get(self, delivery_id: object) -> PromptDelivery | None: ...


class PromptDispatchReadRepository(Protocol):
    def get(self, dispatch_id: object) -> PromptDispatch | None: ...


class ImportedChatGptResponseRepository(Protocol):
    def add(self, response: ImportedChatGptResponse) -> None: ...
    def get(self, response_id: object) -> ImportedChatGptResponse | None: ...
    def list_all(self) -> list[ImportedChatGptResponse]: ...


class ChatGptResponseUnitOfWork(Protocol):
    prompt_dispatches: PromptDispatchReadRepository
    prompt_deliveries: PromptDeliveryReadRepository
    chatgpt_responses: ImportedChatGptResponseRepository

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], ChatGptResponseUnitOfWork]


@dataclass(frozen=True, slots=True)
class ImportChatGptResponseCommand:
    response_id: UUID
    delivery_id: UUID
    session: str
    text: str


class ResponseImportResult(StrEnum):
    IMPORTED = "IMPORTED"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, slots=True)
class ImportedChatGptResponseView:
    response_id: UUID
    delivery_id: UUID
    session: str
    project_id: str
    work_item_id: str
    role: str
    imported_at: datetime
    text: str


def _comparison_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"\\[([^\\]]+)\\]\\(([^)]+)\\)", r"\\1 \\2", normalized)
    normalized = normalized.replace(chr(96), "").replace("*", "").replace("_", "")
    normalized = re.sub(r"(?m)^\\s{0,3}#{1,6}\\s*", "", normalized)
    normalized = re.sub(r"(?m)^\\s*[-+*]\\s+", "", normalized)
    normalized = re.sub(r"\\s+", " ", normalized)
    return normalized.strip()


def _response_echoes_prompt(response_text: str, prompt_text: str) -> bool:
    response = _comparison_text(response_text)
    prompt = _comparison_text(prompt_text)
    if not response or not prompt:
        return False
    if response == prompt:
        return True
    if min(len(response), len(prompt)) < 80:
        return False
    length_ratio = len(response) / len(prompt)
    if not 0.9 <= length_ratio <= 1.1:
        return False
    return SequenceMatcher(None, response, prompt).ratio() >= 0.985

def _resolve_dispatch(
    delivery_id: UUID,
    *,
    uow: ChatGptResponseUnitOfWork,
) -> tuple[PromptDelivery, PromptDispatch]:
    delivery = uow.prompt_deliveries.get(delivery_id)
    if delivery is None:
        raise UnknownPromptDeliveryError("unknown_delivery")
    dispatch = uow.prompt_dispatches.get(delivery.dispatch_id)
    if dispatch is None:
        raise ResponseCorrelationError("missing_source_dispatch")
    return delivery, dispatch


def import_chatgpt_response(
    command: ImportChatGptResponseCommand,
    *,
    uow_factory: UnitOfWorkFactory,
) -> ResponseImportResult:
    if not command.session or command.session != command.session.strip():
        raise ResponseSessionMismatchError("session_mismatch")
    if not command.text or not command.text.strip():
        raise ChatGptResponseImportError("invalid_response_text")

    with uow_factory() as uow:
        _, dispatch = _resolve_dispatch(command.delivery_id, uow=uow)
        if command.session != dispatch.agent_session:
            raise ResponseSessionMismatchError("session_mismatch")
        if _response_echoes_prompt(command.text, dispatch.prompt_text):
            raise ResponseEchoesPromptError("response_echoes_prompt")

        existing = uow.chatgpt_responses.get(command.response_id)
        if existing is not None:
            if existing.represents_same_import(
                delivery_id=command.delivery_id,
                text=command.text,
            ):
                return ResponseImportResult.DUPLICATE
            raise ResponseIdConflictError("response_id_conflict")

        uow.chatgpt_responses.add(
            ImportedChatGptResponse.create(
                response_id=command.response_id,
                delivery_id=command.delivery_id,
                text=command.text,
            )
        )
        uow.commit()
        return ResponseImportResult.IMPORTED


def list_imported_chatgpt_responses(
    project_id: str,
    *,
    uow_factory: UnitOfWorkFactory,
) -> tuple[ImportedChatGptResponseView, ...]:
    views: list[ImportedChatGptResponseView] = []
    with uow_factory() as uow:
        for response in uow.chatgpt_responses.list_all():
            _, dispatch = _resolve_dispatch(response.delivery_id, uow=uow)
            if dispatch.project_id != project_id:
                continue
            views.append(
                ImportedChatGptResponseView(
                    response_id=response.response_id,
                    delivery_id=response.delivery_id,
                    session=dispatch.agent_session,
                    project_id=dispatch.project_id,
                    work_item_id=dispatch.work_item_id,
                    role=dispatch.role.value,
                    imported_at=response.imported_at,
                    text=response.text,
                )
            )
    return tuple(sorted(views, key=lambda item: (item.imported_at, str(item.response_id))))
