"""OpenAI-compatible LLM client for subtitle refinement."""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LLMProvider:
    name: str
    base_url: str
    api_key: str
    model: str
    max_tokens: int = 4096
    temperature: float = 0.3


class LLMClient:
    """Calls an OpenAI-compatible chat completion endpoint."""

    def __init__(self, provider: LLMProvider):
        self._provider = provider

    def call(self, prompt: str, timeout: float = 120.0) -> str:
        """Send a single user message and return the assistant response."""
        url = f"{self._provider.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._provider.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._provider.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self._provider.max_tokens,
            "temperature": self._provider.temperature,
        }

        response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()

        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

    def test_connection(self) -> tuple[bool, str]:
        """Test connectivity. Returns (success, message)."""
        try:
            result = self.call("hi", timeout=30.0)
            return True, f"连接成功: {result[:50]}"
        except httpx.TimeoutException:
            return False, "连接超时"
        except httpx.HTTPStatusError as e:
            return False, f"HTTP {e.response.status_code}: {e.response.text[:100]}"
        except Exception as e:
            return False, f"连接失败: {e}"


def provider_from_dict(d: dict) -> LLMProvider:
    return LLMProvider(
        name=d.get("name", ""),
        base_url=d.get("base_url", ""),
        api_key=d.get("api_key", ""),
        model=d.get("model", ""),
        max_tokens=d.get("max_tokens", 4096),
        temperature=d.get("temperature", 0.3),
    )


def provider_to_dict(p: LLMProvider) -> dict:
    return {
        "name": p.name,
        "base_url": p.base_url,
        "api_key": p.api_key,
        "model": p.model,
        "max_tokens": p.max_tokens,
        "temperature": p.temperature,
    }
