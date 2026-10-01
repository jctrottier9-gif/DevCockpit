from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID


class ImportedChatGptResponseError(ValueError):
    """Raised when an imported ChatGPT response violates domain invariants."""


class ImportedChatGptResponse:
    """One response explicitly selected by the user and imported into DevCockpit."""

    def __init__(
        self,
        *,
        response_id: UUID,
        delivery_id: UUID,
        text: str,
        imported_at: datetime,
    ) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ImportedChatGptResponseError("text must not be blank")
        if imported_at.tzinfo is None:
            raise ImportedChatGptResponseError("imported_at must be timezone-aware")

        self._response_id = response_id
        self._delivery_id = delivery_id
        self._text = text
        self._imported_at = imported_at.astimezone(timezone.utc)

    @classmethod
    def create(
        cls,
        *,
        response_id: UUID,
        delivery_id: UUID,
        text: str,
        now: datetime | None = None,
    ) -> "ImportedChatGptResponse":
        timestamp = now or datetime.now(timezone.utc)
        return cls(
            response_id=response_id,
            delivery_id=delivery_id,
            text=text,
            imported_at=timestamp,
        )

    @classmethod
    def rehydrate(
        cls,
        *,
        response_id: UUID,
        delivery_id: UUID,
        text: str,
        imported_at: datetime,
    ) -> "ImportedChatGptResponse":
        return cls(
            response_id=response_id,
            delivery_id=delivery_id,
            text=text,
            imported_at=imported_at,
        )

    @property
    def response_id(self) -> UUID:
        return self._response_id

    @property
    def delivery_id(self) -> UUID:
        return self._delivery_id

    @property
    def text(self) -> str:
        return self._text

    @property
    def imported_at(self) -> datetime:
        return self._imported_at

    def represents_same_import(self, *, delivery_id: UUID, text: str) -> bool:
        return self.delivery_id == delivery_id and self.text == text
