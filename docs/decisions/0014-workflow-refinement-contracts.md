# ADR 0014: F4 workflow refinements and safe schema/generation contracts

## Status

Accepted for the F4.1–F4.3 UX refinement.

## Context

Compare Tables, Schema Evolution, and Prepare DEV share schema metadata but have different execution boundaries. A contextual comparison must keep its originating table fixed. Synthetic generation must expose only generators that are actually implemented for a selected type, and publication must not infer a destination from name conventions or overwrite user schema edits while switching strategies. A preview can also become invalid while its request is in flight.

## Decision

- Keep the contextual Compare Tables reference fixed. Only the comparison-side object is replaceable; no swap action silently changes the originating workbench context.
- Use one backend generator registry as the source for API/UI options and domain validation. Type-based defaults use an implemented compatible generator; unsupported types remain visible but cannot proceed to generation.
- Resolve a suggested DEV catalog/schema through a unique Unified Discover mapping. Verify table existence/type against DEV. Rank External Locations using catalog/schema path evidence; automatic selection requires a unique strong match and verified effective grants. Otherwise require a manual choice and normal review.
- Determine Create/Isolated versus existing-table strategies from observed DEV target existence. Append/Replace require the observed DEV table schema, and changing schema sources must be an explicit action that warns before discarding custom edits.
- Bind generated previews to the exact schema/rules/count/locale/seed digest and discard late responses after edits or a newer request.
- Parse a restricted recursive Databricks type grammar. Preserve original declarations and struct field names. Protocol-feature types require evidence that the table already supports them; Dexter never enables a feature to make a proposal pass.
- Add PROD-only fields to the Schema Evolution draft as nullable, idempotently. Keep constraints/provider properties for review and block additions whose special semantics do not have an explicit migration plan.
- Review is a navigation stage even when the draft is unchanged; the plan then reports no operation and cannot be applied.
- Give Ask Dexter only active table/workflow stage and schema/difference counts. Do not include row samples or generated values.

## Consequences

### Stabilization on 2026-11-08

Navigation to Schema and Generation Rules does not require remote publication checks or a warehouse. Append/Replace select the observed physical DEV schema and storage because those strategies preserve the table; editable schema/rule snapshots remain available when returning to a creation strategy. Switching PROD/DEV bases restores the respective draft instead of discarding compatible edits. Open workflow tabs retain their own React sessions and only the active one emits context.

Draft columns carry `origin` (`dev`, `prod`, `manual`) through SQLite serialization, undo, removal/restoration and review. Modified/removed presentation uses the draft operation and its baseline; canonical Schema Diff does not infer editing provenance. Autosave uses the physical DEV node ID; persisted client-added columns may be omitted by Undo, while baseline columns must remain represented.

Bound workflow requests, request cancellation and snapshot checks reject obsolete responses. Structured verification distinguishes confirmed 403/404 from network failures and unclassified metadata errors. An unverified review can be inspected but never executed. A repeated accepted start with the same review ID reuses the saved synthetic run and operation, including after a lost HTTP response; execution still rechecks a fresh review and all existing safety gates before its first acceptance.

These checks reduce invalid plans before review while keeping final publication and schema-plan validation on the backend. They do not certify a live Databricks workspace; API permission checks and destination suggestions still require the configured local DEV identity at runtime. Types unsupported by the current generator or table protocol remain unavailable rather than being coerced.

## Alternatives rejected

- Guessing DEV names from `_dev` suffixes or discarding edits when selecting a strategy: rejected because logical/physical mappings differ and edits must be preserved.
- Choosing the first External Location or any location with one matching word: rejected because it is neither confident path evidence nor proof of effective permission.
- Showing every generator for every SQL type or using a sequence fallback: rejected because options would advertise invalid data plans.
- Letting asynchronous preview responses replace the latest user draft: rejected because the displayed values could describe a different plan.
