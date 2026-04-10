import os
from typing import Any, Dict, Optional


BUILTIN_PROVIDERS: Dict[str, Dict[str, str]] = {
    "aihubmix": {
        "base_url": "https://aihubmix.com/v1",
        "api_key_env": "LLM_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
    },
}


def resolve_provider(model_str: str, llm_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = llm_cfg or {}
    user_providers = cfg.get("providers") or {}
    provider_name = None
    model_name = model_str

    if "/" in model_str:
        candidate, model_name = model_str.split("/", 1)
        candidate = candidate.lower()
        all_providers = {**BUILTIN_PROVIDERS, **{k.lower(): v for k, v in user_providers.items()}}
        if candidate in all_providers:
            provider_name = candidate

    if provider_name:
        provider = None
        for key, value in user_providers.items():
            if key.lower() == provider_name:
                provider = value
                break
        if not provider:
            provider = BUILTIN_PROVIDERS.get(provider_name, {})
        api_key_env = provider.get("api_key_env", "LLM_API_KEY")
        api_key = provider.get("api_key") or os.environ.get(api_key_env) or ""
        base_url = provider.get("base_url", "")
    else:
        api_key_env = None
        api_key = None
        base_url = None

    return {
        "provider": provider_name,
        "model": model_name,
        "base_url": base_url if base_url else None,
        "api_key": api_key if api_key else None,
        "api_key_env": api_key_env,
    }
