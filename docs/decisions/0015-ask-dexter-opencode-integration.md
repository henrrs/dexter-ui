# ADR 0015: Ask Dexter conversation runtime with OpenCode

## Status

Accepted — F4.4.

## Context

Dexter needs a persistent conversational panel that can use the active workspace context while remaining local-first. The installed OpenCode Server already supplies model discovery, native sessions/history, streaming events and cancellation. The current product does not have an approved specialized Dexter agent or domain tool interface. The initial chat must therefore be useful as a contextual conversational assistant without inheriting OpenCode's normal shell or file-edit capabilities.

The UI needs multiple conversations, per-conversation model and context preferences, streaming, reload recovery and stable tab references. Dexter must not duplicate transcripts or provider credentials in its SQLite store.

## Decision

Use OpenCode Server as the generation/session runtime behind the Dexter loopback API. Implement a small `OpenCodeAdapter`, `OpenCodeRuntime`, `AskDexterService` and SQLite metadata repository. OpenCode remains the transcript source of truth; Dexter stores session references, per-conversation preferences, idempotent turn state and the compact context snapshot used by each turn.

Start with configurable native agent `build`. For managed servers, use an isolated `--pure` process with private Dexter-owned working/log directories, loopback binding, disabled tools, deny-all permissions and disabled snapshots. Every session is also created with deny-all session permissions, and every prompt explicitly sends tools disabled. Do not trust a system prompt to enforce this policy. Never kill an OpenCode process Dexter did not start.

Trigger connection discovery when the persistent Ask panel requests status, but perform startup outside the API request so a slow local process cannot block the UI or API readiness. Coalesce concurrent requests into one bootstrap. Probe health with short per-request timeouts, retry transient failures for a bounded window, then validate the selected primary agent and connected model metadata before reporting ready. The UI must show a pending state and poll until success or a retryable terminal diagnosis. Reconnect cancels the pending cycle; only a Dexter-managed child is terminated on cancellation or shutdown.

Discover only connected provider models from OpenCode. Prefer Luna 6 by display metadata (the local model currently resolves as GPT-6 Luna) without hardcoding a provider/model ID. Persist model and supported variant by conversation; if the preferred model is missing, report it and require explicit selection.

Send an immutable, versioned workspace context snapshot with each user message through a system context block. The backend enforces active, pinned and selected-tab policies, exact IDs, bounds and redaction. Context content is labeled untrusted; raw rows/previews, credentials and tokens are not sent. SQL comments and literals are removed. Capture context at send time so later workspace changes do not rewrite an in-flight turn.

Stream native OpenCode SSE through Dexter and filter events by the target session. Persist UUID-idempotent turns and remote message baselines so reload/reconnect can recover a response that completed before EventSource connected without resubmitting a prompt. Stop aborts only the session belonging to the selected conversation.

## Alternatives considered

1. **Call model providers directly from React.** Rejected: exposes provider credentials to the browser, duplicates session/stream logic, and bypasses OpenCode's configured provider and auth behavior.
2. **Connect the browser directly to OpenCode.** Rejected: exposes OpenCode's port and auth surface to the host browser and makes cross-origin/WSL behavior more fragile.
3. **Persist the entire transcript in Dexter SQLite.** Rejected: duplicates native history and creates divergent recovery sources. Dexter persists references and turn metadata only.
4. **Create a custom Dexter agent/tool gateway in F4.4.** Rejected: specialized tools and engineering workflows are explicitly later scope; adding them now expands authority before their contracts exist.
5. **Use a mock or fabricated chat response when OpenCode is missing.** Rejected: the panel must show connection failure and preserve conversations, never imply a model answered.
6. **Rely only on a system instruction to prevent tool execution.** Rejected: prompts are not a permission boundary. The agent, session and per-prompt tool policy must all be restrictive.

## Consequences

### Benefits

- OpenCode owns sessions, provider auth, native transcript and actual generation events.
- The browser communicates only with Dexter, whose API remains loopback-restricted.
- Conversation references, model/context preferences and turn recovery survive Dexter restarts without transcript duplication.
- Provider/model and agent selection are discovered from local OpenCode metadata.
- Workspace context can evolve independently through typed providers without creating domain tools.
- A future agent can replace `build` through the configuration boundary while retaining chat, persistence, streaming and context contracts.

### Trade-offs and limits

- A disconnected or removed OpenCode provider/agent prevents new turns; Dexter does not silently fall back.
- Provider services receive user text and the selected compact context. Dexter can redact common secrets/literals but cannot determine whether every metadata value is safe; users should inspect the visible context summary.
- OpenCode must be installed and authenticated locally. Dexter does not install it or manage provider credentials.
- The local workspace hash separates local data homes, not authenticated multi-user accounts. The current app is a single-user local runtime.
- Native-session deletion may be unconfirmed if OpenCode is offline; Dexter still removes its local reference and reports the remote deletion state.
- Context recovery requires exact tab identity; closed pinned/selected references remain unavailable until reopened.

## Verification

`backend/tests/test_ask_dexter.py` covers adapter permission payloads, connected-model metadata filtering, session separation, turn idempotency/state, active/pinned/selected context, SQL/token redaction, session-filtered streaming, cancellation/deletion, endpoint policy and managed agent configuration. Browser navigation and workflow tests run through Firefox/geckodriver. The F4.4 execution record in [testing.md](../testing.md) records the real installed OpenCode version/model and the live UI integration result separately from mock-backed tests.
