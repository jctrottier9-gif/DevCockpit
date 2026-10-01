from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.chatgpt_response import ImportedChatGptResponse
from app.infrastructure.database import Base


class ImportedChatGptResponseRecord(Base):
    __tablename__ = "imported_chatgpt_responses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["delivery_id"],
            ["prompt_deliveries.delivery_id"],
            name="fk_imported_chatgpt_responses_delivery_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(trim(text)) > 0",
            name="ck_imported_chatgpt_responses_text",
        ),
    )

    response_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyImportedChatGptResponseRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, response: ImportedChatGptResponse) -> None:
        self._session.add(
            ImportedChatGptResponseRecord(
                response_id=str(response.response_id),
                delivery_id=str(response.delivery_id),
                text=response.text,
                imported_at=response.imported_at,
            )
        )

    def get(self, response_id: object) -> ImportedChatGptResponse | None:
        record = self._session.get(ImportedChatGptResponseRecord, str(response_id))
        return _domain_from_record(record) if record is not None else None

    def list_all(self) -> list[ImportedChatGptResponse]:
        records = self._session.scalars(
            select(ImportedChatGptResponseRecord).order_by(
                ImportedChatGptResponseRecord.imported_at,
                ImportedChatGptResponseRecord.response_id,
            )
        ).all()
        return [_domain_from_record(record) for record in records]


def _domain_from_record(record: ImportedChatGptResponseRecord) -> ImportedChatGptResponse:
    imported_at = record.imported_at
    if imported_at.tzinfo is None:
        imported_at = imported_at.replace(tzinfo=timezone.utc)
    return ImportedChatGptResponse.rehydrate(
        response_id=UUID(record.response_id),
        delivery_id=UUID(record.delivery_id),
        text=record.text,
        imported_at=imported_at,
    )
