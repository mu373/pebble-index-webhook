from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx
import yaml
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, status
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("pebble-index-adapter")
template_environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


@dataclass(frozen=True)
class TargetRequest:
    event_field: str = "event"
    audio_field: str = "audio"
    audio_filename: str = "index-recording.m4a"
    audio_mime_type: str = "audio/mp4"
    include_audio: bool = True
    event_template: str = "{{ event | tojson }}"


@dataclass(frozen=True)
class TargetStatus:
    url_template: str = ""
    id_field: str = "id"


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    timeout_seconds: float = 30
    headers: dict[str, str] | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    auth_token: str = ""
    request: TargetRequest = TargetRequest()
    status: TargetStatus = TargetStatus()


def _legacy_target_url() -> str:
    gateway_url = os.getenv("GATEWAY_URL", "").rstrip("/")
    return f"{gateway_url}/v1/events" if gateway_url else ""


def _legacy_status_url_template() -> str:
    if os.getenv("TARGET_URL"):
        return ""
    gateway_url = os.getenv("GATEWAY_URL", "").rstrip("/")
    return f"{gateway_url}/v1/events/{{id}}" if gateway_url else ""


def _require_mapping(value: Any, location: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be a mapping")
    return value


def _require_boolean(value: Any, location: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{location} must be true or false")
    return value


def _load_yaml_targets(path: Path) -> tuple[Target, ...]:
    if not path.is_file():
        raise ValueError(f"target configuration does not exist: {path}")
    document = yaml.safe_load(path.read_text())
    root = _require_mapping(document, str(path))
    if root.get("version", 1) != 1:
        raise ValueError(f"unsupported target configuration version in {path}")
    target_values = root.get("targets")
    if not isinstance(target_values, list):
        raise ValueError(f"{path}: targets must be a list")

    targets: list[Target] = []
    names: set[str] = set()
    for index, value in enumerate(target_values):
        item = _require_mapping(value, f"targets[{index}]")
        if not _require_boolean(
            item.get("enabled", True), f"targets[{index}].enabled"
        ):
            continue
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if not name or not url:
            raise ValueError(f"targets[{index}] requires name and url")
        if name in names:
            raise ValueError(f"duplicate target name: {name}")
        names.add(name)

        auth = _require_mapping(item.get("auth"), f"targets[{index}].auth")
        token = str(auth.get("token", ""))
        token_env = str(auth.get("token_env", "")).strip()
        if token and token_env:
            raise ValueError(f"target {name} cannot set both token and token_env")
        if token_env:
            token = os.getenv(token_env, "")
            if not token:
                raise ValueError(
                    f"target {name} requires environment variable {token_env}"
                )

        header_values = _require_mapping(
            item.get("headers"), f"targets[{index}].headers"
        )
        headers = {str(key): str(header) for key, header in header_values.items()}
        request = _require_mapping(
            item.get("request"), f"targets[{index}].request"
        )
        status_config = _require_mapping(
            item.get("status"), f"targets[{index}].status"
        )
        targets.append(
            Target(
                name=name,
                url=url,
                timeout_seconds=float(item.get("timeout_seconds", 30)),
                headers=headers,
                auth_header=str(auth.get("header", "Authorization")),
                auth_scheme=str(auth.get("scheme", "Bearer")),
                auth_token=token,
                request=TargetRequest(
                    event_field=str(request.get("event_field", "event")),
                    audio_field=str(request.get("audio_field", "audio")),
                    audio_filename=str(
                        request.get("audio_filename", "index-recording.m4a")
                    ),
                    audio_mime_type=str(request.get("audio_mime_type", "audio/mp4")),
                    include_audio=_require_boolean(
                        request.get("include_audio", True),
                        f"targets[{index}].request.include_audio",
                    ),
                    event_template=str(
                        request.get("template", "{{ event | tojson }}")
                    ),
                ),
                status=TargetStatus(
                    url_template=str(status_config.get("url_template", "")),
                    id_field=str(status_config.get("id_field", "id")),
                ),
            )
        )
    if not targets:
        raise ValueError(f"{path}: at least one target must be enabled")
    return tuple(targets)


def _environment_target() -> tuple[Target, ...]:
    url = os.getenv("TARGET_URL", _legacy_target_url())
    if not url:
        return ()
    return (
        Target(
            name=os.getenv("TARGET_NAME", "default"),
            url=url,
            timeout_seconds=float(
                os.getenv(
                    "TARGET_TIMEOUT_SECONDS",
                    os.getenv("GATEWAY_TIMEOUT_SECONDS", "30"),
                )
            ),
            auth_header=os.getenv("TARGET_AUTH_HEADER", "Authorization"),
            auth_scheme=os.getenv("TARGET_AUTH_SCHEME", "Bearer"),
            auth_token=os.getenv("TARGET_TOKEN", os.getenv("GATEWAY_TOKEN", "")),
            request=TargetRequest(
                event_field=os.getenv("TARGET_EVENT_FIELD", "event"),
                audio_field=os.getenv("TARGET_AUDIO_FIELD", "audio"),
                audio_filename=os.getenv(
                    "TARGET_AUDIO_FILENAME", "index-recording.m4a"
                ),
                audio_mime_type=os.getenv("TARGET_AUDIO_MIME_TYPE", "audio/mp4"),
                include_audio=os.getenv("TARGET_INCLUDE_AUDIO", "true").lower()
                not in {"0", "false", "no"},
                event_template=os.getenv(
                    "TARGET_EVENT_TEMPLATE", "{{ event | tojson }}"
                ),
            ),
            status=TargetStatus(
                url_template=os.getenv(
                    "TARGET_STATUS_URL_TEMPLATE", _legacy_status_url_template()
                ),
                id_field=os.getenv("TARGET_ID_FIELD", "id"),
            ),
        ),
    )


def _targets_config_path() -> Path | None:
    config_path = os.getenv("TARGETS_CONFIG_PATH", "").strip()
    if config_path:
        return Path(config_path)
    default_path = Path("./targets.yaml")
    return default_path if default_path.is_file() else None


def _load_targets() -> tuple[Target, ...]:
    config_path = _targets_config_path()
    return _load_yaml_targets(config_path) if config_path else _environment_target()


@dataclass(frozen=True)
class Settings:
    webhook_token: str = os.getenv("WEBHOOK_TOKEN", "")
    targets_config_path: str = str(_targets_config_path() or "")
    targets: tuple[Target, ...] = _load_targets()
    sender_id: str = os.getenv("PEBBLE_SENDER_ID", "")
    conversation_id: str = os.getenv("PEBBLE_CONVERSATION_ID", "personal")
    language_hint: str = os.getenv("PEBBLE_LANGUAGE_HINT", "ja")
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data"))
    max_audio_bytes: int = int(os.getenv("MAX_AUDIO_BYTES", str(25 * 1024 * 1024)))
    workers: int = int(os.getenv("WORKERS", "1"))


settings = Settings()
queue: asyncio.Queue[str] = asyncio.Queue()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _authorize(authorization: str | None) -> None:
    if not settings.webhook_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WEBHOOK_TOKEN is not configured",
        )
    expected = f"Bearer {settings.webhook_token}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid webhook token")


async def _save_audio(upload: UploadFile, destination: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with destination.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_audio_bytes:
                output.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="audio file is too large")
            digest.update(chunk)
            output.write(chunk)
    return size, digest.hexdigest()


def _normalized_event(
    local_event_id: str, metadata: dict[str, Any], has_audio: bool
) -> dict[str, Any]:
    if has_audio:
        content = [
            {
                "type": "audio",
                "attachment": "audio",
                "mime_type": "audio/mp4",
                "language": settings.language_hint or None,
            }
        ]
    else:
        content = [{"type": "text", "text": metadata["pebble_transcription"]}]

    return {
        "event_id": f"pebble_index:{local_event_id}",
        "source": "pebble_index",
        "sender_id": settings.sender_id or metadata["client"],
        "conversation_id": settings.conversation_id,
        "content": content,
        "reply": {"adapter": "pebble_index", "target": local_event_id},
        "metadata": {
            "recorded_at": metadata["recorded_at"],
            "client": metadata["client"],
            "trigger": metadata.get("trigger"),
            "input_transcription": metadata.get("pebble_transcription"),
        },
    }


def _target_headers(target: Target) -> dict[str, str]:
    headers = dict(target.headers or {})
    if target.auth_token:
        value = f"{target.auth_scheme} {target.auth_token}".strip()
        headers[target.auth_header] = value
    return headers


def _render_event(target: Target, event: dict[str, Any]) -> str:
    rendered = template_environment.from_string(target.request.event_template).render(
        event=event
    )
    try:
        json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ValueError(f"target {target.name} template did not render valid JSON") from exc
    return rendered


async def _send_event(
    target: Target, event: dict[str, Any], audio_path: Path | None
) -> dict[str, Any]:
    headers = _target_headers(target)
    data = {target.request.event_field: _render_event(target, event)}
    async with httpx.AsyncClient(timeout=target.timeout_seconds) as client:
        if audio_path is None or not target.request.include_audio:
            response = await client.post(target.url, data=data, headers=headers)
        else:
            with audio_path.open("rb") as audio:
                response = await client.post(
                    target.url,
                    data=data,
                    files={
                        target.request.audio_field: (
                            target.request.audio_filename,
                            audio,
                            target.request.audio_mime_type,
                        )
                    },
                    headers=headers,
                )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"target {target.name} response must be a JSON object")
        return result


async def _target_event_status(target: Target, target_id: str) -> dict[str, Any]:
    if not target.status.url_template:
        raise RuntimeError(f"target {target.name} does not configure status lookup")
    encoded_id = quote(target_id, safe="")
    status_url = target.status.url_template.format(id=encoded_id)
    async with httpx.AsyncClient(timeout=target.timeout_seconds) as client:
        response = await client.get(
            status_url,
            headers=_target_headers(target),
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"target {target.name} status must be a JSON object")
        return result


async def _forward(local_event_id: str) -> None:
    event_dir = settings.data_dir / "events" / local_event_id
    metadata_path = event_dir / "metadata.json"
    metadata = _read_json(metadata_path)
    try:
        metadata["status"] = "forwarding"
        _write_json(metadata_path, metadata)
        if not settings.targets:
            raise RuntimeError("no targets are configured")
        audio_path = event_dir / "audio.m4a"
        event = _normalized_event(local_event_id, metadata, audio_path.exists())
        results = await asyncio.gather(
            *(
                _send_event(
                    target,
                    event,
                    audio_path if audio_path.exists() else None,
                )
                for target in settings.targets
            ),
            return_exceptions=True,
        )
        deliveries: dict[str, Any] = {}
        failed = 0
        for target, result in zip(settings.targets, results):
            if isinstance(result, BaseException):
                failed += 1
                deliveries[target.name] = {
                    "status": "failed",
                    "error": str(result),
                }
                logger.error(
                    "failed to forward event %s to %s", local_event_id, target.name
                )
                continue
            deliveries[target.name] = {
                "status": "forwarded",
                "response": result,
            }
            logger.info(
                "forwarded event %s target=%s target_id=%s",
                local_event_id,
                target.name,
                result.get(target.status.id_field),
            )

        metadata["deliveries"] = deliveries
        metadata.pop("target", None)
        metadata.pop("gateway", None)
        if failed == 0:
            metadata["status"] = "forwarded"
            metadata.pop("error", None)
        elif failed == len(settings.targets):
            metadata["status"] = "failed"
            metadata["error"] = "all target deliveries failed"
        else:
            metadata["status"] = "partial"
            metadata["error"] = f"{failed} target deliveries failed"
        _write_json(metadata_path, metadata)
    except Exception as exc:
        logger.exception("failed to forward event %s", local_event_id)
        metadata["status"] = "failed"
        metadata["error"] = str(exc)
        _write_json(metadata_path, metadata)


async def _worker() -> None:
    while True:
        event_id = await queue.get()
        try:
            await _forward(event_id)
        finally:
            queue.task_done()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    events_dir = settings.data_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    tasks = [asyncio.create_task(_worker()) for _ in range(max(settings.workers, 1))]
    for metadata_path in events_dir.glob("*/metadata.json"):
        metadata = _read_json(metadata_path)
        if metadata.get("status") in {"received", "forwarding"}:
            await queue.put(metadata["event_id"])

    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Pebble Index 01 input adapter", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "configured": bool(settings.webhook_token and settings.targets),
        "queued": queue.qsize(),
        "targets": [
            {
                "name": target.name,
                "url": target.url,
                "status_tracking": bool(target.status.url_template),
            }
            for target in settings.targets
        ],
    }


@app.get("/events/{event_id}")
async def event_status(
    event_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize(authorization)
    if len(event_id) != 24 or any(
        character not in "0123456789abcdef" for character in event_id
    ):
        raise HTTPException(status_code=404, detail="event not found")
    metadata_path = settings.data_dir / "events" / event_id / "metadata.json"
    if not metadata_path.exists():
        raise HTTPException(status_code=404, detail="event not found")
    metadata = _read_json(metadata_path)
    response: dict[str, Any] = {"metadata": metadata}
    targets_by_name = {target.name: target for target in settings.targets}
    target_events: dict[str, Any] = {}
    for name, delivery in metadata.get("deliveries", {}).items():
        target = targets_by_name.get(name)
        target_response = delivery.get("response", {})
        if target is None or not target.status.url_template:
            continue
        target_id = target_response.get(target.status.id_field)
        if not target_id:
            continue
        try:
            target_events[name] = await _target_event_status(target, str(target_id))
        except Exception as exc:
            target_events[name] = {"error": str(exc)}
    if target_events:
        response["target_events"] = target_events

    # Read single-target metadata saved by earlier versions of the adapter.
    old_target_response = metadata.get("target") or metadata.get("gateway", {})
    if old_target_response and settings.targets:
        target = settings.targets[0]
        target_id = old_target_response.get(target.status.id_field)
        if target_id and target.status.url_template:
            try:
                response["target_event"] = await _target_event_status(
                    target, str(target_id)
                )
            except Exception as exc:
                response["target_error"] = str(exc)
    return response


@app.post("/webhooks/index01", status_code=status.HTTP_202_ACCEPTED)
async def index01_webhook(
    audio: UploadFile | None = File(default=None),
    transcription: str | None = Form(default=None),
    recorded_at: str = Form(alias="recordedAt"),
    client: str = Form(),
    authorization: str | None = Header(default=None),
    x_index_trigger: str | None = Header(default=None),
) -> dict[str, Any]:
    _authorize(authorization)
    if audio is None and not transcription:
        raise HTTPException(status_code=422, detail="audio or transcription is required")

    seed = f"{recorded_at}\0{client}\0{transcription or ''}".encode()
    provisional_id = hashlib.sha256(seed).hexdigest()[:24]
    provisional_dir = settings.data_dir / "events" / f".{provisional_id}"
    provisional_dir.mkdir(parents=True, exist_ok=True)

    audio_size = 0
    audio_sha256 = ""
    try:
        if audio is not None:
            audio_size, audio_sha256 = await _save_audio(
                audio, provisional_dir / "audio.m4a"
            )

        event_id = hashlib.sha256(seed + audio_sha256.encode()).hexdigest()[:24]
        event_dir = settings.data_dir / "events" / event_id
        if event_dir.exists():
            for child in provisional_dir.iterdir():
                child.unlink()
            provisional_dir.rmdir()
            return {"status": "duplicate", "event_id": event_id}

        provisional_dir.rename(event_dir)
        metadata = {
            "event_id": event_id,
            "status": "received",
            "recorded_at": recorded_at,
            "client": client,
            "trigger": x_index_trigger,
            "audio_size": audio_size,
            "audio_sha256": audio_sha256 or None,
            "pebble_transcription": transcription.strip() if transcription else None,
        }
        _write_json(event_dir / "metadata.json", metadata)
        await queue.put(event_id)
        return {"status": "accepted", "event_id": event_id}
    except Exception:
        if provisional_dir.exists():
            for child in provisional_dir.iterdir():
                child.unlink()
            provisional_dir.rmdir()
        raise
