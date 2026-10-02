# Feishu Long Connection and Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Feishu webhook with a proxy-aware long connection while preserving mention filtering, durable idempotency, scheduled delivery, and localhost-only administration.

**Architecture:** The official `lark-oapi` client owns Feishu framing and dispatch. A single isolated compatibility module supplies proxy-aware endpoint discovery and WebSocket dialing; SDK events are converted to ordinary dictionaries and persisted through the existing SQLite event/outbox transaction. The runtime supervises long connection, admin, and outbox components and exposes a sanitized in-memory connection snapshot to the admin API.

**Tech Stack:** Python 3.8+, `lark-oapi==1.7.3`, `websockets==13.1`, `python-socks[asyncio]==2.7.2`, SQLite, `unittest`

**Spec:** `docs/superpowers/specs/2026-10-02-feishu-long-connection-proxy-design.md`

## Global Constraints

- Long connection is the only Feishu event input; do not retain a webhook mode or fallback switch.
- Only group text messages with a structural mention of the current bot enter the subscription queue.
- Keep the existing SQLite event/outbox transaction and `event_id` / `message_id` idempotency guarantees.
- Proxy precedence is exactly `FEISHU_WS_PROXY`, `HTTPS_PROXY`, `https_proxy`, `ALL_PROXY`, `all_proxy`.
- Supported proxy schemes are exactly `http`, `https`, `socks5`, and `socks5h`.
- `FEISHU_WS_PROXY` affects endpoint discovery and WebSocket connection only; REST sends continue using standard proxy variables.
- Admin remains bound to `127.0.0.1`; no Feishu callback port remains.
- Never log or return App Secret, proxy credentials, URL query strings, or raw event text.
- Preserve Python 3.8 support; pin `lark-oapi==1.7.3`, `websockets==13.1`, and `python-socks[asyncio]==2.7.2`, and do not require automatic proxy support added only by newer Python/websockets combinations.
- All automated tests use fakes and local files; no test may require Feishu or internet access.

## Review Focus

- A proxy URL containing percent-encoded credentials must validate but every diagnostic must redact both decoded and encoded credentials; Task 1 pins this.
- An SDK event with partially missing nested attributes must be acknowledged as ignored, not crash the connection or create a row; Task 2 pins this.
- A database commit failure must escape the SDK handler so Feishu can redeliver, while malformed or irrelevant events are acknowledged; Task 2 pins this.
- Stop requested during connection establishment or reconnect backoff must return promptly without starting another attempt; Task 3 pins this.
- A temporary connection error must not stop admin/outbox, while an authentication or compatibility error must fail the runtime without leaking its URL; Task 4 pins this.

---

### Task 1: Secret-safe Feishu and proxy configuration

**Files:**
- Create: `src/hotnews/feishu/proxy.py`
- Modify: `src/hotnews/config.py`
- Modify: `tests/unit/test_config_domain.py`
- Create: `tests/unit/test_feishu_proxy.py`

**Interfaces:**
- Produces: `FeishuConfig(app_id: str, app_secret: str, ws_proxy: Optional[str], bot_open_id: Optional[str])` with secret-bearing fields excluded from `repr`.
- Produces: `resolve_ws_proxy(environ: Mapping[str, str]) -> Optional[str]`.
- Produces: `redact_sensitive(value: object) -> str` for safe logs, CLI errors, and status snapshots.

- [ ] **Step 1: Write failing configuration tests**

Add tests asserting that only `FEISHU_APP_ID` and `FEISHU_APP_SECRET` are required, callback secrets are ignored, proxy precedence is exact, whitespace-only values fall through, and `repr(FeishuConfig)` contains neither the App Secret nor proxy password.

- [ ] **Step 2: Write failing proxy validation and redaction tests**

Cover all four accepted schemes, missing hosts, invalid ports, fragments, URL query redaction, plain and percent-encoded credentials, and exception objects containing temporary `wss://...?...` URLs. Assert no secret substring survives `redact_sensitive`.

- [ ] **Step 3: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.unit.test_config_domain tests.unit.test_feishu_proxy -v`  
Expected: FAIL because the new proxy API and credential shape do not exist.

- [ ] **Step 4: Implement the configuration boundary**

In `proxy.py`, implement the exact precedence and URL validation without performing network I/O. In `config.py`, remove callback credentials, add `ws_proxy`, use `dataclasses.field(repr=False)` for secrets, and have `load_feishu_config(environ)` call `resolve_ws_proxy`.

- [ ] **Step 5: Run the focused tests and commit**

Run: `python3 -m unittest tests.unit.test_config_domain tests.unit.test_feishu_proxy -v`  
Expected: PASS.

```bash
git add src/hotnews/config.py src/hotnews/feishu/proxy.py tests/unit/test_config_domain.py tests/unit/test_feishu_proxy.py
git commit -m "feat: add proxy-aware Feishu configuration"
```

### Task 2: Long-connection event normalization and durable intake

**Files:**
- Modify: `src/hotnews/feishu/events.py`
- Create: `src/hotnews/feishu/intake.py`
- Create: `tests/unit/test_feishu_intake.py`
- Modify: `tests/unit/test_feishu.py`

**Interfaces:**
- Consumes: `FeishuConfig.bot_open_id` from Task 1.
- Produces: `normalize_event(payload: Mapping[str, object], bot_open_id: str, received_at: Optional[datetime] = None) -> Optional[NormalizedEvent]`.
- Produces: `sdk_event_payload(event: object) -> Mapping[str, object]`, the only SDK-object-to-dictionary adapter outside the compatibility module.
- Produces: `EventIntake(database: Database, bot_open_id: str).handle(payload: Mapping[str, object]) -> bool`; `True` means persisted or safely ignored, while storage errors propagate.

- [ ] **Step 1: Replace callback-centric normalization tests with SDK payload tests**

Assert group/text/user/current-bot-mention acceptance; non-group, non-text, bot/self sender, wrong mention, empty text, malformed mentions, and partially missing nested objects are ignored. Assert the bot placeholder is removed only when its complete mention key matches.

- [ ] **Step 2: Add failing intake transaction tests**

Assert one accepted event atomically inserts the existing inbound row and fixed acknowledgement outbox row; concurrent or repeated `event_id` / `message_id` inserts produce one of each; ignored events produce none; a forced commit failure raises and leaves neither row.

- [ ] **Step 3: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.unit.test_feishu tests.unit.test_feishu_intake -v`  
Expected: FAIL because normalization still requires webhook authentication and `EventIntake` is absent.

- [ ] **Step 4: Implement pure normalization and transactional intake**

Remove signature, token, encryption, challenge, and HTTP-body decoding from `events.py`. Preserve the current `NormalizedEvent` fields and raw text/mentions storage shape. Move `ACKNOWLEDGEMENT` to `intake.py`; catch structural validation inside `handle` as an acknowledged ignore, but do not catch SQLite, filesystem, or commit errors.

- [ ] **Step 5: Run the focused tests and commit**

Run: `python3 -m unittest tests.unit.test_feishu tests.unit.test_feishu_intake -v`  
Expected: PASS.

```bash
git add src/hotnews/feishu/events.py src/hotnews/feishu/intake.py tests/unit/test_feishu.py tests/unit/test_feishu_intake.py
git commit -m "feat: add durable long-connection event intake"
```

### Task 3: Isolated SDK compatibility layer and connection lifecycle

**Files:**
- Create: `src/hotnews/feishu/connection.py`
- Create: `src/hotnews/feishu/ws_compat.py`
- Modify: `src/hotnews/feishu/__init__.py`
- Modify: `pyproject.toml`
- Create: `tests/unit/test_feishu_connection.py`
- Create: `tests/unit/test_ws_compat.py`

**Interfaces:**
- Consumes: `EventIntake.handle(payload) -> bool`, `FeishuConfig`, and `redact_sensitive` from Tasks 1–2.
- Produces: immutable `ConnectionSnapshot(state, connected_at, last_event_at, reconnect_attempts, last_error)`.
- Produces: thread-safe `ConnectionStatus.snapshot() -> ConnectionSnapshot` plus `starting()`, `connected()`, `event_received()`, `reconnecting(error)`, `stopped()`, and `fatal(error)` transitions.
- Produces: `FeishuLongConnection(config, intake, status, connector=...)` with `run(stop_event: threading.Event) -> None` and `check(timeout_seconds: float) -> ConnectionSnapshot`.
- Produces: `build_sdk_connector(config: FeishuConfig, event_callback: Callable[[object], None]) -> SDKConnector`; this is the only function allowed to access `lark_oapi` private members.

- [ ] **Step 1: Add failing lifecycle and sanitization tests**

Using a fake connector, assert `starting → connected`, event timestamps, transient failure `reconnecting → connected`, capped jittered backoff, fatal authentication/compatibility state, and sanitized errors. Assert a stop during connect or backoff returns within one test tick and performs no later attempt.

- [ ] **Step 2: Add failing SDK compatibility tests**

Use fake SDK modules to pin the supported version/signature contract, proxy-aware endpoint discovery, HTTP/HTTPS and SOCKS dialing arguments, per-client rather than process-global injection, callback registration for `im.message.receive_v1`, exception propagation from intake, and a close operation that unblocks the SDK loop.

- [ ] **Step 3: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.unit.test_feishu_connection tests.unit.test_ws_compat -v`  
Expected: FAIL because connection modules do not exist.

- [ ] **Step 4: Pin dependencies and implement the compatibility boundary**

Add the exact Python-3.8-compatible pins `lark-oapi==1.7.3`, `websockets==13.1`, and `python-socks[asyncio]==2.7.2`. Implement endpoint discovery with an instance-scoped proxy argument and create a proxy-connected socket for WebSocket TLS, rather than mutating `HTTP_PROXY`, monkeypatching a module globally, or depending on automatic proxy support unavailable on Python 3.8. Reject an unexpected SDK version/signature before network access.

- [ ] **Step 5: Implement lifecycle supervision**

Classify credential/SDK compatibility failures as fatal; retry transport failures with exponential backoff capped at 60 seconds and bounded jitter. The SDK callback calls `sdk_event_payload`, then `EventIntake.handle`; it records `last_event_at` only after an acknowledged result and lets storage exceptions escape.

- [ ] **Step 6: Run the focused tests and commit**

Run: `python3 -m unittest tests.unit.test_feishu_connection tests.unit.test_ws_compat -v`  
Expected: PASS with no network access.

```bash
git add pyproject.toml src/hotnews/feishu/__init__.py src/hotnews/feishu/connection.py src/hotnews/feishu/ws_compat.py tests/unit/test_feishu_connection.py tests/unit/test_ws_compat.py
git commit -m "feat: add proxy-aware Feishu long connection"
```

### Task 4: Runtime integration without a callback server

**Files:**
- Modify: `src/hotnews/runtime.py`
- Modify: `src/hotnews/config.py`
- Modify: `config.example.json`
- Modify: `tests/integration/test_runtime.py`

**Interfaces:**
- Consumes: `FeishuLongConnection.run`, `ConnectionStatus`, `EventIntake`, and Task 1 configuration.
- Produces: `run_service(config: AppConfig, stop_event: threading.Event) -> None` supervising services named `connection`, `admin`, and `outbox`.

- [ ] **Step 1: Write failing runtime tests**

Assert migration and bot identity resolution precede connection startup; only connection/admin/outbox start; transient reconnect state leaves peers alive; connection fatal failure stops peers and raises a sanitized `RuntimeServiceError`; early stop opens no network connection; and all started services observe stop and join.

- [ ] **Step 2: Write failing callback-configuration removal tests**

Assert `AppConfig` has no `callback`, `load_config` rejects an obsolete `callback` section with a migration message, admin alone owns its port, and the example JSON contains no callback section.

- [ ] **Step 3: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.integration.test_runtime tests.unit.test_config_domain -v`  
Expected: FAIL because runtime still starts `serve_gateway` and config still exposes callback.

- [ ] **Step 4: Integrate the new services**

Resolve bot identity using the existing REST client, construct one `ConnectionStatus` and `EventIntake`, pass the status provider to admin, and replace the callback thread with `FeishuLongConnection.run`. A connection object's internal transient retry is not a service exit; an actual unexpected return or fatal exception retains the existing peer-stop semantics.

- [ ] **Step 5: Run the focused tests and commit**

Run: `python3 -m unittest tests.integration.test_runtime tests.unit.test_config_domain -v`  
Expected: PASS.

```bash
git add src/hotnews/runtime.py src/hotnews/config.py config.example.json tests/integration/test_runtime.py tests/unit/test_config_domain.py
git commit -m "feat: run HotNews over Feishu long connection"
```

### Task 5: Local admin connection status

**Files:**
- Modify: `src/hotnews/admin/app.py`
- Modify: `src/hotnews/admin/server.py`
- Modify: `src/hotnews/admin/static/index.html`
- Modify: `src/hotnews/admin/static/app.js`
- Modify: `src/hotnews/admin/static/styles.css`
- Modify: `tests/unit/test_admin_api.py`
- Modify: `tests/integration/test_admin_page.py`

**Interfaces:**
- Consumes: `Callable[[], ConnectionSnapshot]` from Task 3.
- Produces: `GET /api/connection` response with `state`, ISO timestamps, `reconnect_attempts`, and sanitized `last_error`; it contains no proxy URL or credentials.

- [ ] **Step 1: Add failing admin API tests**

Assert localhost/Host protections apply to `/api/connection`, method is GET-only, timestamps are UTC strings, all five states serialize, and a malicious status error cannot expose credentials or a query string.

- [ ] **Step 2: Add failing browser behavior tests**

Assert the page displays connected/reconnecting/fatal state and last safe error, refreshes the status without changing subscription rows, and renders text through DOM text nodes rather than HTML.

- [ ] **Step 3: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.unit.test_admin_api tests.integration.test_admin_page -v`  
Expected: FAIL because the status endpoint and UI do not exist.

- [ ] **Step 4: Implement status injection, route, and UI**

Add an optional status provider to `AdminApplication` and `serve_admin`; default to a stopped snapshot for isolated tests. Poll `/api/connection` alongside existing refreshes and add a compact status region without weakening CSP or localhost-only binding.

- [ ] **Step 5: Run the focused tests and commit**

Run: `python3 -m unittest tests.unit.test_admin_api tests.integration.test_admin_page -v`  
Expected: PASS.

```bash
git add src/hotnews/admin tests/unit/test_admin_api.py tests/integration/test_admin_page.py
git commit -m "feat: show Feishu connection status locally"
```

### Task 6: Read-only Feishu diagnostics

**Files:**
- Modify: `src/hotnews/cli.py`
- Create: `src/hotnews/feishu/diagnostics.py`
- Create: `tests/integration/test_feishu_check.py`
- Modify: `tests/integration/test_dry_run.py`

**Interfaces:**
- Consumes: Task 1 config/redaction, existing `FeishuClient.get_bot_open_id`, and `FeishuLongConnection.check(timeout_seconds)`.
- Produces: `hotnews check-feishu` with exit `0` and safe JSON on success, exit `1` and a fixed safe error on network/auth failure, exit `2` on local validation failure.

- [ ] **Step 1: Add failing CLI contract tests**

Assert parser includes `{serve,check-feishu,agent,dry-run}`; success reports proxy source/sanitized endpoint, bot identity presence, and connected state; timeout, proxy refusal, auth failure, and SDK incompatibility return the specified codes without echoing injected secrets.

- [ ] **Step 2: Add failing no-side-effect test**

Point the command at a temporary migrated database and fake clients, then assert subscription, inbound-event, outbox, delivery, and run tables are unchanged and the message-send methods were never called.

- [ ] **Step 3: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.integration.test_feishu_check tests.integration.test_dry_run -v`  
Expected: FAIL because `check-feishu` is not registered.

- [ ] **Step 4: Implement the bounded diagnostic flow**

Load credentials, validate proxy, resolve bot identity, perform one bounded long-connection handshake, close it, and emit only a small safe result. Do not migrate or open the application database and do not construct an outbox worker.

- [ ] **Step 5: Run the focused tests and commit**

Run: `python3 -m unittest tests.integration.test_feishu_check tests.integration.test_dry_run -v`  
Expected: PASS.

```bash
git add src/hotnews/cli.py src/hotnews/feishu/diagnostics.py tests/integration/test_feishu_check.py tests/integration/test_dry_run.py
git commit -m "feat: add safe Feishu connection diagnostics"
```

### Task 7: Remove webhook artifacts and update deployment guidance

**Files:**
- Delete: `src/hotnews/feishu/gateway.py`
- Delete: `src/hotnews/feishu/crypto.py`
- Delete: `tests/integration/test_gateway.py`
- Modify: `src/hotnews/feishu/__init__.py`
- Modify: `tests/test_hotnews.py`
- Modify: `tests/unit/test_deployment_contract.py`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: long-connection intake/runtime/check command from Tasks 2–6.
- Produces: deployment with no webhook listener or published callback port and documented Feishu long-connection setup.

- [ ] **Step 1: Rewrite the end-to-end acceptance test around long connection**

Inject a fake SDK event through the long-connection callback twice, process the queued command/research flow, and assert one inbound event, one subscription, one acknowledgement, and one confirmed digest. Include an unmentioned event and assert it creates nothing.

- [ ] **Step 2: Change deployment contract tests to fail on webhook artifacts**

Assert Dockerfile has no `EXPOSE`, compose has no `ports`, callback secrets are absent, standard proxy variables and optional `FEISHU_WS_PROXY` pass through, and runtime source plus operator-facing README contain no `/callbacks/feishu`, URL verification, Encrypt Key, tunnel, or callback-port instructions. Historical design documents are excluded from this assertion.

- [ ] **Step 3: Run acceptance and deployment tests and verify failure**

Run: `python3 -m unittest tests.test_hotnews tests.unit.test_deployment_contract -v`  
Expected: FAIL while webhook code and port mappings still exist.

- [ ] **Step 4: Delete callback-only code and update exports**

Remove gateway, crypto, callback tests, callback imports, and callback package exports. Retain generic HTTP utilities still used by localhost admin and the Feishu REST client.

- [ ] **Step 5: Update container and operator documentation**

Document installing/running, `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, proxy precedence, `hotnews check-feishu`, selecting “使用长连接接收事件”, subscribing to `im.message.receive_v1`, required bot permissions, adding the bot to a group, localhost admin access, and proxy recovery. Do not publish any container port; describe native execution as the recommended personal deployment because container-local `127.0.0.1` admin is intentionally not exposed.

- [ ] **Step 6: Run acceptance, static checks, and full suite**

Run: `python3 -m unittest tests.test_hotnews tests.unit.test_deployment_contract -v`  
Expected: PASS.

Run: `python3 -m unittest discover -s tests -v`  
Expected: all tests PASS with no network access.

Run: `python3 -m compileall -q src tests && git diff --check`  
Expected: exit 0.

- [ ] **Step 7: Run optional real read-only diagnostic**

If real credentials are present, run `hotnews --config config.json check-feishu` with the current proxy environment and verify a connected result. If credentials are absent, record the check as skipped; do not invent or request secrets in logs.

- [ ] **Step 8: Commit the migration**

```bash
git add -A
git commit -m "docs: migrate setup to Feishu long connection"
```

### Task 8: Final review and remote update

**Files:**
- Review: all files changed since `1718a99`

**Interfaces:**
- Consumes: completed Tasks 1–7.
- Produces: reviewed feature branch ready for the user's machine.

- [ ] **Step 1: Verify repository and dependency artifacts**

Run: `git status --short`, inspect all diffs since `1718a99`, and build the wheel if the local build backend is available. Confirm generated caches, local database files, `.env`, and credentials are not tracked.

- [ ] **Step 2: Run the required implementation-method review**

For subagent-driven execution, request a whole-branch review after every task-level review has passed. For native execution, request one fresh whole-branch review now. Fix findings with focused regression tests and rerun the affected tests plus the full suite.

- [ ] **Step 3: Push the feature branch**

```bash
git push origin feature/feishu-hotnews-agent
```

- [ ] **Step 4: Report operator actions**

Give the user the exact dependency install, environment setup, `check-feishu`, service start, Feishu console selection, event/permission configuration, and localhost admin URL. Explicitly state whether the real diagnostic ran or was skipped.
