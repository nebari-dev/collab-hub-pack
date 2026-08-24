---
title: Building a Software Pack
---

This guide covers Nebari Software Packs: what they are, how to build one, and how to maintain it.

A software pack is a Kubernetes application bundled with a `NebariApp` custom resource. When you deploy one, the Nebari platform automatically wires it into the shared routing, TLS, and OIDC authentication systems - no manual gateway or certificate configuration required.

## In this guide

- **[What is a software pack](/what-is-a-software-pack/)** - definition, why packs exist, pack contents, the NebariApp resource, pack lifecycle, and official vs community packs
- **[Build your own](/build-your-own/)** - start from the template, choose an example, deploy via ArgoCD, private and organizational packs

## Reference pages

- **[NebariApp CRD reference](/nebariapp-crd-reference/)** - complete field-by-field reference for the `NebariApp` custom resource
- **[Authentication flow](/auth-flow/)** - how OIDC works end-to-end; reading the IdToken in your app
- **[Release readiness](/release-readiness/)** - maturity levels and the promotion checklist for first-party packs

## Pack template

Use the [software-pack-template](https://github.com/nebari-dev/software-pack-template) as your starting point. It includes five examples spanning plain YAML, Kustomize, basic Helm, auth-aware apps, and wrapping an existing upstream chart.
