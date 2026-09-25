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
# SeaweedFS serves the S3 API here, as it does in dev/compose.yaml: MinIO's images
# left Docker Hub (#114, #115) and then quay.io, and cannot be pulled at all.
S3_IMAGE="${S3_IMAGE:-chrislusf/seaweedfs:4.47}"
# The AWS CLI speaks to any S3, so the bucket is created the same way anywhere.
AWS_CLI_IMAGE="${AWS_CLI_IMAGE:-amazon/aws-cli:2.37.3}"
S3_ACCESS_KEY="${S3_ACCESS_KEY:-devaccesskey}"
S3_SECRET_KEY="${S3_SECRET_KEY:-devsecretkey}"
BUCKET="${BUCKET:-frames}"

load_api_image_for_kind "${IMAGE}" "${ROOT_DIR}" "${CLUSTER_NAME}"

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${NAMESPACE}" create secret generic s3-credentials \
    --from-literal=AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY}" \
    --from-literal=AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY}" \
    --dry-run=client -o yaml | kubectl apply -f -

# The same keys, in the form the S3 server reads them.
kubectl -n "${NAMESPACE}" create configmap s3-identities \
    --from-literal=s3.json="$(cat <<EOF
{
  "identities": [
    {
      "name": "dev",
      "credentials": [{"accessKey": "${S3_ACCESS_KEY}", "secretKey": "${S3_SECRET_KEY}"}],
      "actions": ["Admin", "Read", "Write", "List", "Tagging"]
    }
  ]
}
EOF
)" --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "${NAMESPACE}" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: s3
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: s3
  template:
    metadata:
      labels:
        app.kubernetes.io/name: s3
    spec:
      containers:
        - name: seaweedfs
          image: ${S3_IMAGE}
          args:
            - server
            - -dir=/data
            - -s3
            - -s3.port=9000
            - -s3.config=/etc/seaweedfs/s3.json
            - -master.volumeSizeLimitMB=64
            - -volume.max=10
          ports:
            - name: api
              containerPort: 9000
            - name: master
              containerPort: 9333
          readinessProbe:
            httpGet:
              path: /cluster/healthz
              port: master
            initialDelaySeconds: 3
            periodSeconds: 5
          volumeMounts:
            - name: identities
              mountPath: /etc/seaweedfs
              readOnly: true
      volumes:
        - name: identities
          configMap:
            name: s3-identities
---
apiVersion: v1
kind: Service
metadata:
  name: s3
spec:
  selector:
    app.kubernetes.io/name: s3
  ports:
    - name: api
      port: 9000
      targetPort: api
EOF

kubectl -n "${NAMESPACE}" rollout status deployment/s3 --timeout=120s

# One Job creates the bucket; a second counts what the API wrote into it. Both talk
# S3, so neither depends on the server that serves it.
run_aws_job() {
    local name="$1" script="$2"
    kubectl -n "${NAMESPACE}" delete job "${name}" --ignore-not-found
    kubectl -n "${NAMESPACE}" apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${name}
spec:
  backoffLimit: 3
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: aws
          image: ${AWS_CLI_IMAGE}
          envFrom:
            - secretRef:
                name: s3-credentials
          env:
            - name: AWS_DEFAULT_REGION
              value: us-east-1
          command: ["sh", "-c", "${script}"]
EOF
    kubectl -n "${NAMESPACE}" wait --for=condition=complete "job/${name}" --timeout=120s
}

S3_URL="http://s3:9000"
run_aws_job s3-create-bucket \
    "aws --endpoint-url ${S3_URL} s3api create-bucket --bucket ${BUCKET} >/dev/null 2>&1 || aws --endpoint-url ${S3_URL} s3api head-bucket --bucket ${BUCKET}"

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
    --set frames.s3.endpointUrl="http://s3.${NAMESPACE}.svc.cluster.local:9000" \
    --set frames.s3.region=us-east-1 \
    --set frames.s3.existingSecret=s3-credentials \
    --set frames.activeState.backend=memory \
    --wait \
    --timeout 3m

SERVICE_NAME="$(chart_service_name "${RELEASE}" "${ROOT_DIR}" \
    --set frames.storage.backend=s3 \
    --set frames.s3.bucket="${BUCKET}")"

kubectl -n "${NAMESPACE}" port-forward "svc/${SERVICE_NAME}" "${LOCAL_PORT}:80" >/tmp/collab-hub-frames-s3-port-forward.log 2>&1 &
PF_PID=$!
trap 'kill ${PF_PID} >/dev/null 2>&1 || true' EXIT
sleep 3

"${PYTHON_BIN}" "${ROOT_DIR}/scripts/smoke_frames_http.py" \
    --base-url "http://127.0.0.1:${LOCAL_PORT}" \
    --check-active-state \
    --keep-frame

run_aws_job s3-count-objects \
    "aws --endpoint-url ${S3_URL} s3 ls s3://${BUCKET}/frames --recursive | tee /dev/stderr | wc -l"
OBJECT_COUNT="$(kubectl -n "${NAMESPACE}" logs job/s3-count-objects --tail=1 | tr -d '[:space:]')"
if [ "${OBJECT_COUNT:-0}" -lt 1 ]; then
    echo "expected Frame objects in the S3 store, found ${OBJECT_COUNT:-0}" >&2
    exit 1
fi
