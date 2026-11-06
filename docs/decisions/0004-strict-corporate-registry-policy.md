# ADR 0004: Strict corporate registry policy

- **Status:** Accepted
- **Context:** Corporate Nexus should be used where configured; public registry fallback can violate supply-chain, proxy, certificate, or network policies.
- **Decision:** Dependency installation runs through a guard that discovers explicit npm and pip/uv registry configuration, chooses one registry, rejects missing configuration and multiple Python indexes, and permits public npm/PyPI only with both an explicit URL and `DEXTER_ALLOW_PUBLIC_REGISTRY=1`. npm lock hosts are rewritten to the selected registry. Host proxy and certificate configuration remains in effect.
- **Alternatives:** Trust package-manager defaults (silently uses public PyPI/npm when no company configuration exists); `extra-index-url` (permits cross-index package confusion); hard-code one Nexus hostname (would not match each company/tenant).
- **Trade-offs:** A first install requires a configured Nexus. Users must opt in explicitly to public registries. The dependency doctor is local-only and does not probe whether the registry is reachable.
- **Traceability:** Requirement 2; tests exercise missing Nexus, explicit corporate registry, unauthorized/authorized public registry, and secondary-index rejection.
