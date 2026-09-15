"""Capability resolution and model bindings for Cogs.

The resolver selects declared providers and projects a model Cog into the
connection configuration a context Cog needs. Secrets remain references; the
resolver never loads or embeds their values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class BindingResolutionError(LookupError):
    """Raised when a declared Cog dependency cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """A structured capability requirement declared by a Cog."""

    capability: str
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class ModelCog:
    """A model provider's public connection metadata.

    ``auth_ref`` identifies a secret or credential binding managed by the
    runtime. It is never a token or password.
    """

    name: str
    endpoint: str
    model_identifier: str
    auth_ref: str
    transport: str = "http"
    provides: frozenset[str] = field(default_factory=lambda: frozenset({"model"}))


@dataclass(frozen=True, slots=True)
class ContextCog:
    """A context Cog that points to a model Cog rather than embedding one."""

    name: str
    model: CapabilityRequirement


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """The non-secret model configuration delivered to a worker."""

    endpoint: str
    model_identifier: str
    auth_ref: str
    transport: str


class CapabilityResolver(Protocol):
    def resolve_model(self, context: ContextCog, models: tuple[ModelCog, ...]) -> ModelBinding:
        """Resolve a context Cog's model requirement into a worker binding."""


class DeclaredCapabilityResolver(CapabilityResolver):
    """Resolve model capabilities using only structured declarations."""

    def resolve_model(self, context: ContextCog, models: tuple[ModelCog, ...]) -> ModelBinding:
        requirement = context.model
        matching = [model for model in models if requirement.capability in model.provides]
        if requirement.provider is not None:
            matching = [model for model in matching if model.name == requirement.provider]
        if len(matching) != 1:
            detail = "no matching model Cog" if not matching else "multiple matching model Cogs"
            raise BindingResolutionError(f"{detail} for {context.name!r}")
        model = matching[0]
        return ModelBinding(
            endpoint=model.endpoint,
            model_identifier=model.model_identifier,
            auth_ref=model.auth_ref,
            transport=model.transport,
        )
