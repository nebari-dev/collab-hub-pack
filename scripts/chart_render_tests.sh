#!/usr/bin/env sh
# Render tests for the collab-hub chart's Cog registry block (issue #87).
#
# The validations templates fail the render on purpose; helm lint alone never
# exercises those branches, and a values fixture that renders proves only the
# happy path. This script renders the positive cases and asserts on their
# output, then renders each negative case and asserts that it fails with the
# expected message. Run from anywhere; needs `helm` on PATH (or HELM=... to
# point at a wrapper, e.g. a docker alias).
#
#   scripts/chart_render_tests.sh
set -eu

HELM="${HELM:-helm}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHART="${CHART:-$ROOT/helm/collab-hub}"
CI_VALUES="$CHART/ci/cogs-values.yaml"
failures=0

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n     %s\n' "$1" "$2"; failures=$((failures + 1)); }

# expect_render <name> <grep pattern that must appear> [helm args...]
expect_render() {
  name="$1"; pattern="$2"; shift 2
  if out="$("$HELM" template t "$CHART" "$@" 2>&1)"; then
    if printf '%s' "$out" | grep -q -- "$pattern"; then pass "$name"; else fail "$name" "rendered, but missing: $pattern"; fi
  else
    fail "$name" "render failed: $(printf '%s' "$out" | head -3)"
  fi
}

# expect_absent <name> <grep pattern that must NOT appear> [helm args...]
expect_absent() {
  name="$1"; pattern="$2"; shift 2
  if out="$("$HELM" template t "$CHART" "$@" 2>&1)"; then
    if printf '%s' "$out" | grep -q -- "$pattern"; then fail "$name" "rendered, but contains: $pattern"; else pass "$name"; fi
  else
    fail "$name" "render failed: $(printf '%s' "$out" | head -3)"
  fi
}

# expect_fail <name> <substring of the expected error> [helm args...]
expect_fail() {
  name="$1"; pattern="$2"; shift 2
  if out="$("$HELM" template t "$CHART" "$@" 2>&1)"; then
    fail "$name" "rendered, but should have failed with: $pattern"
  elif printf '%s' "$out" | grep -q -- "$pattern"; then
    pass "$name"
  else
    fail "$name" "failed for another reason: $(printf '%s' "$out" | head -3)"
  fi
}

STATIC='--set cogs.registry.sources[0].id=public --set cogs.registry.sources[0].kind=static --set cogs.registry.sources[0].url=https://registry.example.com --set cogs.registry.sources[0].repositories[0]=cogs/alpha'
HARBOR='--set cogs.registry.sources[0].id=harbor-main --set cogs.registry.sources[0].kind=harbor --set cogs.registry.sources[0].url=https://harbor.example.com --set cogs.registry.sources[0].projects[0]=cogs'

# --- positive -----------------------------------------------------------------
expect_render "default values render" 'name: COLLAB_HUB_API__COGS__INDEX__ENABLED'
expect_absent "default values: no cogs env beyond the enabled flag" 'COLLAB_HUB_API__COGS__REGISTRY_SOURCES\|COLLAB_HUB_API__COGS__INDEX__INTERVAL\|cogs-ca-bundle'
expect_render "ci fixture: harbor + static render" 'COLLAB_HUB_API__COGS__REGISTRY_SOURCES' -f "$CI_VALUES"
expect_render "ci fixture: robot secret mounted under derived name" 'name: COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD' -f "$CI_VALUES"
expect_render "ci fixture: webhook secret mounted under derived name" 'name: COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_WEBHOOK_SECRET' -f "$CI_VALUES"
expect_render "ci fixture: JSON points at the env names, not values" '\\"password_env\\":\\"COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD\\"' -f "$CI_VALUES"
expect_absent "ci fixture: JSON never carries the Secret names" 'collab-hub-harbor-robot\\"' -f "$CI_VALUES"
expect_render "ci fixture: CA bundle applied to sources without their own" '\\"ca_bundle_path\\":\\"/etc/collab-hub/cogs-ca/ca.crt\\"' -f "$CI_VALUES"
expect_render "ci fixture: per-source caBundlePath wins" '\\"ca_bundle_path\\":\\"/etc/ssl/certs/ca-certificates.crt\\"' -f "$CI_VALUES"
expect_render "ci fixture: CA ConfigMap mounted read-only" 'mountPath: /etc/collab-hub/cogs-ca' -f "$CI_VALUES"
expect_render "sources with index disabled render (static read API)" 'COLLAB_HUB_API__COGS__REGISTRY_SOURCES' $STATIC
expect_absent "index disabled: no interval/run_on_startup env" 'INTERVAL_SECONDS' $STATIC

# --- negative: validations template -------------------------------------------
expect_fail "index enabled with zero sources" 'cogs.index.enabled=true with no cogs.registry.sources' --set cogs.index.enabled=true
expect_fail "credentials block with empty Secret name" 'credentials.existingSecret is empty' $HARBOR --set 'cogs.registry.sources[0].credentials.existingSecret='
expect_fail "webhook block with empty Secret name" 'webhook.existingSecret is empty' $HARBOR --set 'cogs.registry.sources[0].webhook.existingSecret='
expect_fail "duplicate source id" 'reuses an id' $STATIC --set cogs.registry.sources[1].id=public --set cogs.registry.sources[1].kind=static --set cogs.registry.sources[1].url=https://other.example.com --set cogs.registry.sources[1].repositories[0]=cogs/beta
expect_fail "ids colliding after env-name derivation" 'same Secret env var names' $STATIC --set cogs.registry.sources[1].id=public --set cogs.registry.sources[1].id=pub-lic --set cogs.registry.sources[0].id=pub.lic --set cogs.registry.sources[1].kind=static --set cogs.registry.sources[1].url=https://other.example.com --set cogs.registry.sources[1].repositories[0]=cogs/beta
expect_fail "harbor without projects" 'needs at least one entry in projects' --set cogs.registry.sources[0].id=h --set cogs.registry.sources[0].kind=harbor --set cogs.registry.sources[0].url=https://harbor.example.com
expect_fail "static without repositories or indexUrl" 'needs repositories and/or indexUrl' --set cogs.registry.sources[0].id=s --set cogs.registry.sources[0].kind=static --set cogs.registry.sources[0].url=https://registry.example.com
expect_fail "static with a webhook block" 'has no webhook' $STATIC --set cogs.registry.sources[0].webhook.existingSecret=x
expect_fail "CA ConfigMap with empty key" 'cogs.caBundle.key must name' --set cogs.caBundle.configMap=ca --set cogs.caBundle.key=
expect_fail "extraEnv may not reach cogs settings" 'may not set COLLAB_HUB_API__COGS__INDEX__ENABLED' --set api.deployment.extraEnv[0].name=COLLAB_HUB_API__COGS__INDEX__ENABLED --set api.deployment.extraEnv[0].value=true
expect_fail "extraEnv may not reach a source secret var" 'may not set COLLAB_HUB_COGS_SOURCE_X_PASSWORD' --set api.deployment.extraEnv[0].name=COLLAB_HUB_COGS_SOURCE_X_PASSWORD --set api.deployment.extraEnv[0].value=pw

# --- negative: values.schema.json ------------------------------------------------
# Patterns accept both helm 3 ("Additional property X is not allowed",
# "X is required") and helm 4 ("additional properties 'X' not allowed",
# "missing property 'X'") wording.
expect_fail "schema rejects an unknown cogs key" 'bogus.*not allowed' --set cogs.bogus=1
expect_fail "schema rejects an unknown source key" 'repos.*not allowed' $STATIC --set cogs.registry.sources[0].repos[0]=x
expect_fail "schema rejects an unknown kind" 'kind' $STATIC --set cogs.registry.sources[0].kind=quay
expect_fail "schema rejects a missing url" "url is required\\|missing property 'url'" --set cogs.registry.sources[0].id=s --set cogs.registry.sources[0].kind=static --set cogs.registry.sources[0].repositories[0]=cogs/alpha
expect_fail "schema bounds the sweep interval" 'intervalSeconds' $STATIC --set cogs.index.enabled=true --set cogs.index.intervalSeconds=1

if [ "$failures" -ne 0 ]; then
  printf '\n%d chart render test(s) failed\n' "$failures"
  exit 1
fi
printf '\nall chart render tests passed\n'
