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
#   HELM="docker run --rm -v $PWD:$PWD -w $PWD alpine/helm" PYTHON="uv run --project api python" scripts/chart_render_tests.sh
set -eu

HELM="${HELM:-helm}"
PYTHON="${PYTHON:-python3}"   # needs PyYAML; the assertions live in chart_render_check.py
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHART="${CHART:-$ROOT/helm/collab-hub}"
CI_VALUES="$CHART/ci/cogs-values.yaml"
TESTDATA="$ROOT/scripts/testdata/chart"
failures=0

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n     %s\n' "$1" "$2"; failures=$((failures + 1)); }

# check <name> <case in chart_render_check.py> [helm args...]
check() {
  name="$1"; case="$2"; shift 2
  if out="$($HELM template t "$CHART" "$@" 2>&1)"; then
    if err="$(printf '%s' "$out" | $PYTHON "$ROOT/scripts/chart_render_check.py" "$case" 2>&1)"; then
      pass "$name"
    else
      fail "$name" "$err"
    fi
  else
    fail "$name" "render failed: $(printf '%s' "$out" | head -3)"
  fi
}

# expect_fail <name> <substring of the expected error> [helm args...]
expect_fail() {
  name="$1"; pattern="$2"; shift 2
  if out="$($HELM template t "$CHART" "$@" 2>&1)"; then
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
check "default values: only the enabled flag, no CA mount" default
check "ci fixture: harbor + static, Secrets attached, CA mounted read-only" fixture -f "$CI_VALUES"
check "sources with the indexer off: JSON renders, tuning vars do not" static-only $STATIC
check "three sources: each credential attached to its own source" three-sources -f "$TESTDATA/cogs-three-sources.yaml"
check "middle source removed and order reversed: attachments follow the id" reordered -f "$TESTDATA/cogs-reordered.yaml"

# --- negative: validations template -------------------------------------------
expect_fail "index enabled with zero sources" 'cogs.index.enabled=true with no cogs.registry.sources' --set cogs.index.enabled=true
expect_fail "credentials block with empty Secret name" 'credentials.existingSecret is empty' $HARBOR --set 'cogs.registry.sources[0].credentials.existingSecret='
expect_fail "webhook block with empty Secret name" 'webhook.existingSecret is empty' $HARBOR --set 'cogs.registry.sources[0].webhook.existingSecret='
expect_fail "duplicate source id" 'reuses an id' $STATIC --set cogs.registry.sources[1].id=public --set cogs.registry.sources[1].kind=static --set cogs.registry.sources[1].url=https://other.example.com --set cogs.registry.sources[1].repositories[0]=cogs/beta
expect_fail "ids colliding after env-name derivation" 'same Secret env var names' $STATIC --set cogs.registry.sources[1].id=public --set cogs.registry.sources[1].id=pub-lic --set cogs.registry.sources[0].id=pub.lic --set cogs.registry.sources[1].kind=static --set cogs.registry.sources[1].url=https://other.example.com --set cogs.registry.sources[1].repositories[0]=cogs/beta
expect_fail "harbor without projects" 'needs at least one entry in projects' --set cogs.registry.sources[0].id=h --set cogs.registry.sources[0].kind=harbor --set cogs.registry.sources[0].url=https://harbor.example.com
expect_fail "static without repositories or indexUrl" 'needs repositories and/or indexUrl' --set cogs.registry.sources[0].id=s --set cogs.registry.sources[0].kind=static --set cogs.registry.sources[0].url=https://registry.example.com
expect_fail "harbor with repositories" 'does not read repositories or indexUrl' $HARBOR --set cogs.registry.sources[0].repositories[0]=cogs/x
expect_fail "harbor with indexUrl" 'does not read repositories or indexUrl' $HARBOR --set cogs.registry.sources[0].indexUrl=https://harbor.example.com/catalog.v1.json
expect_fail "static with projects" 'does not read projects or apiUrl' $STATIC --set cogs.registry.sources[0].projects[0]=cogs
expect_fail "static with apiUrl" 'does not read projects or apiUrl' $STATIC --set cogs.registry.sources[0].apiUrl=http://registry.svc:5000
expect_fail "static with a webhook block" 'has no webhook' $STATIC --set cogs.registry.sources[0].webhook.existingSecret=x
expect_fail "CA ConfigMap with empty key" 'cogs.caBundle.key must name' --set cogs.caBundle.configMap=ca --set cogs.caBundle.key=
expect_fail "extraEnv may not reach cogs settings" 'may not set COLLAB_HUB_API__COGS__INDEX__ENABLED' --set api.deployment.extraEnv[0].name=COLLAB_HUB_API__COGS__INDEX__ENABLED --set api.deployment.extraEnv[0].value=true
expect_fail "extraEnv may not reach a source secret var" 'may not set COLLAB_HUB_COGS_SOURCE_X_PASSWORD' --set api.deployment.extraEnv[0].name=COLLAB_HUB_COGS_SOURCE_X_PASSWORD --set api.deployment.extraEnv[0].value=pw

# --- negative: userinfo in URLs must never reach the manifest -------------------
# The schema pattern rejects these first; the template check is the message a
# reader gets if the schema is ever loosened. Either refusal passes here.
USERINFO='embeds a username or password\|pattern'
expect_fail "url with embedded credentials" "$USERINFO" --set cogs.registry.sources[0].id=s --set cogs.registry.sources[0].kind=static --set 'cogs.registry.sources[0].url=https://robot:REVIEW_SYNTHETIC@registry.example.com' --set cogs.registry.sources[0].repositories[0]=cogs/alpha
expect_fail "apiUrl with embedded credentials" "$USERINFO" $HARBOR --set 'cogs.registry.sources[0].apiUrl=http://robot:REVIEW_SYNTHETIC@harbor-core.svc'
expect_fail "tokenUrl with embedded credentials" "$USERINFO" $HARBOR --set 'cogs.registry.sources[0].tokenUrl=http://robot:REVIEW_SYNTHETIC@harbor-core.svc/service/token'
expect_fail "url with embedded credentials, schema skipped: the template rule stands alone" 'embeds a username or password' --skip-schema-validation --set cogs.registry.sources[0].id=s --set cogs.registry.sources[0].kind=static --set 'cogs.registry.sources[0].url=https://robot:REVIEW_SYNTHETIC@registry.example.com' --set cogs.registry.sources[0].repositories[0]=cogs/alpha
expect_fail "indexUrl with embedded credentials" "$USERINFO" $STATIC --set 'cogs.registry.sources[0].indexUrl=https://robot:REVIEW_SYNTHETIC@registry.example.com/catalog.v1.json'

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
