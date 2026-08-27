# Local development Makefile for Nebari Software Pack examples
#
# Creates a kind cluster with the full Nebari infrastructure stack
# (MetalLB, Envoy Gateway, cert-manager, Keycloak, nebari-operator)
# so all examples deploy with NebariApp enabled.
#
# Prerequisites: docker, kind, helm, kubectl, git
#
# Usage:
#   make up-vanilla    Deploy the vanilla YAML example
#   make up-kustomize  Deploy the kustomize example (dev overlay)
#   make up-basic      Deploy the basic nginx Helm example
#   make up-fastapi    Deploy the FastAPI Helm example (pre-built GHCR image)
#   make up-podinfo    Deploy the podinfo wrapper Helm example
#   make update-hosts  Update /etc/hosts with NebariApp hostnames
#   make down          Delete the kind cluster

VANILLA_DIR   := ../examples/vanilla-yaml
KUSTOMIZE_DIR := ../examples/kustomize-nginx
CHART_BASIC   := ../examples/basic-nginx/chart
CHART_FASTAPI := ../examples/auth-fastapi/chart
CHART_PODINFO := ../examples/wrap-existing-chart/chart
CLUSTER_NAME  := my-pack-dev
HOSTNAME      := my-pack.nebari.local
CACHE_DIR     := .cache
OPERATOR_REPO := $(CACHE_DIR)/nebari-operator

# MetalLB IP pool configuration template.
# METALLB_BASE_IP is substituted at runtime with the Kind network base.
define METALLB_POOL
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: kind-pool
  namespace: metallb-system
spec:
  addresses:
  - METALLB_BASE_IP.255.200-METALLB_BASE_IP.255.250
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: kind-l2
  namespace: metallb-system
spec:
  ipAddressPools:
  - kind-pool
endef
export METALLB_POOL

.PHONY: up-vanilla up-kustomize up-basic up-fastapi up-podinfo down cluster update-hosts

# --------------------------------------------------------------------------
# cluster - create kind cluster with full Nebari infrastructure
# --------------------------------------------------------------------------
cluster:
	@if kind get clusters 2>/dev/null | grep -q "^$(CLUSTER_NAME)$$"; then \
		echo "Cluster '$(CLUSTER_NAME)' already exists, skipping setup"; \
	else \
		$(MAKE) _cluster-create; \
	fi

_cluster-create: _clone-operator _kind-create _metallb _services _keycloak-setup _operator _label-namespace
	@echo ""
	@echo "=========================================="
	@echo "Cluster setup complete!"
	@echo "=========================================="
	@echo ""
	@echo "Run 'make up-basic' (or any up-* target) to deploy an example."

OPERATOR_REF := v0.1.0-alpha.19

_clone-operator:
	@if [ ! -d "$(OPERATOR_REPO)" ]; then \
		echo "Cloning nebari-operator ($(OPERATOR_REF)) into $(OPERATOR_REPO)..."; \
		mkdir -p $(CACHE_DIR); \
		git clone --depth 1 --branch $(OPERATOR_REF) \
			https://github.com/nebari-dev/nebari-operator.git $(OPERATOR_REPO); \
	else \
		echo "nebari-operator already cloned"; \
	fi

_kind-create:
	@echo "Creating kind cluster: $(CLUSTER_NAME)"
	kind create cluster --name $(CLUSTER_NAME)

_metallb:
	@echo "Installing MetalLB..."
	kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.8/config/manifests/metallb-native.yaml 2>&1 | grep -v "unrecognized format"
	kubectl wait --namespace metallb-system \
		--for=condition=available deployment/controller \
		--timeout=90s
	kubectl wait --namespace metallb-system \
		--for=jsonpath='{.status.numberReady}'=1 \
		daemonset/speaker \
		--timeout=90s
	@KIND_NET_CIDR=$$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Subnet}}{{"\n"}}{{end}}' 2>/dev/null | grep '\.' | head -n1); \
	BASE_IP=$$(echo $${KIND_NET_CIDR} | awk -F. '{print $$1"."$$2}'); \
	echo "MetalLB IP pool: $${BASE_IP}.255.200-$${BASE_IP}.255.250"; \
	echo "$$METALLB_POOL" | sed "s/METALLB_BASE_IP/$${BASE_IP}/g" | kubectl apply -f -
	@echo "MetalLB installed"

_services:
	@echo "Installing foundational services (Envoy Gateway, cert-manager, Keycloak, Gateway, TLS)..."
	CLUSTER_NAME=$(CLUSTER_NAME) $(OPERATOR_REPO)/dev/scripts/services/install.sh

_keycloak-setup:
	@echo "Setting up Keycloak realm..."
	CLUSTER_NAME=$(CLUSTER_NAME) $(OPERATOR_REPO)/dev/scripts/services/keycloak/setup.sh

_operator:
	@echo "Installing nebari-operator..."
	kubectl apply -f https://github.com/nebari-dev/nebari-operator/releases/download/$(OPERATOR_REF)/install.yaml
	kubectl wait --for=condition=available deployment/nebari-operator-controller-manager \
		-n nebari-operator-system --timeout=120s
	@echo "nebari-operator is running"

_label-namespace:
	kubectl label namespace default nebari.dev/managed=true --overwrite

# --------------------------------------------------------------------------
# up-vanilla - deploy plain YAML example with NebariApp
# --------------------------------------------------------------------------
up-vanilla: cluster
	kubectl apply -f $(VANILLA_DIR)/deployment.yaml \
		-f $(VANILLA_DIR)/service.yaml
	sed 's/my-pack.nebari.example.com/$(HOSTNAME)/' \
		$(VANILLA_DIR)/nebariapp.yaml | kubectl apply -f -
	kubectl wait --for=condition=ready pod -l app=my-pack --timeout=120s
	kubectl wait --for=condition=Ready nebariapp/my-pack --timeout=120s
	CLUSTER_NAME=$(CLUSTER_NAME) $(OPERATOR_REPO)/dev/scripts/networking/update-hosts.sh
	@echo ""
	@echo "Vanilla nginx deployed with NebariApp."
	@echo "  https://$(HOSTNAME)"

# --------------------------------------------------------------------------
# up-kustomize - deploy kustomize example (dev overlay) with NebariApp
# --------------------------------------------------------------------------
up-kustomize: cluster
	kubectl kustomize $(KUSTOMIZE_DIR)/overlays/dev/ \
		| sed 's/my-pack.dev.nebari.example.com/$(HOSTNAME)/' \
		| kubectl apply -f -
	kubectl wait --for=condition=ready pod -l app=my-pack --timeout=120s
	kubectl wait --for=condition=Ready nebariapp/my-pack --timeout=120s
	CLUSTER_NAME=$(CLUSTER_NAME) $(OPERATOR_REPO)/dev/scripts/networking/update-hosts.sh
	@echo ""
	@echo "Kustomize nginx deployed with NebariApp."
	@echo "  https://$(HOSTNAME)"

# --------------------------------------------------------------------------
# up-basic - deploy basic nginx Helm example with NebariApp
# --------------------------------------------------------------------------
up-basic: cluster
	helm upgrade --install my-pack $(CHART_BASIC) \
		--set nebariapp.enabled=true \
		--set nebariapp.hostname=$(HOSTNAME) \
		--wait --timeout 2m
	kubectl wait --for=condition=Ready nebariapp/my-pack-my-pack --timeout=120s
	CLUSTER_NAME=$(CLUSTER_NAME) $(OPERATOR_REPO)/dev/scripts/networking/update-hosts.sh
	@echo ""
	@echo "Basic nginx deployed with NebariApp."
	@echo "  https://$(HOSTNAME)"

# --------------------------------------------------------------------------
# up-fastapi - deploy auth-aware FastAPI Helm example with NebariApp
# --------------------------------------------------------------------------
up-fastapi: cluster
	helm upgrade --install my-pack $(CHART_FASTAPI) \
		--set nebariapp.enabled=true \
		--set nebariapp.hostname=$(HOSTNAME) \
		--set nebariapp.auth.enabled=true \
		--wait --timeout 2m
	kubectl wait --for=condition=Ready nebariapp/my-pack-my-pack --timeout=120s
	CLUSTER_NAME=$(CLUSTER_NAME) $(OPERATOR_REPO)/dev/scripts/networking/update-hosts.sh
	@echo ""
	@echo "FastAPI app deployed with NebariApp (auth enabled)."
	@echo "  https://$(HOSTNAME)"
	@echo "  Keycloak login: admin / nebari-admin"

# --------------------------------------------------------------------------
# up-podinfo - deploy podinfo wrapper Helm example with NebariApp
# --------------------------------------------------------------------------
up-podinfo: cluster
	helm dependency update $(CHART_PODINFO)
	helm upgrade --install my-pack $(CHART_PODINFO) \
		--set nebariapp.enabled=true \
		--set nebariapp.hostname=$(HOSTNAME) \
		--wait --timeout 3m
	kubectl wait --for=condition=Ready nebariapp/my-pack-my-pack --timeout=120s
	CLUSTER_NAME=$(CLUSTER_NAME) $(OPERATOR_REPO)/dev/scripts/networking/update-hosts.sh
	@echo ""
	@echo "Podinfo deployed with NebariApp."
	@echo "  https://$(HOSTNAME)"

# --------------------------------------------------------------------------
# update-hosts - update /etc/hosts with all NebariApp hostnames
# --------------------------------------------------------------------------
update-hosts:
	CLUSTER_NAME=$(CLUSTER_NAME) $(OPERATOR_REPO)/dev/scripts/networking/update-hosts.sh

# --------------------------------------------------------------------------
# down - delete the kind cluster
# --------------------------------------------------------------------------
down:
	kind delete cluster --name $(CLUSTER_NAME)
