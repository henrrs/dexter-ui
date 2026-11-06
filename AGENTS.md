# Dexter engineering principles

- Keep Dexter local-first, loopback-only by default, and usable without corporate services.
- Keep domain logic independent from HTTP, SQLite, and vendor SDKs; never persist credentials or access tokens.
- Respect configured corporate registries and fail clearly instead of silently using public registries.
- Keep integrations read-only in F1 and isolate DEV from PROD.
- Define future module contracts without implementing future product features.
