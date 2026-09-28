# Feature flags

Work in progress lands on `main`, so a feature can reach a deployment before it is ready to expose. Feature flags keep such a feature dark by default and let an operator turn it on per deployment, with no code change and no long-lived branch.

The mechanism is always on and needs no registration. Every flag is off until a deployment sets it to true.

## Turning a flag on

With the Helm chart, add the flag to `features` and set it to `true`:

```yaml
features:
  cogs_ui: true
```

The chart renders each entry as an environment variable, `COLLAB_HUB_API__FEATURES__<NAME>`, which is also how to set a flag without the chart:

```sh
COLLAB_HUB_API__FEATURES__COGS_UI=true
```

The variable name follows pydantic-settings' [nested environment variables](https://docs.pydantic.dev/latest/concepts/pydantic_settings/#parsing-environment-variable-values), and the value is parsed with pydantic's [boolean rules](https://docs.pydantic.dev/latest/api/standard_library_types/#booleans): `true`, `1`, `yes` and `on` (any case) turn a flag on, and `false`, `0`, `no` and `off` keep it off. Any other value stops the API at startup with an error naming the flag, so a typo never silently leaves a feature off.

## Reading a flag

Code reads flags only through the one accessor on the config, never with a bare `os.environ` lookup:

```python
if config.features.enabled("cogs_ui"):
    ...
```

A flag that nobody set reads as off.

## Adding a flag

Pick a name, gate the code path on `config.features.enabled("<name>")`, and describe what the flag exposes in the feature's own docs. Flags are expected to disappear once the feature ships. A shipped capability that needs a permanent operational switch belongs in its own config section as an explicit `bool` field with a documented default, such as `connectors.github.api_get_enabled`.
