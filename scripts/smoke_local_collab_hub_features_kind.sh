#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
. "${SCRIPT_DIR}/smoke_frames_common.sh"

RELEASE="${RELEASE:-collab-hub-features-smoke}"
NAMESPACE="${NAMESPACE:-collab-hub-features-smoke}"
IMAGE="${IMAGE:-collab-hub-api:features-smoke}"
LOCAL_PORT="${LOCAL_PORT:-18084}"
CLUSTER_NAME="${CLUSTER_NAME:-}"
PYTHON_BIN="$(resolve_python "${PYTHON_BIN:-}")"
FAKE_GOOGLE_IMAGE="${FAKE_GOOGLE_IMAGE:-python:3.14-slim}"
FAKE_SLACK_IMAGE="${FAKE_SLACK_IMAGE:-python:3.14-slim}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16.4-alpine}"
POSTGRES_USER="${POSTGRES_USER:-tasks}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-tasks-password}"
POSTGRES_DB="${POSTGRES_DB:-tasks}"
DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres.${NAMESPACE}.svc.cluster.local:5432/${POSTGRES_DB}"

load_api_image_for_kind "${IMAGE}" "${ROOT_DIR}" "${CLUSTER_NAME}"

# Add local Collab Hub feature smokes to this script when a feature needs to be
# exercised through a kind-deployed chart boundary.

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "${NAMESPACE}" app.kubernetes.io/managed-by=Helm --overwrite
kubectl annotate namespace "${NAMESPACE}" \
    meta.helm.sh/release-name="${RELEASE}" \
    meta.helm.sh/release-namespace="${NAMESPACE}" \
    --overwrite
kubectl -n "${NAMESPACE}" create secret generic postgres-credentials \
    --from-literal=POSTGRES_USER="${POSTGRES_USER}" \
    --from-literal=POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
    --from-literal=POSTGRES_DB="${POSTGRES_DB}" \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${NAMESPACE}" create secret generic task-postgres \
    --from-literal=DATABASE_URL="${DATABASE_URL}" \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${NAMESPACE}" create configmap fake-google-drive-app \
    --from-file=app.py="${ROOT_DIR}/scripts/testdata/fake_google_drive_app.py" \
    --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${NAMESPACE}" create configmap fake-slack-app \
    --from-file=app.py="${ROOT_DIR}/scripts/testdata/fake_slack_app.py" \
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

kubectl -n "${NAMESPACE}" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fake-google-drive
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: fake-google-drive
  template:
    metadata:
      labels:
        app.kubernetes.io/name: fake-google-drive
    spec:
      containers:
        - name: fake-google-drive
          image: ${FAKE_GOOGLE_IMAGE}
          command: ["python", "/app/app.py"]
          ports:
            - name: http
              containerPort: 8000
          volumeMounts:
            - name: fake-google-drive-app
              mountPath: /app
              readOnly: true
          readinessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 1
            periodSeconds: 2
      volumes:
        - name: fake-google-drive-app
          configMap:
            name: fake-google-drive-app
---
apiVersion: v1
kind: Service
metadata:
  name: fake-google-drive
spec:
  selector:
    app.kubernetes.io/name: fake-google-drive
  ports:
    - name: http
      port: 8000
      targetPort: http
EOF

kubectl -n "${NAMESPACE}" rollout status deployment/fake-google-drive --timeout=120s

kubectl -n "${NAMESPACE}" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fake-slack
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: fake-slack
  template:
    metadata:
      labels:
        app.kubernetes.io/name: fake-slack
    spec:
      containers:
        - name: fake-slack
          image: ${FAKE_SLACK_IMAGE}
          command: ["python", "/app/app.py"]
          ports:
            - name: http
              containerPort: 8000
          volumeMounts:
            - name: fake-slack-app
              mountPath: /app
              readOnly: true
          readinessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 1
            periodSeconds: 2
      volumes:
        - name: fake-slack-app
          configMap:
            name: fake-slack-app
---
apiVersion: v1
kind: Service
metadata:
  name: fake-slack
spec:
  selector:
    app.kubernetes.io/name: fake-slack
  ports:
    - name: http
      port: 8000
      targetPort: http
EOF

kubectl -n "${NAMESPACE}" rollout status deployment/fake-slack --timeout=120s

helm upgrade --install "${RELEASE}" "${ROOT_DIR}/helm/collab-hub" \
    --create-namespace \
    --namespace "${NAMESPACE}" \
    --set api.deployment.image.repository="${IMAGE%:*}" \
    --set api.deployment.image.tag="${IMAGE##*:}" \
    --set api.deployment.image.pullPolicy=IfNotPresent \
    --set api.nebariapp.hostname=collab.example.com \
    --set frames.activeState.backend=memory \
    --set tasks.backend=postgres \
    --set tasks.postgres.existingSecret=task-postgres \
    --set tasks.postgres.autoMigrate=true \
    --set connectors.google.brokerTokenUrl="http://fake-google-drive.${NAMESPACE}.svc.cluster.local:8000/broker/token" \
    --set connectors.google.driveApiBaseUrl="http://fake-google-drive.${NAMESPACE}.svc.cluster.local:8000/drive/v3" \
    --set connectors.google.gmailApiBaseUrl="http://fake-google-drive.${NAMESPACE}.svc.cluster.local:8000/gmail/v1" \
    --set connectors.google.calendarApiBaseUrl="http://fake-google-drive.${NAMESPACE}.svc.cluster.local:8000/calendar/v3" \
    --set connectors.google.requestTimeoutSeconds=5 \
    --set connectors.slack.brokerTokenUrl="http://fake-slack.${NAMESPACE}.svc.cluster.local:8000/broker/token" \
    --set connectors.slack.apiBaseUrl="http://fake-slack.${NAMESPACE}.svc.cluster.local:8000/api" \
    --set connectors.slack.requestTimeoutSeconds=5 \
    --set api.deployment.extraEnv[0].name=FRAMES_UNSAFE_AUTH_ENABLED \
    --set-string api.deployment.extraEnv[0].value=true \
    --set api.deployment.extraEnv[1].name=FRAMES_BEARER_ALLOW_UNSIGNED \
    --set-string api.deployment.extraEnv[1].value=true \
    --wait \
    --timeout 3m

SERVICE_NAME="$(chart_service_name "${RELEASE}" "${ROOT_DIR}" \
    --set connectors.google.brokerTokenUrl="http://fake-google-drive.${NAMESPACE}.svc.cluster.local:8000/broker/token" \
    --set connectors.google.driveApiBaseUrl="http://fake-google-drive.${NAMESPACE}.svc.cluster.local:8000/drive/v3")"

kubectl -n "${NAMESPACE}" port-forward "svc/${SERVICE_NAME}" "${LOCAL_PORT}:80" >/tmp/collab-hub-features-port-forward.log 2>&1 &
PF_PID=$!
trap 'kill ${PF_PID} >/dev/null 2>&1 || true' EXIT
sleep 3

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/smoke_connectors_google_drive.py" \
    --base-url "http://127.0.0.1:${LOCAL_PORT}"

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/smoke_connectors_google_workspace.py" \
    --base-url "http://127.0.0.1:${LOCAL_PORT}"

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/smoke_connectors_slack.py" \
    --base-url "http://127.0.0.1:${LOCAL_PORT}"

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/smoke_tasks.py" \
    --base-url "http://127.0.0.1:${LOCAL_PORT}"

API_DEPLOYMENT="$(chart_deployment_name "${RELEASE}" "${ROOT_DIR}" \
    --set connectors.google.brokerTokenUrl="http://fake-google-drive.${NAMESPACE}.svc.cluster.local:8000/broker/token" \
    --set connectors.google.driveApiBaseUrl="http://fake-google-drive.${NAMESPACE}.svc.cluster.local:8000/drive/v3")"
API_LOGS="$(kubectl -n "${NAMESPACE}" logs "deployment/${API_DEPLOYMENT}" --tail=300)"
if echo "${API_LOGS}" | grep -q "fake-google-access-token"; then
    echo "expected Collab Hub logs not to contain the Google access token" >&2
    exit 1
fi
if echo "${API_LOGS}" | grep -q "fake-slack-access-token"; then
    echo "expected Collab Hub logs not to contain the Slack access token" >&2
    exit 1
fi
