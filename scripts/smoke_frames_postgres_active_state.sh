#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
. "${SCRIPT_DIR}/smoke_frames_common.sh"

RELEASE="${RELEASE:-collab-hub-postgres-smoke}"
NAMESPACE="${NAMESPACE:-collab-hub-postgres-smoke}"
IMAGE="${IMAGE:-collab-hub-api:frames-smoke}"
LOCAL_PORT="${LOCAL_PORT:-18085}"
CLUSTER_NAME="${CLUSTER_NAME:-}"
PYTHON_BIN="$(resolve_python "${PYTHON_BIN:-}")"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16.4-alpine}"
POSTGRES_USER="${POSTGRES_USER:-frames}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-frames-password}"
POSTGRES_DB="${POSTGRES_DB:-frames}"
DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres.${NAMESPACE}.svc.cluster.local:5432/${POSTGRES_DB}"

load_api_image_for_kind "${IMAGE}" "${ROOT_DIR}" "${CLUSTER_NAME}"

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${NAMESPACE}" create secret generic postgres-credentials \
    --from-literal=POSTGRES_USER="${POSTGRES_USER}" \
    --from-literal=POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
    --from-literal=POSTGRES_DB="${POSTGRES_DB}" \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${NAMESPACE}" create secret generic frames-active-state \
    --from-literal=DATABASE_URL="${DATABASE_URL}" \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "${NAMESPACE}" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgres
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: postgres
  template:
    metadata:
      labels:
        app.kubernetes.io/name: postgres
    spec:
      containers:
        - name: postgres
          image: ${POSTGRES_IMAGE}
          envFrom:
            - secretRef:
                name: postgres-credentials
          ports:
            - name: postgres
              containerPort: 5432
          readinessProbe:
            exec:
              command: ["pg_isready", "-U", "${POSTGRES_USER}", "-d", "${POSTGRES_DB}"]
            initialDelaySeconds: 5
            periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
spec:
  selector:
    app.kubernetes.io/name: postgres
  ports:
    - name: postgres
      port: 5432
      targetPort: postgres
EOF

kubectl -n "${NAMESPACE}" rollout status deployment/postgres --timeout=120s

helm upgrade --install "${RELEASE}" "${ROOT_DIR}/helm/collab-hub" \
    --create-namespace \
    --namespace "${NAMESPACE}" \
    --set api.deployment.image.repository="${IMAGE%:*}" \
    --set api.deployment.image.tag="${IMAGE##*:}" \
    --set api.deployment.image.pullPolicy=IfNotPresent \
    --set api.nebariapp.hostname=collab.example.com \
    --set frames.activeState.backend=postgres \
    --set frames.activeState.postgres.existingSecret=frames-active-state \
    --set frames.activeState.postgres.autoMigrate=true \
    --wait \
    --timeout 3m

SERVICE_NAME="$(chart_service_name "${RELEASE}" "${ROOT_DIR}" \
    --set frames.activeState.backend=postgres \
    --set frames.activeState.postgres.existingSecret=frames-active-state)"

kubectl -n "${NAMESPACE}" port-forward "svc/${SERVICE_NAME}" "${LOCAL_PORT}:80" >/tmp/collab-hub-frames-postgres-port-forward.log 2>&1 &
PF_PID=$!
trap 'kill ${PF_PID} >/dev/null 2>&1 || true' EXIT
sleep 3

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/smoke_frames_http.py" \
    --base-url "http://127.0.0.1:${LOCAL_PORT}" \
    --check-active-state
