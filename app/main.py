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
from typing import Any, AsyncIterator, Mapping
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


@dataclass(frozen=True)
class TargetEventRequest:
    url: str
    timeout_seconds: float
    headers: dict[str, str]
    data: dict[str, str]
    audio_field: str | None
    audio_filename: str | None
    audio_mime_type: str | None


@dataclass(frozen=True)
class TargetStatusLookup:
    name: str
    target: Target
    target_id: str


@dataclass(frozen=True)
class TargetStatusRequest:
    url: str
    timeout_seconds: float
    headers: dict[str, str]


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


def _parse_target(
    value: Any, index: int, environment: Mapping[str, str]
) -> Target | None:
    item = _require_mapping(value, f"targets[{index}]")
    if not _require_boolean(item.get("enabled", True), f"targets[{index}].enabled"):
        return None
    name = str(item.get("name", "")).strip()
    url = str(item.get("url", "")).strip()
    if not name or not url:
        raise ValueError(f"targets[{index}] requires name and url")
    auth = _require_mapping(item.get("auth"), f"targets[{index}].auth")
    token = str(auth.get("token", ""))
    token_env = str(auth.get("token_env", "")).strip()
    if token and token_env:
        raise ValueError(f"target {name} cannot set both token and token_env")
    if token_env:
        token = environment.get(token_env, "")
        if not token:
            raise ValueError(f"target {name} requires environment variable {token_env}")
    header_values = _require_mapping(item.get("headers"), f"targets[{index}].headers")
    request = _require_mapping(item.get("request"), f"targets[{index}].request")
    status_config = _require_mapping(item.get("status"), f"targets[{index}].status")
    return Target(
        name=name,
        url=url,
        timeout_seconds=float(item.get("timeout_seconds", 30)),
        headers={str(key): str(header) for key, header in header_values.items()},
        auth_header=str(auth.get("header", "Authorization")),
        auth_scheme=str(auth.get("scheme", "Bearer")),
        auth_token=token,
        request=TargetRequest(
            event_field=str(request.get("event_field", "event")),
            audio_field=str(request.get("audio_field", "audio")),
            audio_filename=str(request.get("audio_filename", "index-recording.m4a")),
            audio_mime_type=str(request.get("audio_mime_type", "audio/mp4")),
            include_audio=_require_boolean(
                request.get("include_audio", True),
                f"targets[{index}].request.include_audio",
            ),
            event_template=str(request.get("template", "{{ event | tojson }}")),
        ),
        status=TargetStatus(
            url_template=str(status_config.get("url_template", "")),
            id_field=str(status_config.get("id_field", "id")),
        ),
    )


def parse_targets_config(
    document: Any, location: str, environment: Mapping[str, str]
) -> tuple[Target, ...]:
    """Validate target configuration using only caller-provided values."""
    root = _require_mapping(document, location)
    if root.get("version", 1) != 1:
        raise ValueError(f"unsupported target configuration version in {location}")
    target_values = root.get("targets")
    if not isinstance(target_values, list):
        raise ValueError(f"{location}: targets must be a list")
    targets = [
        target
        for index, value in enumerate(target_values)
        if (target := _parse_target(value, index, environment)) is not None
    ]
    names: set[str] = set()
    for target in targets:
        if target.name in names:
            raise ValueError(f"duplicate target name: {target.name}")
        names.add(target.name)
    if not targets:
        raise ValueError(f"{location}: at least one target must be enabled")
    return tuple(targets)


def _load_yaml_targets(path: Path) -> tuple[Target, ...]:
    if not path.is_file():
        raise ValueError(f"target configuration does not exist: {path}")
    return parse_targets_config(yaml.safe_load(path.read_text()), str(path), os.environ)


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


def _require_webhook_authorization(authorization: str | None) -> None:
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


def build_normalized_event(
    local_event_id: str,
    metadata: Mapping[str, Any],
    has_audio: bool,
    *,
    sender_id: str,
    conversation_id: str,
    language_hint: str,
) -> dict[str, Any]:
    """Build the target-neutral event without reading runtime state."""
    if has_audio:
        content = [
            {
                "type": "audio",
                "attachment": "audio",
                "mime_type": "audio/mp4",
                "language": language_hint or None,
            }
        ]
    else:
        content = [{"type": "text", "text": metadata["pebble_transcription"]}]

    return {
        "event_id": f"pebble_index:{local_event_id}",
        "source": "pebble_index",
        "sender_id": sender_id or metadata["client"],
        "conversation_id": conversation_id,
        "content": content,
        "reply": {"adapter": "pebble_index", "target": local_event_id},
        "metadata": {
            "recorded_at": metadata["recorded_at"],
            "client": metadata["client"],
            "trigger": metadata.get("trigger"),
            "input_transcription": metadata.get("pebble_transcription"),
        },
    }


def build_target_headers(target: Target) -> dict[str, str]:
    """Build target headers without mutating target configuration."""
    headers = dict(target.headers or {})
    if target.auth_token:
        value = f"{target.auth_scheme} {target.auth_token}".strip()
        headers[target.auth_header] = value
    return headers


def render_target_event(target: Target, event: Mapping[str, Any]) -> str:
    """Render and validate the target-specific event payload."""
    rendered = template_environment.from_string(target.request.event_template).render(
        event=event
    )
    try:
        json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"target {target.name} template did not render valid JSON"
        ) from exc
    return rendered


def build_target_event_request(
    target: Target, event: Mapping[str, Any], has_audio: bool
) -> TargetEventRequest:
    """Describe an outbound target request without performing I/O."""
    include_audio = has_audio and target.request.include_audio
    return TargetEventRequest(
        url=target.url,
        timeout_seconds=target.timeout_seconds,
        headers=build_target_headers(target),
        data={target.request.event_field: render_target_event(target, event)},
        audio_field=target.request.audio_field if include_audio else None,
        audio_filename=target.request.audio_filename if include_audio else None,
        audio_mime_type=target.request.audio_mime_type if include_audio else None,
    )


async def _send_event(
    target: Target, event: dict[str, Any], audio_path: Path | None
) -> dict[str, Any]:
    request = build_target_event_request(target, event, audio_path is not None)
    async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
        if audio_path is None or request.audio_field is None:
            response = await client.post(
                request.url, data=request.data, headers=request.headers
            )
        else:
            with audio_path.open("rb") as audio:
                response = await client.post(
                    request.url,
                    data=request.data,
                    files={
                        request.audio_field: (
                            request.audio_filename,
                            audio,
                            request.audio_mime_type,
                        )
                    },
                    headers=request.headers,
                )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"target {target.name} response must be a JSON object")
        return result


def build_target_status_url(target: Target, target_id: str) -> str:
    """Build a safely encoded status URL from target configuration."""
    if not target.status.url_template:
        raise ValueError(f"target {target.name} does not configure status lookup")
    return target.status.url_template.format(id=quote(target_id, safe=""))


def build_target_status_request(target: Target, target_id: str) -> TargetStatusRequest:
    """Describe an outbound target status request without performing I/O."""
    return TargetStatusRequest(
        url=build_target_status_url(target, target_id),
        timeout_seconds=target.timeout_seconds,
        headers=build_target_headers(target),
    )


async def _fetch_target_event_status(target: Target, target_id: str) -> dict[str, Any]:
    request = build_target_status_request(target, target_id)
    async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
        response = await client.get(
            request.url,
            headers=request.headers,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"target {target.name} status must be a JSON object")
        return result


def summarize_target_deliveries(
    targets: tuple[Target, ...], results: list[Any]
) -> tuple[dict[str, Any], str, str | None]:
    """Aggregate target outcomes without performing I/O."""
    deliveries: dict[str, Any] = {}
    failed = 0
    for target, result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            failed += 1
            deliveries[target.name] = {"status": "failed", "error": str(result)}
        else:
            deliveries[target.name] = {"status": "forwarded", "response": result}
    if failed == 0:
        return deliveries, "forwarded", None
    if failed == len(targets):
        return deliveries, "failed", "all target deliveries failed"
    return deliveries, "partial", f"{failed} target deliveries failed"


def find_target_status_lookups(
    targets: tuple[Target, ...], metadata: Mapping[str, Any]
) -> tuple[TargetStatusLookup, ...]:
    """Find configured status lookups represented by stored deliveries."""
    targets_by_name = {target.name: target for target in targets}
    lookups = []
    for name, delivery in metadata.get("deliveries", {}).items():
        target = targets_by_name.get(name)
        if target is None or not target.status.url_template:
            continue
        target_response = delivery.get("response", {})
        target_id = target_response.get(target.status.id_field)
        if target_id:
            lookups.append(TargetStatusLookup(name, target, str(target_id)))
    return tuple(lookups)


def find_legacy_target_status_lookup(
    targets: tuple[Target, ...], metadata: Mapping[str, Any]
) -> TargetStatusLookup | None:
    """Find a status lookup in metadata written by a pre-multi-target adapter."""
    old_target_response = metadata.get("target") or metadata.get("gateway", {})
    if not old_target_response or not targets:
        return None
    target = targets[0]
    target_id = old_target_response.get(target.status.id_field)
    if not target_id or not target.status.url_template:
        return None
    return TargetStatusLookup(target.name, target, str(target_id))


async def _forward_event_to_targets(local_event_id: str) -> None:
    event_dir = settings.data_dir / "events" / local_event_id
    metadata_path = event_dir / "metadata.json"
    metadata = _read_json(metadata_path)
    try:
        metadata["status"] = "forwarding"
        _write_json(metadata_path, metadata)
        if not settings.targets:
            raise RuntimeError("no targets are configured")
        audio_path = event_dir / "audio.m4a"
        event = build_normalized_event(
            local_event_id,
            metadata,
            audio_path.exists(),
            sender_id=settings.sender_id,
            conversation_id=settings.conversation_id,
            language_hint=settings.language_hint,
        )
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
        for target, result in zip(settings.targets, results):
            if isinstance(result, BaseException):
                logger.error(
                    "failed to forward event %s to %s", local_event_id, target.name
                )
                continue
            logger.info(
                "forwarded event %s target=%s target_id=%s",
                local_event_id,
                target.name,
                result.get(target.status.id_field),
            )

        deliveries, delivery_status, delivery_error = summarize_target_deliveries(
            settings.targets, results
        )
        metadata["deliveries"] = deliveries
        metadata.pop("target", None)
        metadata.pop("gateway", None)
        metadata["status"] = delivery_status
        if delivery_error is None:
            metadata.pop("error", None)
        else:
            metadata["error"] = delivery_error
        _write_json(metadata_path, metadata)
    except Exception as exc:
        logger.exception("failed to forward event %s", local_event_id)
        metadata["status"] = "failed"
        metadata["error"] = str(exc)
        _write_json(metadata_path, metadata)


async def _run_forwarding_worker() -> None:
    while True:
        event_id = await queue.get()
        try:
            await _forward_event_to_targets(event_id)
        finally:
            queue.task_done()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    events_dir = settings.data_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        asyncio.create_task(_run_forwarding_worker())
        for _ in range(max(settings.workers, 1))
    ]
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
    _require_webhook_authorization(authorization)
    if len(event_id) != 24 or any(
        character not in "0123456789abcdef" for character in event_id
    ):
        raise HTTPException(status_code=404, detail="event not found")
    metadata_path = settings.data_dir / "events" / event_id / "metadata.json"
    if not metadata_path.exists():
        raise HTTPException(status_code=404, detail="event not found")
    metadata = _read_json(metadata_path)
    response: dict[str, Any] = {"metadata": metadata}
    target_events: dict[str, Any] = {}
    for lookup in find_target_status_lookups(settings.targets, metadata):
        try:
            target_events[lookup.name] = await _fetch_target_event_status(
                lookup.target, lookup.target_id
            )
        except Exception as exc:
            target_events[lookup.name] = {"error": str(exc)}
    if target_events:
        response["target_events"] = target_events

    # Read single-target metadata saved by earlier versions of the adapter.
    legacy_lookup = find_legacy_target_status_lookup(settings.targets, metadata)
    if legacy_lookup is not None:
        try:
            response["target_event"] = await _fetch_target_event_status(
                legacy_lookup.target, legacy_lookup.target_id
            )
        except Exception as exc:
            response["target_error"] = str(exc)
    return response


def compute_event_id(
    recorded_at: str,
    client: str,
    transcription: str | None,
    audio_sha256: str = "",
) -> str:
    seed = f"{recorded_at}\0{client}\0{transcription or ''}".encode()
    return hashlib.sha256(seed + audio_sha256.encode()).hexdigest()[:24]


def build_event_metadata(
    event_id: str,
    *,
    recorded_at: str,
    client: str,
    trigger: str | None,
    transcription: str | None,
    audio_size: int,
    audio_sha256: str,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "status": "received",
        "recorded_at": recorded_at,
        "client": client,
        "trigger": trigger,
        "audio_size": audio_size,
        "audio_sha256": audio_sha256 or None,
        "pebble_transcription": transcription.strip() if transcription else None,
    }


def _discard_provisional_event(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        child.unlink()
    path.rmdir()


@app.post("/webhooks/index01", status_code=status.HTTP_202_ACCEPTED)
async def index01_webhook(
    audio: UploadFile | None = File(default=None),
    transcription: str | None = Form(default=None),
    recorded_at: str = Form(alias="recordedAt"),
    client: str = Form(),
    authorization: str | None = Header(default=None),
    x_index_trigger: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_webhook_authorization(authorization)
    if audio is None and not transcription:
        raise HTTPException(
            status_code=422, detail="audio or transcription is required"
        )

    provisional_id = compute_event_id(recorded_at, client, transcription)
    provisional_dir = settings.data_dir / "events" / f".{provisional_id}"
    provisional_dir.mkdir(parents=True, exist_ok=True)

    audio_size = 0
    audio_sha256 = ""
    try:
        if audio is not None:
            audio_size, audio_sha256 = await _save_audio(
                audio, provisional_dir / "audio.m4a"
            )

        event_id = compute_event_id(recorded_at, client, transcription, audio_sha256)
        event_dir = settings.data_dir / "events" / event_id
        if event_dir.exists():
            _discard_provisional_event(provisional_dir)
            return {"status": "duplicate", "event_id": event_id}

        provisional_dir.rename(event_dir)
        metadata = build_event_metadata(
            event_id,
            recorded_at=recorded_at,
            client=client,
            trigger=x_index_trigger,
            transcription=transcription,
            audio_size=audio_size,
            audio_sha256=audio_sha256,
        )
        _write_json(event_dir / "metadata.json", metadata)
        await queue.put(event_id)
        return {"status": "accepted", "event_id": event_id}
    except Exception:
        _discard_provisional_event(provisional_dir)
        raise
