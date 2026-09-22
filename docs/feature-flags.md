# Feature flags

Trunk-based development lands work in progress on `main`, so a feature can
reach a deployment before it is ready to expose. Feature flags keep such a
feature dark by default and let an operator turn it on per deployment with an
environment variable — no code change, no long-lived branch.

## Setting a flag

A flag is off unless its variable is set to a truthy value (`1`, `true`,
`yes`, `on`, any case):

```sh
COLLAB_HUB_API__FEATURES__COGS_UI=true
```

With the Helm chart, set it through the API deployment's environment the same
way as any other `COLLAB_HUB_API__*` setting.

## Reading a flag

All reads go through the one accessor on the config — never a bare
`os.environ` lookup:

```python
if config.features.enabled("cogs_ui"):
    ...
```

Unknown flags read as off, so checking a flag never needs registration.

## Adding a flag

Pick a name, gate the code path on `config.features.enabled("<name>")`, and
list the flag with one line on what it exposes in the feature's own docs or
PR. Flags in this block are expected to disappear once the feature ships: a
shipped capability that needs a permanent operational off-switch belongs on
its own config section as an explicit `bool` field with a documented default
(the `connectors.github.api_get_enabled` pattern), not here.
