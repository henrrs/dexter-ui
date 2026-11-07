# ADR 0009: Discover Explorer, workbench, and availability states

- **Status:** Accepted for the F3 Discover correction.
- **Context:** The Discover tree and selected asset details competed for the center workspace, while the platform shell showed an unrelated Workspace/Sources Explorer outside Discover. Logical environment availability was also represented as synthetic child variants, mixing provider data and reconciliation control state.

## Decision

- Home and Connections do not render the old Workspace Explorer. Discover owns one collapsible and resizable catalog Explorer beside its table workbench; the workbench remains neutral until a table or view is selected.
- Selecting a table opens a workbench tab. Each tab retains its chosen DEV, PROD, or local context when the user switches to another table. The right context panel tracks the active table and environment without adding chat-session behavior.
- The logical node payload contains real physical variants only. Confirmed absence is a separate per-environment availability map carried with the opaque node ID and inherited during lazy expansion. Provider errors remain explicit and do not confirm absence. Since the local dataset index is complete, a missing local variant is confirmed absent.
- L/D/P are always visible and mean existence. Read access is separate; metadata-only Purview entries display a lock and cannot use preview or materialization actions.
- Unified Catalog account discovery still uses Azure CLI and ARM, but its Data Assets calls use the documented Unified Catalog service root. The ARM account `catalog` endpoint is not treated as the Unified Catalog host. There is no fallback to Atlas or a privileged identity.
- Make Local and valid local queries remain operational. Synthetic data remains a planned entry point. At F3 time, table comparison was disabled; F4.1 adds read-only schema comparison while row-level data comparison remains unimplemented. Lineage says when no provider supplies lineage.

## Consequences

Tree navigation, API access, and detail work now have distinct UI roles. Control state cannot be mistaken for provider nodes during child expansion. DEV and PROD partial results continue to render with unknown or confirmed-absent state kept distinct. Purview behavior remains subject to tenant-level delegated authorization and requires a live account check for final confirmation.
