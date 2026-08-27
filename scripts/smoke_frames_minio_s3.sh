#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
. "${SCRIPT_DIR}/smoke_frames_common.sh"

RELEASE="${RELEASE:-collab-hub-s3-smoke}"
NAMESPACE="${NAMESPACE:-collab-hub-s3-smoke}"
IMAGE="${IMAGE:-collab-hub-api:frames-smoke}"
LOCAL_PORT="${LOCAL_PORT:-18083}"
CLUSTER_NAME="${CLUSTER_NAME:-}"
PYTHON_BIN="$(resolve_python "${PYTHON_BIN:-}")"
MINIO_IMAGE="${MINIO_IMAGE:-minio/minio:RELEASE.2024-03-30T09-41-56Z}"
MC_IMAGE="${MC_IMAGE:-minio/mc:RELEASE.2024-03-30T15-29-52Z}"
MINIO_USER="${MINIO_USER:-minioadmin}"
MINIO_PASSWORD="${MINIO_PASSWORD:-minioadmin123}"
BUCKET="${BUCKET:-frames}"

load_api_image_for_kind "${IMAGE}" "${ROOT_DIR}" "${CLUSTER_NAME}"

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${NAMESPACE}" create secret generic minio-credentials \
    --from-literal=AWS_ACCESS_KEY_ID="${MINIO_USER}" \
    --from-literal=AWS_SECRET_ACCESS_KEY="${MINIO_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "${NAMESPACE}" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: minio
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: minio
  template:
    metadata:
      labels:
        app.kubernetes.io/name: minio
    spec:
      containers:
        - name: minio
          image: ${MINIO_IMAGE}
          args: ["server", "/data"]
          env:
            - name: MINIO_ROOT_USER
              valueFrom:
                secretKeyRef:
                  name: minio-credentials
                  key: AWS_ACCESS_KEY_ID
            - name: MINIO_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: minio-credentials
                  key: AWS_SECRET_ACCESS_KEY
          ports:
            - name: api
              containerPort: 9000
          readinessProbe:
            httpGet:
              path: /minio/health/ready
              port: api
            initialDelaySeconds: 5
            periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: minio
spec:
  selector:
    app.kubernetes.io/name: minio
  ports:
    - name: api
      port: 9000
      targetPort: api
EOF

kubectl -n "${NAMESPACE}" rollout status deployment/minio --timeout=120s
kubectl -n "${NAMESPACE}" delete job minio-create-bucket --ignore-not-found
kubectl -n "${NAMESPACE}" apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: minio-create-bucket
spec:
  backoffLimit: 3
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: mc
          image: ${MC_IMAGE}
          env:
            - name: MINIO_ROOT_USER
              valueFrom:
                secretKeyRef:
                  name: minio-credentials
                  key: AWS_ACCESS_KEY_ID
            - name: MINIO_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: minio-credentials
                  key: AWS_SECRET_ACCESS_KEY
          command:
            - /bin/sh
            - -c
            - |
              mc alias set local http://minio:9000 "\${MINIO_ROOT_USER}" "\${MINIO_ROOT_PASSWORD}"
              mc mb --ignore-existing local/${BUCKET}
EOF
kubectl -n "${NAMESPACE}" wait --for=condition=complete job/minio-create-bucket --timeout=120s

helm upgrade --install "${RELEASE}" "${ROOT_DIR}/helm/collab-hub" \
    --create-namespace \
    --namespace "${NAMESPACE}" \
    --set api.deployment.image.repository="${IMAGE%:*}" \
    --set api.deployment.image.tag="${IMAGE##*:}" \
    --set api.deployment.image.pullPolicy=IfNotPresent \
    --set api.nebariapp.hostname=collab.example.com \
    --set frames.storage.backend=s3 \
    --set frames.s3.bucket="${BUCKET}" \
    --set frames.s3.prefix=frames \
    --set frames.s3.endpointUrl="http://minio.${NAMESPACE}.svc.cluster.local:9000" \
    --set frames.s3.region=us-east-1 \
    --set frames.s3.existingSecret=minio-credentials \
    --set frames.activeState.backend=memory \
    --wait \
    --timeout 3m

SERVICE_NAME="$(chart_service_name "${RELEASE}" "${ROOT_DIR}" \
    --set frames.storage.backend=s3 \
    --set frames.s3.bucket="${BUCKET}")"

kubectl -n "${NAMESPACE}" port-forward "svc/${SERVICE_NAME}" "${LOCAL_PORT}:80" >/tmp/collab-hub-frames-minio-port-forward.log 2>&1 &
PF_PID=$!
trap 'kill ${PF_PID} >/dev/null 2>&1 || true' EXIT
sleep 3

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/smoke_frames_http.py" \
    --base-url "http://127.0.0.1:${LOCAL_PORT}" \
    --check-active-state \
    --keep-frame

OBJECT_COUNT="$(kubectl -n "${NAMESPACE}" exec deployment/minio -- sh -c "find /data/${BUCKET}/frames -type f 2>/dev/null | wc -l")"
if [ "${OBJECT_COUNT}" -lt 1 ]; then
    echo "expected Frame objects in MinIO, found ${OBJECT_COUNT}" >&2
    exit 1
fi
