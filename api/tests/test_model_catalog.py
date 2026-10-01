"""Reading the model catalogue from the serving layer.

The catalogue belongs to the serving pack. This reads it and never forks it:
nothing here writes a model into the hub's own database, so the panel cannot
drift from what the gateway will actually serve.
"""

from __future__ import annotations

import httpx
import pytest

from collab_hub_api.frames.model_catalog import (
    ModelCatalogClient,
    ModelCatalogError,
)


def client(handler, **kwargs) -> ModelCatalogClient:
    return ModelCatalogClient(
        base_url="https://serving.example.com",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_the_catalogue_comes_back_in_the_order_the_serving_layer_gave_it():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "llama-3.1-8b", "owned_by": "hub"},
                    {"id": "mistral-7b", "owned_by": "hub"},
                ],
            },
        )

    models = client(handler).list_models()

    assert [model.id for model in models] == ["llama-3.1-8b", "mistral-7b"]
    assert models[0].owned_by == "hub"


def test_an_unreachable_serving_endpoint_raises_rather_than_answering_empty():
    """An empty catalogue and an unreachable one must not look alike."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ModelCatalogError):
        client(handler).list_models()


def test_a_malformed_payload_is_refused_rather_than_half_read():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": "not a list"})

    with pytest.raises(ModelCatalogError):
        client(handler).list_models()


def test_an_unconfigured_deployment_reports_that_rather_than_failing():
    catalog = ModelCatalogClient(base_url="")

    assert catalog.configured is False
    with pytest.raises(ModelCatalogError):
        catalog.list_models()
