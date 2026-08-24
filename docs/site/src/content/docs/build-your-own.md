---
title: Build your own pack
---

If a workload you'd like to run on Nebari isn't already in the catalog, you can package it yourself.

## What's in a pack?

A pack is a Kubernetes application bundled with a `NebariApp` custom resource. The Nebari Operator reads the `NebariApp` to wire up routing, TLS, and authentication for your app.

If your app already runs on Kubernetes - via Helm, Kustomize, or plain YAML - adding a `NebariApp` resource is all it takes to make it a pack.

## Start from the template

The easiest way to create a pack is the [Software Pack template](https://github.com/nebari-dev/software-pack-template).

1. Click **Use this template** on the template repository.
2. Clone your new repo.
3. Pick the example closest to your application.
4. Follow the instructions in the README to deploy your pack to a Nebari cluster.

The template ships five examples of increasing complexity:

| Example | Best for |
|---------|----------|
| `examples/vanilla-yaml/` | Plain `kubectl apply`, no tooling |
| `examples/kustomize-nginx/` | Per-environment overlays |
| `examples/basic-nginx/` | Simplest Helm chart |
| `examples/auth-fastapi/` | Custom app that reads auth tokens |
| `examples/wrap-existing-chart/` | Wrapping an existing upstream Helm chart |

## Deploy via ArgoCD

To deploy a pack, commit an ArgoCD Application to your gitops repo. This is a small YAML file that tells ArgoCD which pack to deploy and how to configure it.

ArgoCD then:

1. Reads the Application.
2. Pulls in the pack from its repository.
3. Applies the Application's configuration values to the pack.
4. Applies the resulting resources to the cluster.

This GitOps approach means the pack configuration is version-controlled alongside everything else in your infrastructure.

## Private and organizational packs

Packs do not need to be public. Put yours in a private GitHub repo, an internal Git host, or any other location ArgoCD can reach. It works the same as a published pack - it just won't appear in the public catalog.

## Going deeper

- [NebariApp CRD reference](/nebariapp-crd-reference/) - every field explained
- [Authentication flow](/auth-flow/) - how OIDC works end-to-end, including reading the IdToken in your app
- [Release readiness](/release-readiness/) - maturity levels and the promotion checklist for official packs
