# QQ / Hermes integration probe

Status: upstream contract inspected on 2026-09-14; deployment-specific probe pending.

No real QQ payload, Token, Secret, complete OpenID, or real message was captured during this
implementation. The workspace did not contain a Hermes checkout or a running QQBot deployment.

## Confirmed from current upstream source and documentation

- A native plugin uses `plugin.yaml`, root `__init__.py`, and `register(ctx)`.
- `pre_gateway_dispatch(event, gateway, session_store, **kwargs)` runs after Hermes's internal-event
  guard and before auth, pairing, and agent dispatch.
- It accepts `{"action":"skip","reason":"..."}` and `{"action":"allow"}`. Hermes normally falls
  through if a callback raises, so AutoQQ catches all callback errors and explicitly returns `skip`.
- The normalized event exposes `event.text` and `event.source.platform`, `user_id`, `chat_id`, and
  `chat_type`.
- The current QQBot adapter maps C2C `author.user_openid` to normalized `user_id`; group messages map
  `author.member_openid` to normalized `user_id` and `group_openid` to `chat_id`.
- `ctx.register_platform_handler("qqbot", factory)` invokes the factory at platform connect time with
  a read-only adapter handle; the documented send surface is `await adapter.send(chat_id, text)`.

These facts describe the current upstream `main` branch, not a verified installed deployment.

## Required checks in the locked deployment

- [ ] Record Hermes commit/version and Python version.
- [ ] Run `hermes plugins doctor` against this directory and record the sanitized result.
- [ ] Confirm QQBot C2C, group-at, guild, and direct-message event types actually enabled.
- [ ] Capture sanitized normalized events for private, group, and group-at messages.
- [ ] Confirm `source.user_id`, `source.chat_id`, `source.chat_type`, and `event.text` values.
- [ ] Confirm whether a non-bot mentioned member's OpenID is exposed through a stable plugin contract.
- [ ] Confirm AutoQQ's control hook executes in the expected order relative to deployment auth rules.
- [ ] Confirm command replies scheduled through the adapter are delivered after returning `skip`.
- [ ] Confirm platform-connect factory invocation and worker shutdown during gateway restart.
- [ ] Confirm proactive C2C send limits, maximum text size, error classification, and any idempotency key.
- [ ] Confirm sending a claimed notification does not create a normal Agent/LLM turn.

## Sanitization template

Store only a structural summary, for example:

```json
{
  "event_type": "C2C_MESSAGE_CREATE",
  "source": {
    "platform": "qqbot",
    "user_id": "id:<sha256-prefix>",
    "chat_id": "id:<sha256-prefix>",
    "chat_type": "dm"
  },
  "text_shape": {"length": 12, "starts_with_slash": false},
  "mention_fields_present": []
}
```

Never save raw authorization headers, QQ credentials, full OpenIDs, pairing codes, lease tokens,
attachments, or message contents.
