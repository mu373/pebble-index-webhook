# Pebble Index 01 input adapter

A thin, durable adapter between Pebble's multipart webhook and a configurable HTTP
event receiver.

This service does only four things:

1. Authenticate the public Pebble webhook.
2. Validate and durably save the payload.
3. Convert Pebble fields into a normalized event envelope.
4. Forward the event and optional audio to a configured target.

It does not transcribe audio, select an agent, load tools, or deliver task results.
Those concerns belong to the target service.

## Flow

```text
Index 01 -> Pebble app -> /webhooks/index01 -> configured HTTP target
```

The public endpoint remains:

```text
POST /webhooks/index01
Authorization: Bearer <WEBHOOK_TOKEN>
```

The adapter immediately returns `202` after durable local acceptance. Its worker sends
the event concurrently to every enabled target in `targets.yaml`. Each target controls
its URL, authentication, headers, multipart field names, JSON template, and optional
status lookup. An audio event defaults to the Japanese language hint `ja`;
`PEBBLE_LANGUAGE_HINT` can override or disable it.

## Target contract

The adapter sends `multipart/form-data` with an `event` field containing JSON:

```json
{
  "event_id": "pebble_index:<local-id>",
  "source": "pebble_index",
  "sender_id": "<configured sender or Pebble client>",
  "conversation_id": "personal",
  "content": [{"type": "text", "text": "example"}],
  "reply": {"adapter": "pebble_index", "target": "<local-id>"},
  "metadata": {
    "recorded_at": "<Pebble timestamp>",
    "client": "<Pebble client>",
    "trigger": "<optional trigger>",
    "input_transcription": "<optional Pebble transcript>"
  }
}
```

This is the template context available as `event`. A target's Jinja template transforms
it into the JSON placed in that target's configured multipart event field. For
recordings, the request also includes a file using the target's configured audio field.
The target must return a JSON object after accepting the event.

## Configure

```bash
cp .env.example .env
cp targets.example.yaml targets.yaml
set -a; source .env; set +a
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8787
```

Generate a public webhook credential and any credentials required by your targets:

```bash
openssl rand -hex 32  # WEBHOOK_TOKEN: configured in Pebble
openssl rand -hex 32  # for a target's token_env variable, when required
```

Keep secrets in `.env` and reference their variable names with `auth.token_env`; avoid
placing tokens directly in YAML. Do not reuse the public Pebble token for a target.

## Target configuration

`targets.example.yaml` contains a complete example. Every enabled entry receives the
event. The adapter automatically loads `./targets.yaml`; `TARGETS_CONFIG_PATH` can point
to a different location. A minimal target looks like this:

```yaml
version: 1
targets:
  - name: receiver
    url: https://receiver.example.com/events
    auth:
      header: Authorization
      scheme: Bearer
      token_env: RECEIVER_TOKEN
    request:
      event_field: event
      audio_field: audio
      template: |
        {{ event | tojson }}
```

Templates use sandboxed Jinja with strict undefined values and must render valid JSON.
They can select, rename, nest, or omit fields. The `tojson` filter should be used when
inserting values so strings and nulls remain valid JSON.

Static `headers` may be added to a target. `request` can also set `audio_filename`,
`audio_mime_type`, and `include_audio`. Status lookup is optional:

```yaml
status:
  url_template: https://receiver.example.com/events/{id}
  id_field: id
```

`id_field` names the field in the target's POST response. Its value replaces `{id}` in
the status URL. Targets without `status` are fire-and-forget after acceptance.

Delivery results are saved independently under `metadata.deliveries`. The overall event
status is `forwarded` when all targets succeed, `partial` when some succeed, and `failed`
when none succeed.

## Personal agent gateway example

The first entry in `targets.example.yaml` configures the sibling
`personal-agent-gateway` as one compatible target:

```yaml
- name: personal-agent-gateway
  url: http://127.0.0.1:8790/v1/events
  auth:
    token_env: GATEWAY_TOKEN
  request:
    template: |
      {
        "event_id": {{ event.event_id | tojson }},
        "source": {{ event.source | tojson }},
        "sender_id": {{ event.sender_id | tojson }},
        "conversation_id": {{ event.conversation_id | tojson }},
        "content": {{ event.content | tojson }},
        "reply": {{ event.reply | tojson }},
        "metadata": {{ event.metadata | tojson }}
      }
  status:
    url_template: http://127.0.0.1:8790/v1/events/{id}
    id_field: id
```

When `TARGETS_CONFIG_PATH` is unset, the previous single-target `TARGET_*` environment
variables remain supported when no `./targets.yaml` exists. Existing `GATEWAY_URL`,
`GATEWAY_TOKEN`, and `GATEWAY_TIMEOUT_SECONDS` deployments also remain compatible;
`GATEWAY_URL` is treated as a base URL and expanded to `/v1/events`.

## Inspect an event

```bash
curl http://127.0.0.1:8787/events/EVENT_ID \
  -H "Authorization: Bearer $WEBHOOK_TOKEN"
```

The response always includes local delivery metadata. It also includes current status
and results for targets that configure status lookup.

## Test

```bash
uv run pytest
```

## License

[MIT License](LICENSE)
