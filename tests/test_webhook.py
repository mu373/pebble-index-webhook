import importlib
import json
import time

from fastapi.testclient import TestClient


def load_app(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TARGETS_CONFIG_PATH", raising=False)
    monkeypatch.setenv("WEBHOOK_TOKEN", "test-token")
    monkeypatch.setenv("TARGET_TOKEN", "target-test-token")
    monkeypatch.setenv("TARGET_URL", "http://target.test/events")
    monkeypatch.setenv(
        "TARGET_STATUS_URL_TEMPLATE", "http://target.test/events/{id}"
    )
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.main

    main = importlib.reload(app.main)
    captured = {}

    async def fake_send_event(target, event, audio_path):
        captured["target"] = target
        captured["event"] = event
        captured["audio_path"] = audio_path
        return {
            "status": "accepted",
            "id": "0123456789abcdef01234567",
            "event_id": event["event_id"],
        }

    async def fake_target_status(target, target_id):
        return {
            "metadata": {"id": target_id, "status": "completed"},
            "result": {"agent_response": "done"},
        }

    monkeypatch.setattr(main, "_send_event", fake_send_event)
    monkeypatch.setattr(main, "_fetch_target_event_status", fake_target_status)
    return main, captured


def wait_until_forwarded(client, event_id):
    for _ in range(100):
        response = client.get(
            f"/events/{event_id}",
            headers={"Authorization": "Bearer test-token"},
        )
        if response.json()["metadata"]["status"] in {
            "forwarded",
            "partial",
            "failed",
        }:
            return response.json()
        time.sleep(0.01)
    raise AssertionError("event was not forwarded")


def test_accepts_and_forwards_pebble_audio(monkeypatch, tmp_path):
    main, captured = load_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        accepted = client.post(
            "/webhooks/index01",
            headers={
                "Authorization": "Bearer test-token",
                "X-Index-Trigger": "double-click-hold",
            },
            data={
                "transcription": "Add milk to my shopping list",
                "recordedAt": "1787752800000",
                "client": "ring",
            },
            files={"audio": ("recording.m4a", b"fake-m4a", "audio/mp4")},
        )
        assert accepted.status_code == 202
        event_id = accepted.json()["event_id"]
        response = wait_until_forwarded(client, event_id)

    metadata = json.loads(
        (tmp_path / "events" / event_id / "metadata.json").read_text()
    )
    assert metadata["status"] == "forwarded"
    assert metadata["deliveries"]["default"]["status"] == "forwarded"
    assert response["target_events"]["default"]["metadata"]["status"] == "completed"
    assert captured["event"]["event_id"] == f"pebble_index:{event_id}"
    assert captured["event"]["source"] == "pebble_index"
    assert "profile" not in captured["event"]
    assert captured["event"]["content"] == [
        {
            "type": "audio",
            "attachment": "audio",
            "mime_type": "audio/mp4",
            "language": "ja",
        }
    ]
    assert captured["event"]["metadata"]["input_transcription"] == (
        "Add milk to my shopping list"
    )
    assert captured["audio_path"].name == "audio.m4a"


def test_forwards_text_when_audio_is_absent(monkeypatch, tmp_path):
    main, captured = load_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        accepted = client.post(
            "/webhooks/index01",
            headers={"Authorization": "Bearer test-token"},
            data={
                "transcription": "こんにちは",
                "recordedAt": "1787752800000",
                "client": "ring",
            },
        )
        wait_until_forwarded(client, accepted.json()["event_id"])

    assert captured["event"]["content"] == [
        {"type": "text", "text": "こんにちは"}
    ]
    assert captured["audio_path"] is None


def test_rejects_bad_token(monkeypatch, tmp_path):
    main, _ = load_app(monkeypatch, tmp_path)
    with TestClient(main.app) as client:
        response = client.post(
            "/webhooks/index01",
            headers={"Authorization": "Bearer wrong"},
            data={
                "transcription": "hello",
                "recordedAt": "1787752800000",
                "client": "ring",
            },
        )
    assert response.status_code == 401


def test_deduplicates_retries(monkeypatch, tmp_path):
    main, _ = load_app(monkeypatch, tmp_path)
    request = {
        "headers": {"Authorization": "Bearer test-token"},
        "data": {
            "transcription": "hello",
            "recordedAt": "1787752800000",
            "client": "ring",
        },
    }
    with TestClient(main.app) as client:
        first = client.post("/webhooks/index01", **request)
        second = client.post("/webhooks/index01", **request)
    assert first.status_code == 202
    assert second.json()["status"] == "duplicate"


def test_supports_legacy_gateway_configuration(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TARGETS_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TARGET_URL", raising=False)
    monkeypatch.delenv("TARGET_TOKEN", raising=False)
    monkeypatch.delenv("TARGET_STATUS_URL_TEMPLATE", raising=False)
    monkeypatch.setenv("GATEWAY_URL", "http://gateway.test")
    monkeypatch.setenv("GATEWAY_TOKEN", "legacy-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.main

    main = importlib.reload(app.main)

    target = main.settings.targets[0]
    assert target.url == "http://gateway.test/v1/events"
    assert target.auth_token == "legacy-token"
    assert target.status.url_template == (
        "http://gateway.test/v1/events/{id}"
    )


def test_forwards_to_multiple_yaml_targets_with_custom_templates(monkeypatch, tmp_path):
    config_path = tmp_path / "targets.yaml"
    config_path.write_text(
        """
version: 1
targets:
  - name: normalized
    url: http://normalized.test/events
    auth:
      token_env: NORMALIZED_TOKEN
    request:
      template: |
        {{ event | tojson }}

  - name: summary
    url: http://summary.test/hooks/pebble
    headers:
      X-Source: pebble
    request:
      event_field: payload
      audio_field: recording
      template: |
        {
          "id": {{ event.event_id | tojson }},
          "text": {{ event.metadata.input_transcription | tojson }}
        }

  - name: disabled
    enabled: false
    url: http://disabled.test/events
""".lstrip()
    )
    monkeypatch.setenv("TARGETS_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("NORMALIZED_TOKEN", "normalized-secret")
    monkeypatch.setenv("WEBHOOK_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import app.main

    main = importlib.reload(app.main)
    captured = []

    async def fake_send_event(target, event, audio_path):
        captured.append((target, event, audio_path))
        return {"id": f"{target.name}-id", "status": "accepted"}

    monkeypatch.setattr(main, "_send_event", fake_send_event)
    with TestClient(main.app) as client:
        accepted = client.post(
            "/webhooks/index01",
            headers={"Authorization": "Bearer test-token"},
            data={
                "transcription": "buy tea",
                "recordedAt": "1787752800000",
                "client": "ring",
            },
        )
        response = wait_until_forwarded(client, accepted.json()["event_id"])

    assert [target.name for target in main.settings.targets] == [
        "normalized",
        "summary",
    ]
    assert {target.name for target, _, _ in captured} == {"normalized", "summary"}
    assert set(response["metadata"]["deliveries"]) == {"normalized", "summary"}
    summary = main.settings.targets[1]
    rendered = json.loads(main._render_event(summary, captured[0][1]))
    assert rendered["text"] == "buy tea"
    assert summary.request.event_field == "payload"
    assert summary.request.audio_field == "recording"
    assert main._target_headers(main.settings.targets[0]) == {
        "Authorization": "Bearer normalized-secret"
    }


def test_pure_event_builders_are_deterministic(monkeypatch, tmp_path):
    main, _ = load_app(monkeypatch, tmp_path)

    event_id = main.compute_event_id("1787752800000", "ring", "hello", "abc")
    metadata = main.build_event_metadata(
        event_id,
        recorded_at="1787752800000",
        client="ring",
        trigger=None,
        transcription=" hello ",
        audio_size=3,
        audio_sha256="abc",
    )

    assert event_id == main.compute_event_id(
        "1787752800000", "ring", "hello", "abc"
    )
    assert metadata["event_id"] == event_id
    assert metadata["pebble_transcription"] == "hello"


def test_summarizes_partial_target_failures(monkeypatch, tmp_path):
    main, _ = load_app(monkeypatch, tmp_path)
    targets = (
        main.Target("first", "http://first.test"),
        main.Target("second", "http://second.test"),
    )

    deliveries, status, error = main.summarize_target_deliveries(
        targets, [{"id": "ok"}, RuntimeError("offline")]
    )

    assert status == "partial"
    assert error == "1 target deliveries failed"
    assert deliveries["first"]["status"] == "forwarded"
    assert deliveries["second"] == {"status": "failed", "error": "offline"}


def test_parses_targets_without_reading_process_environment(monkeypatch, tmp_path):
    main, _ = load_app(monkeypatch, tmp_path)
    targets = main.parse_targets_config(
        {
            "targets": [
                {
                    "name": "gateway",
                    "url": "http://gateway.test/events",
                    "auth": {"token_env": "GATEWAY_TOKEN"},
                }
            ]
        },
        "memory",
        {"GATEWAY_TOKEN": "provided"},
    )

    assert targets[0].auth_token == "provided"
