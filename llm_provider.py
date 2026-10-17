"""Configurable LLM backend (selected via LLM_PROVIDER in .env).

All real providers speak the OpenAI Chat Completions API with tool calling:

  litellm  - LiteLLM proxy (lecture setup)        LITELLM_API_BASE, LITELLM_KEY, LITELLM_DEFAULT_MODEL
  azure    - Azure OpenAI / Azure AI Foundry      AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY,
                                                  AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION
  openai   - OpenAI directly                      OPENAI_API_KEY, OPENAI_MODEL
  ollama   - local model via Ollama               OLLAMA_BASE_URL, OLLAMA_MODEL
  mock     - offline scripted LLM, ONLY for smoke-testing the pipeline without any key
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass
class LLM:
    client: Any
    model: str
    provider: str

    def chat(self, messages: list[dict], tools: list[dict]) -> Any:
        """Returns the assistant message (OpenAI SDK object or compatible)."""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        temp = os.getenv("LLM_TEMPERATURE", "").strip()
        if temp:  # leave empty for reasoning models that reject a temperature
            kwargs["temperature"] = float(temp)
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message


def create_llm() -> LLM:
    provider = os.getenv("LLM_PROVIDER", "litellm").strip().lower()

    if provider == "mock":
        return LLM(client=_MockClient(), model="mock-react-v1", provider="mock")

    from openai import AzureOpenAI, OpenAI

    if provider == "litellm":
        return LLM(
            client=OpenAI(base_url=_req("LITELLM_API_BASE"), api_key=_req("LITELLM_KEY")),
            model=os.getenv("LITELLM_DEFAULT_MODEL", "gpt-4o-mini"),
            provider=provider,
        )
    if provider == "azure":
        return LLM(
            client=AzureOpenAI(
                azure_endpoint=_req("AZURE_OPENAI_ENDPOINT"),
                api_key=_req("AZURE_OPENAI_KEY"),
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            ),
            model=_req("AZURE_OPENAI_DEPLOYMENT"),
            provider=provider,
        )
    if provider == "openai":
        return LLM(
            client=OpenAI(api_key=_req("OPENAI_API_KEY")),
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            provider=provider,
        )
    if provider == "ollama":
        return LLM(
            client=OpenAI(base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"), api_key="ollama"),
            model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
            provider=provider,
        )
    raise ValueError(f"Unknown LLM_PROVIDER '{provider}'. Use litellm | azure | openai | ollama | mock.")


def _req(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Environment variable {name} is not set (see .env.example)")
    return val


# ---------------------------------------------------------------------------
# Offline mock: a tiny rule-based "LLM" that follows the ReAct protocol.
# It lets you verify MCP wiring, tool execution and logging without an API key.
# It is NOT intelligent - use a real provider for the homework execution logs.
# ---------------------------------------------------------------------------
class _MockClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @staticmethod
    def _tc(name: str, args: dict) -> Any:
        return SimpleNamespace(id=f"call_{uuid.uuid4().hex[:8]}", type="function",
                               function=SimpleNamespace(name=name, arguments=json.dumps(args)))

    @staticmethod
    def _msg(content: str | None, tool_calls: list | None = None) -> Any:
        m = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls or None)
        return m

    def _create(self, model, messages, tools=None, **_):
        user = next(m["content"] for m in messages if _role(m) == "user").lower()
        done = [m.get("name", "") for m in messages if _role(m) == "tool"]
        observations = [m["content"] for m in messages if _role(m) == "tool"]
        sku_m = re.search(r"\b([a-z]{3}-[a-z0-9]{3,5})\b", user)
        qty_m = re.search(r"(\d+)\s*(?:units|pcs|pieces|x|stück)", user)
        tier = next((t for t in ("platinum", "gold", "silver") if t in user), "standard")

        if "query_inventory" not in done:
            if sku_m:
                args = {"sku": sku_m.group(1).upper()}
            elif "proximity" in user:
                args = {"search": "proximity"}
            else:
                args = {"low_stock_only": True}
            msg = self._msg("Thought: I need inventory data first.", [self._tc("query_inventory", args)])
        elif qty_m and "calculate_tiered_discount" not in done:
            args = {"quantity": int(qty_m.group(1)), "customer_tier": tier}
            found = re.search(r'"sku":"([A-Z0-9-]+)"', observations[0])
            if sku_m or found:
                args["sku"] = sku_m.group(1).upper() if sku_m else found.group(1)
            msg = self._msg("Thought: Now compute the tiered discount.", [self._tc("calculate_tiered_discount", args)])
        elif "append_audit_log" not in done:
            msg = self._msg("Thought: Record the result in the audit log.", [self._tc(
                "append_audit_log", {"event_type": "MOCK_RUN", "message": f"Processed request: {user[:80]}"})])
        else:
            msg = self._msg("Final Answer (mock): " + " | ".join(o[:160] for o in observations[:-1]))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


def _role(m: Any) -> str:
    return m["role"] if isinstance(m, dict) else getattr(m, "role", "")
