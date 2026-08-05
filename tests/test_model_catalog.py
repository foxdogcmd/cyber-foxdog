from foxdog.llm.client import ModelEndpoint
from foxdog.llm.model_catalog import model_urls


def test_model_urls_deepseek_anthropic_uses_root_models_endpoint():
    endpoint = ModelEndpoint(base_url="https://api.deepseek.com/anthropic", protocol="anthropic")
    assert model_urls(endpoint) == ["https://api.deepseek.com/models"]


def test_model_urls_kimi_code_tries_versioned_and_direct_endpoints():
    endpoint = ModelEndpoint(base_url="https://api.kimi.com/coding/", protocol="anthropic")
    assert model_urls(endpoint) == [
        "https://api.kimi.com/coding/v1/models",
        "https://api.kimi.com/coding/models",
    ]
