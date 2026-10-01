from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.application.prompt_dispatches import CreatePromptDispatchCommand, create_prompt_dispatch
from app.config import Settings
from app.infrastructure.database import upgrade_database
from app.infrastructure.prompt_dispatches import SqlAlchemyUnitOfWork
from app.infrastructure.websocket_transport import SINGLE_COMPANION_CLOSE_CODE
from app.main import create_app


def _application(tmp_path: Path):
    database_path = tmp_path / "websocket.db"
    settings = Settings(database_url=f"sqlite+pysqlite:///{database_path}")
    upgrade_database(settings)
    return create_app(settings)


def _create_dispatch(application, *, work_item: str = "DC-011", role: str = "DEV"):
    return create_prompt_dispatch(
        CreatePromptDispatchCommand(
            project_id="DevCockpit",
            work_item_id=work_item,
            role=role,
            prompt_text=f"Prompt for {work_item}",
            idempotency_key=f"ws:{role}:{work_item}",
        ),
        uow_factory=application.state.uow_factory,
    )


def test_websocket_sends_versioned_envelope_with_minimal_functional_payload_and_ack(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)

    with TestClient(application) as client:
        dispatch = _create_dispatch(application)

        with client.websocket_connect("/api/companion/ws") as websocket:
            message = websocket.receive_json()

            assert message["version"] == 1
            assert message["type"] == "prompt"
            assert set(message) == {"version", "type", "delivery_id", "payload"}
            assert message["payload"] == {
                "session": "DevCockpit:DEV:DC-011",
                "text": "Prompt for DC-011",
            }
            assert set(message["payload"]) == {"session", "text"}

            websocket.send_json(
                {
                    "version": 1,
                    "type": "ack",
                    "delivery_id": message["delivery_id"],
                }
            )
            websocket.send_json({"version": 1, "type": "ping"})
            assert websocket.receive_json() == {"version": 1, "type": "pong"}

        with SqlAlchemyUnitOfWork(application.state.session_factory) as uow:
            delivery = uow.prompt_deliveries.get_by_dispatch_id(dispatch.dispatch_id)
            assert delivery is not None
            assert delivery.is_acknowledged is True
            assert delivery.attempt_count == 1
            assert uow.prompt_dispatches.get(dispatch.dispatch_id).status.value == "PREPARED"


def test_disconnect_before_ack_replays_same_delivery_after_reconnect(tmp_path: Path) -> None:
    application = _application(tmp_path)

    with TestClient(application) as client:
        dispatch = _create_dispatch(application)

        with client.websocket_connect("/api/companion/ws") as first:
            first_message = first.receive_json()

        with client.websocket_connect("/api/companion/ws") as second:
            replay = second.receive_json()
            assert replay["delivery_id"] == first_message["delivery_id"]
            assert replay["payload"] == first_message["payload"]
            second.send_json(
                {
                    "version": 1,
                    "type": "ack",
                    "delivery_id": replay["delivery_id"],
                }
            )
            second.send_json({"version": 1, "type": "ping"})
            assert second.receive_json()["type"] == "pong"

        with SqlAlchemyUnitOfWork(application.state.session_factory) as uow:
            delivery = uow.prompt_deliveries.get_by_dispatch_id(dispatch.dispatch_id)
            assert delivery.attempt_count == 2
            assert delivery.is_acknowledged is True


def test_protocol_errors_are_explicit(tmp_path: Path) -> None:
    application = _application(tmp_path)

    with TestClient(application) as client:
        with client.websocket_connect("/api/companion/ws") as websocket:
            websocket.send_text("{")
            assert websocket.receive_json()["code"] == "invalid_json"

            websocket.send_json({"version": 1, "type": "chatgpt_response", "text": "invalid"})
            assert websocket.receive_json()["code"] == "invalid_chatgpt_response"

            websocket.send_json({"version": 1, "type": "unknown"})
            assert websocket.receive_json()["code"] == "unknown_type"

            websocket.send_json(
                {
                    "version": 1,
                    "type": "ack",
                    "delivery_id": str(uuid4()),
                }
            )
            assert websocket.receive_json()["code"] == "unknown_delivery_ack"

            websocket.send_json({"version": 999, "type": "ping"})
            assert websocket.receive_json()["code"] == "unsupported_version"


def test_duplicate_ack_is_idempotent_and_connection_stays_usable(tmp_path: Path) -> None:
    application = _application(tmp_path)

    with TestClient(application) as client:
        _create_dispatch(application)
        with client.websocket_connect("/api/companion/ws") as websocket:
            message = websocket.receive_json()
            ack = {
                "version": 1,
                "type": "ack",
                "delivery_id": message["delivery_id"],
            }
            websocket.send_json(ack)
            websocket.send_json(ack)
            websocket.send_json({"version": 1, "type": "ping"})
            assert websocket.receive_json() == {"version": 1, "type": "pong"}


def test_multiple_sessions_are_delivered_without_cross_association(tmp_path: Path) -> None:
    application = _application(tmp_path)

    with TestClient(application) as client:
        first = _create_dispatch(application, work_item="DC-011-A", role="DEV")
        second = _create_dispatch(application, work_item="DC-011-B", role="ARCH")

        with client.websocket_connect("/api/companion/ws") as websocket:
            messages = [websocket.receive_json(), websocket.receive_json()]
            by_session = {message["payload"]["session"]: message for message in messages}

            assert set(by_session) == {
                "DevCockpit:DEV:DC-011-A",
                "DevCockpit:ARCH:DC-011-B",
            }

            for message in messages:
                websocket.send_json(
                    {
                        "version": 1,
                        "type": "ack",
                        "delivery_id": message["delivery_id"],
                    }
                )
            websocket.send_json({"version": 1, "type": "ping"})
            assert websocket.receive_json()["type"] == "pong"

        with SqlAlchemyUnitOfWork(application.state.session_factory) as uow:
            assert uow.prompt_deliveries.get_by_dispatch_id(first.dispatch_id).is_acknowledged
            assert uow.prompt_deliveries.get_by_dispatch_id(second.dispatch_id).is_acknowledged


def test_second_simultaneous_companion_is_rejected_deterministically(tmp_path: Path) -> None:
    application = _application(tmp_path)

    with TestClient(application) as client:
        with client.websocket_connect("/api/companion/ws") as first:
            with client.websocket_connect("/api/companion/ws") as second:
                with pytest.raises(WebSocketDisconnect) as exc:
                    second.receive_text()
                assert exc.value.code == SINGLE_COMPANION_CLOSE_CODE

            first.send_json({"version": 1, "type": "ping"})
            assert first.receive_json() == {"version": 1, "type": "pong"}

def test_chatgpt_response_is_persisted_and_identical_replay_gets_same_ack(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    response_id = "10cd3422-3dbd-481f-a5b0-5915a1f7f5be"

    with TestClient(application) as client:
        _create_dispatch(application, work_item="DC-030")
        with client.websocket_connect("/api/companion/ws") as websocket:
            prompt = websocket.receive_json()
            websocket.send_json(
                {
                    "version": 1,
                    "type": "ack",
                    "delivery_id": prompt["delivery_id"],
                }
            )
            response_message = {
                "version": 1,
                "type": "chatgpt_response",
                "response_id": response_id,
                "delivery_id": prompt["delivery_id"],
                "payload": {
                    "session": "DevCockpit:DEV:DC-030",
                    "text": "Returned response\n\n- complete text",
                },
            }
            websocket.send_json(response_message)
            assert websocket.receive_json() == {
                "version": 1,
                "type": "chatgpt_response_ack",
                "response_id": response_id,
            }

            websocket.send_json(response_message)
            assert websocket.receive_json() == {
                "version": 1,
                "type": "chatgpt_response_ack",
                "response_id": response_id,
            }

    with SqlAlchemyUnitOfWork(application.state.session_factory) as uow:
        responses = uow.chatgpt_responses.list_all()
        assert len(responses) == 1
        assert responses[0].text == "Returned response\n\n- complete text"


def test_chatgpt_response_collision_unknown_delivery_and_session_mismatch_are_explicit(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    response_id = "10cd3422-3dbd-481f-a5b0-5915a1f7f5be"

    with TestClient(application) as client:
        _create_dispatch(application, work_item="DC-030")
        with client.websocket_connect("/api/companion/ws") as websocket:
            prompt = websocket.receive_json()
            valid = {
                "version": 1,
                "type": "chatgpt_response",
                "response_id": response_id,
                "delivery_id": prompt["delivery_id"],
                "payload": {
                    "session": "DevCockpit:DEV:DC-030",
                    "text": "first",
                },
            }
            websocket.send_json(valid)
            assert websocket.receive_json()["type"] == "chatgpt_response_ack"

            changed = {
                **valid,
                "payload": {
                    "session": "DevCockpit:DEV:DC-030",
                    "text": "changed",
                },
            }
            websocket.send_json(changed)
            collision = websocket.receive_json()
            assert collision["code"] == "response_id_conflict"
            assert collision["response_id"] == response_id

            mismatch = {
                **valid,
                "response_id": str(uuid4()),
                "payload": {
                    "session": "DevCockpit:ARCH:DC-030",
                    "text": "wrong session",
                },
            }
            websocket.send_json(mismatch)
            assert websocket.receive_json()["code"] == "session_mismatch"

            unknown = {
                **valid,
                "response_id": str(uuid4()),
                "delivery_id": str(uuid4()),
            }
            websocket.send_json(unknown)
            assert websocket.receive_json()["code"] == "unknown_delivery"


def test_chatgpt_response_over_limit_fails_without_truncation(tmp_path: Path) -> None:
    application = _application(tmp_path)

    with TestClient(application) as client:
        with client.websocket_connect("/api/companion/ws") as websocket:
            websocket.send_json(
                {
                    "version": 1,
                    "type": "chatgpt_response",
                    "response_id": str(uuid4()),
                    "delivery_id": str(uuid4()),
                    "payload": {
                        "session": "DevCockpit:DEV:DC-030",
                        "text": "x" * (512 * 1024),
                    },
                }
            )
            assert websocket.receive_json()["code"] == "message_too_large"



def test_superseded_prompt_is_not_sent_from_prepared_batch(tmp_path, monkeypatch):
    """A Handoff may cancel a dispatch after the transport reserved its batch."""
    import importlib
    main_module = importlib.import_module('app.main')
    original = main_module.prepare_prompt_deliveries_for_send
    application = _application(tmp_path)
    dispatch = _create_dispatch(application)

    def reserve_then_supersede(**kwargs):
        result = original(**kwargs)
        if result:
            with application.state.uow_factory() as uow:
                current = uow.prompt_dispatches.get(dispatch.dispatch_id)
                if current.status.value == 'PREPARED':
                    current.cancel()
                    uow.prompt_dispatches.save(current)
                    uow.commit()
        return result

    monkeypatch.setattr(main_module, 'prepare_prompt_deliveries_for_send', reserve_then_supersede)
    with TestClient(application) as client, client.websocket_connect('/api/companion/ws') as ws:
        ws.send_json({'version':1, 'type':'ping'})
        assert ws.receive_json() == {'version':1, 'type':'pong'}
