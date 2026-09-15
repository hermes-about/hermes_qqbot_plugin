# AutoQQ Hermes Business Plugin

This repository is an independent Python project that implements the deterministic AutoQQ layer
between Hermes QQBot messages and the AutoQQ EventServer.

Implemented in `0.1.0`:

- native Hermes `plugin.yaml` plus `register(ctx)` entry point;
- `pre_gateway_dispatch` authorization before Hermes auth/pairing/agent dispatch;
- independent `chat` and `command` permission gates with blocked-account precedence;
- explicit `public`, `authorized`, and `admin` command policies;
- deterministic user/admin commands that always return `skip` and never reach the LLM;
- short-lived in-process permission cache with mutation invalidation;
- authenticated, bounded, no-redirect EventServer HTTP client;
- generic delivery claim, QQBot adapter send, lease `ack`/`fail`, backoff, and safe shutdown;
- masked OpenID audit fields and Secret/token redaction helpers.

The Plugin has no database dependency and does not import EventServer models or Warframe providers.

## Layout

```text
hermes_qqbot_plugin/
├── plugin.yaml
├── __init__.py
├── config/commands.yaml
├── autoqq_business_plugin/
├── tests/
└── docs/qq-payload-probe.md
```

`config/commands.yaml` intentionally contains JSON syntax. JSON is a valid YAML subset and lets the
Plugin parse its signed-off policy with the Python standard library instead of adding a YAML runtime
dependency.

## Configuration

Copy `.env.example` into the Hermes deployment's secret/configuration mechanism. Do not commit the
real `INTERNAL_API_TOKEN`, OpenIDs, or QQ credentials. The token must be at least 32 characters.
Delivery polling also requires a stable, per-instance `DELIVERY_WORKER_ID`.

The implementation always denies on missing users, blocked users, invalid configuration, malformed
EventServer responses, timeouts, and internal processing errors. There is no global allow default.

## Install from GitHub

Python package installation is the recommended form. Run the command in the same Python environment
as Hermes so that Hermes can discover the `hermes_agent.plugins` entry point:

```bash
python -m pip install \
  "autoqq-business-plugin @ git+https://github.com/fkYang/hermes_qqbot_plugin.git@main"
hermes plugins enable autoqq-business
```

For a reproducible deployment, replace `main` with a release tag or full commit SHA. To update an
existing Git installation:

```bash
python -m pip install --upgrade --force-reinstall \
  "autoqq-business-plugin @ git+https://github.com/fkYang/hermes_qqbot_plugin.git@main"
```

Hermes also supports a native directory plugin. This keeps `plugin.yaml` and the root `register(ctx)`
entry point visible to its directory loader:

```bash
git clone https://github.com/fkYang/hermes_qqbot_plugin.git \
  ~/.hermes/plugins/autoqq-business
hermes plugins doctor ~/.hermes/plugins/autoqq-business
hermes plugins enable autoqq-business
```

Choose one loading form for a deployment; do not install the entry-point package and clone the same
plugin into Hermes' directory at the same time. For a directory install, ensure `httpx>=0.28,<1` is
available in the Hermes Python environment.

Before enabling against real QQ, configure the required environment variables, complete the checks
in [`docs/qq-payload-probe.md`](docs/qq-payload-probe.md), and run the Hermes plugin doctor for a
directory install. Directory installation, doctor, enablement, gateway loading and proactive QQBot
C2C delivery were verified against Hermes Agent `v0.21.2` on 2026-09-15. See the standalone
[`操作指南.md`](操作指南.md) for the EventServer/Hermes deployment sequence.

Current upstream Hermes also provides `register_platform_handler`. AutoQQ uses it to bind the QQBot
adapter and start the delivery worker when QQBot connects. `pre_gateway_dispatch` repeats that binding
as a compatibility fallback. The worker sends only through the read-only Hermes adapter handle; it
does not hold QQ credentials or call Tencent APIs directly.

## Commands

| Command | Policy | Behavior |
| --- | --- | --- |
| `/help`, `/events`, `/whoami` | public | Still checks account status; blocked users are denied |
| `/bind`, `/unbind`, `/bindings` | authorized | Requires active account and `command=true` |
| `/grant`, `/revoke`, `/admin`, `/unadmin`, `/permissions`, `/userlist` | admin | Also requires `role=admin` |

`/grant` and `/revoke` require an explicit `chat`, `command`, or `all` dimension. Eight-character
EventServer pairing codes are accepted by `/grant`. `@nickname` is rejected until a real QQ payload
probe demonstrates a stable mentioned-member OpenID; no nickname lookup is attempted.

The current EventServer v1 has no user-list endpoint, so `/userlist` returns a deterministic
"not available" response and skips the LLM. Adding that endpoint requires a versioned cross-project
contract change and matching tests.

## Offline verification

The following commands were verified using the already-present EventServer Python environment; no
dependencies were installed and no network, database, Hermes, QQ, or Warframe service was contacted:

```bash
../eventserver/.venv/bin/python -m pytest
../eventserver/.venv/bin/ruff check .
../eventserver/.venv/bin/ruff format --check .
```

The suite uses `httpx.MockTransport` and fake Hermes adapters. A separate authorized test on
2026-09-15 also verified the real `claim -> Hermes QQBot send -> ack` path; private/group payload
coverage and platform limits must still be rechecked when the Hermes/QQBot version changes.

## Delivery semantics

Delivery is at least once. The worker validates the generic target and message, sends through the
Hermes QQBot adapter, and then writes `ack` or `fail` with the current lease token. It never retries a
message locally; EventServer owns retry timing and the terminal `dead` state. If sending succeeds but
the process dies before `ack`, the notification may be repeated after lease expiry. QQBot idempotency
support still needs environment verification.
