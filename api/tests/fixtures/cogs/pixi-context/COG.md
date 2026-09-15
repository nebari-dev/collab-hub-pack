---
type: cog [0.1]
name: cog-meeting-notes
description: Context Cog. Extracts grounded, cited notes from meeting records for a stated focus. Depends on a Cog providing an OpenAI-compatible model endpoint.
version: "0.1.0"
license: BSD-3-Clause
publisher: Example Organization
manifest: pixi.toml
manifest_schema: openteams/cog-manifest [0.1]
metadata:
  category: meetings
  maturity: experimental
---

# Meeting Notes

## Purpose

Extracts grounded, cited notes from meeting records for a stated focus.

## Using it

```bash
pixi run resolve
pixi run ask -- --bundle examples/sample-bundle.json
```
