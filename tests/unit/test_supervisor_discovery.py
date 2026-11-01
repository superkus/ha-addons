"""Supervisor discovery payload and best-effort POST behavior."""

from __future__ import annotations

from typing import ClassVar

import aiohttp
import pytest
from pydantic import SecretStr

import remote_energy_meter.supervisor_discovery as discovery
from remote_energy_meter.config.models import MqttSettings


class FakeResponse:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.failure is not None:
            raise self.failure


class FakeSession:
    created: ClassVar[list[FakeSession]] = []
    response_failure: ClassVar[Exception | None] = None

    def __init__(self, *, timeout: object) -> None:
        self.timeout = timeout
        self.posts: list[tuple[str, dict[str, object], dict[str, str]]] = []
        self.created.append(self)

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    def post(self, url: str, *, json: dict[str, object], headers: dict[str, str]) -> FakeResponse:
        self.posts.append((url, json, headers))
        return FakeResponse(failure=self.response_failure)


@pytest.fixture(autouse=True)
def reset_session() -> None:
    FakeSession.created.clear()
    FakeSession.response_failure = None


def _settings(base_topic: str = "remote_energy_meter") -> MqttSettings:
    return MqttSettings(
        host="core-mosquitto",
        port=1883,
        username=SecretStr("broker-user"),
        password=SecretStr("broker-password"),
        base_topic=base_topic,
    )


def test_discovery_payload_is_exact_and_contains_no_broker_or_device_credentials() -> None:
    payload = discovery.supervisor_discovery_payload(_settings(" /meters/ "))

    assert payload == {
        "service": "remote_energy_meter",
        "config": {
            "instance_id": "remote_energy_meter",
            "base_topic": "meters",
            "inventory_topic": "meters/integration/devices",
            "schema_version": 1,
        },
    }
    serialized = str(payload)
    assert "broker-user" not in serialized
    assert "broker-password" not in serialized
    assert "core-mosquitto" not in serialized


async def test_post_uses_supervisor_token_only_as_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    assert await discovery.announce_supervisor_discovery(_settings(), token="supervisor-secret") is True

    session = FakeSession.created[0]
    assert len(session.posts) == 1
    url, payload, headers = session.posts[0]
    assert url == "http://supervisor/discovery"
    assert payload == discovery.supervisor_discovery_payload(_settings())
    assert headers["Authorization"] == "Bearer supervisor-secret"
    assert "supervisor-secret" not in str(payload)


async def test_missing_token_is_warning_only_and_does_not_open_a_session(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    with caplog.at_level("WARNING"):
        result = await discovery.announce_supervisor_discovery(_settings())

    assert result is False
    assert FakeSession.created == []
    assert "SUPERVISOR_TOKEN" in caplog.text


async def test_http_failure_is_warning_nonfatal_and_does_not_log_token_or_error_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "never-log-this"
    FakeSession.response_failure = RuntimeError(f"request rejected with {token}")
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)

    with caplog.at_level("WARNING"):
        result = await discovery.announce_supervisor_discovery(_settings(), token=token)

    assert result is False
    assert len(FakeSession.created) == 1
    assert len(FakeSession.created[0].posts) == 1
    assert token not in caplog.text
    assert "request rejected" not in caplog.text
    assert any("non-fatally" in record.getMessage() for record in caplog.records)
