from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.application.prompt_deliveries import (
    AcknowledgementResult,
    acknowledge_prompt_delivery,
    prepare_prompt_deliveries_for_send,
)
from app.config import Settings, get_settings
from app.infrastructure.database import build_engine, build_session_factory
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork
from app.infrastructure.websocket_transport import (
    AckMessage,
    CompanionConnectionManager,
    PingMessage,
    ProtocolMessageError,
    build_error_message,
    build_pong_message,
    build_prompt_message,
    parse_inbound_message,
)


_DELIVERY_POLL_SECONDS = 0.1


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    engine = build_engine(active_settings)
    session_factory = build_session_factory(engine)
    connection_manager = CompanionConnectionManager()

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        engine.dispose()

    application = FastAPI(
        title=active_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.state.engine = engine
    application.state.session_factory = session_factory
    application.state.uow_factory = uow_factory
    application.state.companion_connections = connection_manager

    @application.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.websocket("/api/companion/ws")
    async def companion_websocket(websocket: WebSocket) -> None:
        if not await connection_manager.connect(websocket):
            return

        sent_on_connection: set = set()
        receive_task: asyncio.Task[str] | None = asyncio.create_task(websocket.receive_text())

        try:
            while True:
                outbound = prepare_prompt_deliveries_for_send(
                    uow_factory=uow_factory,
                    exclude_delivery_ids=sent_on_connection,
                )
                for delivery in outbound:
                    await websocket.send_json(build_prompt_message(delivery))
                    sent_on_connection.add(delivery.delivery_id)

                done, _ = await asyncio.wait(
                    {receive_task},
                    timeout=_DELIVERY_POLL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    continue

                try:
                    raw_message = receive_task.result()
                except WebSocketDisconnect:
                    break
                receive_task = asyncio.create_task(websocket.receive_text())

                try:
                    message = parse_inbound_message(raw_message)
                except ProtocolMessageError as exc:
                    await websocket.send_json(build_error_message(exc.code))
                    continue

                if isinstance(message, AckMessage):
                    result = acknowledge_prompt_delivery(
                        message.delivery_id,
                        uow_factory=uow_factory,
                    )
                    if result is AcknowledgementResult.UNKNOWN:
                        await websocket.send_json(build_error_message("unknown_delivery_ack"))
                    continue

                if isinstance(message, PingMessage):
                    await websocket.send_json(build_pong_message())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                with suppress(asyncio.CancelledError):
                    await receive_task
            await connection_manager.disconnect(websocket)

    return application


app = create_app()
