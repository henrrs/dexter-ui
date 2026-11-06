# Databricks workspace setup

Run this command from the Dexter repository:

```sh
make databricks-setup
```

The setup configures two workspaces, one for DEV and one for PRD. It reuses the active Azure CLI login for both. If there is no active login, it offers to run `az login`; Azure CLI retains its credentials in its own local credential store. Dexter never asks for or saves a Databricks token.

Enter each HTTPS workspace URL when prompted. Dexter checks the current identity against both workspaces before writing either profile. If either validation fails, neither profile is written. The managed profiles are stored separately from the standard `~/.databrickscfg` file at `${XDG_CONFIG_HOME:-$HOME/.config}/dexter/databricks/databrickscfg`. This keeps the profiles private to Dexter while the Azure CLI login remains shared by local tools that use Azure CLI authentication.

The managed profile names are `DEXTER-DEV` and `DEXTER-PRD`; both specify `auth_type = azure-cli` and share the active Azure tenant. This flow supports Azure Databricks workspaces that accept that identity; it does not configure AWS/GCP or another authentication provider. Other Databricks profiles are left untouched. If a workspace changes, setup refuses to overwrite the existing Dexter profile by default. Use `make databricks-setup RESET=1` to replace Dexter-owned profiles; the existing managed config is backed up first. `make databricks-setup OUTPUT=json` emits a JSON result while keeping interactive prompts on stderr.

The wizard validates authentication and current-user access for both workspaces. It does not start compute or modify workspace resources. After setup, Dexter loads each profile into its matching DEV or PRD connection; it never falls back from one environment to the other.
