# ADR 0002: Host-first loopback runtime

- **Status:** Accepted
- **Context:** F1 must start offline, reuse local CLIs, avoid daemon requirements, and not expose a local API to the network.
- **Decision:** Run FastAPI and Vite on loopback, persist in XDG directories, reject non-loopback bind settings/Host headers, and allow only local UI origins. No long-running service is installed; managed dev processes have explicit start/stop commands.
- **Alternatives:** Docker-first app (adds engine, image and credential-mount requirements); LAN listener (unnecessary exposure); auto-installed system daemon (harder lifecycle and permissions).
- **Trade-offs:** Host-managed dependencies need packaging later. Loopback plus origin/Host checks constrain browser access; local processes owned by the same user remain trusted.
- **Containerization:** Future workers may be isolated, but host credentials stay outside images. Prefer a scoped host-side credential broker; any development profile mount is explicit, read-only, selected-profile-only, and kept separate by environment.
- **Traceability:** Requirements 1/5; `Settings`, `LocalOriginGuard`, `scripts/dev.sh`, `scripts/stop.sh`, and offline doctor checks.
