---
title: What is a software pack
---

A software pack is a self-contained, installable artifact published to a git repository that delivers a specific capability to a Nebari cluster - a chat assistant, a BI dashboard, a model-serving stack, and so on. Once installed, a pack is automatically wired into the platform's login, routing, and TLS systems.

## Why packs exist

The pack architecture gives four concrete benefits:

- **Composable:** Teams install only the capabilities they need, without pulling in unrelated components.
- **Independent upgrades:** Each pack updates on its own schedule without affecting the others.
- **Bring your own pack:** Anyone can build a custom pack using the same template and resources as the official ones.
- **Smaller blast radius:** When one pack fails, others keep running. Troubleshooting and rollbacks stay scoped to that pack.

## What a pack contains

A pack is a set of Kubernetes manifests:

- Application workloads: Deployments, Services, ConfigMaps, and any other resources the app needs.
- A `NebariApp` custom resource that tells the Nebari Operator to wire the pack into the platform's routing, TLS, and authentication systems.

An ArgoCD Application deploys a pack by pointing at the pack repository and supplying configuration values. Because the config is separate from the pack itself, the same pack can be deployed multiple times with different settings.

## The NebariApp resource

The `NebariApp` manifest is the integration point between your application and the Nebari platform. It declares the hostname to expose, the Kubernetes Service to route traffic to, whether to provision a TLS certificate, and whether to require login.

From that declaration, the Nebari Operator creates:

- An **HTTPRoute** directing traffic to the target service
- A **Certificate** for HTTPS connections (via cert-manager)
- A **Keycloak client** for user authentication (via an Envoy Gateway SecurityPolicy)

See the [NebariApp CRD reference](/nebariapp-crd-reference/) for the full field list, and [Authentication flow](/auth-flow/) for how the OIDC login sequence works end-to-end.

## Pack lifecycle

When a pack is deployed, it passes through four stages:

1. An ArgoCD Application is committed to the gitops repo, and ArgoCD syncs it.
2. ArgoCD applies the pack's workloads and the `NebariApp` resource to the cluster.
3. The Nebari Operator processes the `NebariApp` and creates the routing, certificate, and authentication client.
4. The pack appears on the Nebari landing page as an available capability.

## Official vs community packs

Packs come from two places:

- **Official packs** are maintained by OpenTeams under the `nebari-dev` GitHub organization.
- **Community packs** are created by external developers. They follow the same structural requirements as official packs but are maintained independently.
