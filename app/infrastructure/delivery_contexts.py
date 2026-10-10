"""Additive accepted snapshot persistence; not a release-operation API."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.domain.delivery_context import DeliveryContext, DeliveryContractError
from app.infrastructure.database import Base


class DeliveryContextRecord(Base):
    __tablename__ = "delivery_contexts"
    __table_args__ = (UniqueConstraint("repository_id", "work_item_id",
                                      name="uq_delivery_context_work_item"),)

    repository_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    repository_full_name: Mapped[str] = mapped_column(String(250), nullable=False)
    delivery_issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    issue_body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_json: Mapped[str] = mapped_column(Text, nullable=False)
    contract_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAlchemyDeliveryContextRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, repository_id: int, work_item_id: str) -> DeliveryContext | None:
        row = self._session.get(DeliveryContextRecord, (repository_id, work_item_id))
        if row is None:
            return None
        contract = DeliveryContext.from_json(row.contract_json)
        if (contract.fingerprint() != row.contract_sha256
                or contract.repository_full_name != row.repository_full_name
                or contract.delivery_issue_number != row.delivery_issue_number
                or contract.accepted_issue_body_sha256 != row.issue_body_sha256):
            raise DeliveryContractError("Persisted delivery context is corrupted")
        return contract

    def add_accepted(self, context: DeliveryContext) -> None:
        """Only persist a caller-verified, GitHub-issue-anchored acceptance.

        This method never selects refs or authorizes a release workflow. A changed
        accepted contract requires a separately authorized replacement in B+.
        """
        existing = self.get(context.repository_id, context.work_item_id)
        if existing is not None:
            if existing != context:
                raise DeliveryContractError("Accepted delivery context is immutable")
            return
        self._session.add(DeliveryContextRecord(
            repository_id=context.repository_id,
            work_item_id=context.work_item_id,
            repository_full_name=context.repository_full_name,
            delivery_issue_number=context.delivery_issue_number,
            issue_body_sha256=context.accepted_issue_body_sha256,
            contract_json=context.canonical_json(),
            contract_sha256=context.fingerprint(),
            accepted_at=datetime.now(timezone.utc),
        ))
