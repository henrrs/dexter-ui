# ADR 0005: Modular monolith with narrow ports

- **Status:** Accepted
- **Context:** F1 must define future module boundaries without premature services or abstraction-heavy runtime machinery.
- **Decision:** Keep one process with domain, services, SQLite and vendor adapters. Domain contracts use small protocols for future discovery sources, snapshot publication, and Ask session persistence. Vendor SDKs load only inside adapters.
- **Alternatives:** Microservices (network, deployment, and availability costs without scale need); one all-in-one module (vendor details and future work become entangled); speculative plugin framework (contracts without current consumers).
- **Trade-offs:** Future modules will need concrete repositories/workers and migration design. Typed protocols define seams without promising an extension runtime.
- **Traceability:** Requirements 1/6; `domain/future_contracts.py`, `domain/operations.py`, and architecture tests.
