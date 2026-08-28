import pytest

from collab_hub_api.execution import (
    BindingResolutionError,
    CapabilityRequirement,
    ContextCog,
    DeclaredCapabilityResolver,
    ModelCog,
)


def test_context_binding_projects_selected_model_without_secret_value():
    context = ContextCog("summarizer", CapabilityRequirement("model", provider="fast"))
    models = (
        ModelCog("fast", "https://fast.example/v1", "small", "secret/fast"),
        ModelCog("accurate", "https://accurate.example/v1", "large", "secret/accurate"),
    )

    binding = DeclaredCapabilityResolver().resolve_model(context, models)

    assert binding.endpoint == "https://fast.example/v1"
    assert binding.model_identifier == "small"
    assert binding.auth_ref == "secret/fast"
    assert "token" not in repr(binding)


def test_swapping_model_cog_changes_only_the_resolved_binding():
    context = ContextCog("summarizer", CapabilityRequirement("model"))
    old = ModelCog("old", "https://old.example/v1", "old-model", "secret/old")
    new = ModelCog("new", "https://new.example/v1", "new-model", "secret/new")

    assert DeclaredCapabilityResolver().resolve_model(context, (old,)) != DeclaredCapabilityResolver().resolve_model(
        context, (new,)
    )


def test_model_resolution_fails_on_ambiguous_or_missing_provider():
    context = ContextCog("summarizer", CapabilityRequirement("model"))
    models = (
        ModelCog("one", "https://one.example", "one", "secret/one"),
        ModelCog("two", "https://two.example", "two", "secret/two"),
    )
    resolver = DeclaredCapabilityResolver()

    with pytest.raises(BindingResolutionError):
        resolver.resolve_model(context, models)
    with pytest.raises(BindingResolutionError):
        resolver.resolve_model(ContextCog("summarizer", CapabilityRequirement("image")), models)
