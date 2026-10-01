from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_application_factory_returns_fastapi_app() -> None:
    application = create_app(Settings(app_name="DevCockpit Test"))

    assert isinstance(application, FastAPI)
    assert application.title == "DevCockpit Test"


def test_health_endpoint_is_available_without_external_services() -> None:
    client = TestClient(create_app(Settings()))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
