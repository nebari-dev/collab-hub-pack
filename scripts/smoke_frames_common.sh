#!/usr/bin/env bash

resolve_python() {
    local candidate="${1:-}"
    if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
        printf '%s\n' "${candidate}"
        return
    fi
    if [ -x "api/.venv/bin/python" ]; then
        printf '%s\n' "api/.venv/bin/python"
        return
    fi
    printf '%s\n' python
}

load_api_image_for_kind() {
    local image="$1"
    local root_dir="$2"
    local cluster_name="${3:-}"

    docker build --build-context projectroot="${root_dir}" -t "${image}" "${root_dir}/api"
    if [ -n "${cluster_name}" ]; then
        kind load docker-image "${image}" --name "${cluster_name}"
    fi
}

chart_service_name() {
    local release="$1"
    local root_dir="$2"
    shift 2

    helm template "${release}" "${root_dir}/helm/collab-hub" \
        --set api.nebariapp.hostname=collab.example.com \
        "$@" \
        | awk '/^kind: Service$/{found=1} found && /^  name:/{print $2; exit}'
}

chart_deployment_name() {
    local release="$1"
    local root_dir="$2"
    shift 2

    helm template "${release}" "${root_dir}/helm/collab-hub" \
        --set api.nebariapp.hostname=collab.example.com \
        "$@" \
        | awk '/^kind: Deployment$/{found=1} found && /^  name:/{print $2; exit}'
}
