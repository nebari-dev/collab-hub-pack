#!/usr/bin/env bash

resolve_python() {
    local candidate="${1:-}"
    if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
        printf '%s\n' "${candidate}"
        return
    fi
    # Anchored to ROOT_DIR, not the working directory. Every script that
    # sources this sets ROOT_DIR before calling, and a caller invoked from
    # elsewhere -- dev/Makefile runs its targets from dev/ -- would otherwise
    # miss the virtualenv entirely and fall back to a bare `python`: often
    # absent on a uv-managed machine, and otherwise an interpreter without the
    # httpx the smoke clients import.
    local venv="${ROOT_DIR:-.}/api/.venv/bin/python"
    if [ -x "${venv}" ]; then
        printf '%s\n' "${venv}"
        return
    fi
    if command -v python3 >/dev/null 2>&1; then
        printf '%s\n' python3
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
