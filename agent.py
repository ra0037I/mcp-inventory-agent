"""ReAct agent that uses the MCI-Inventory-Tools MCP server.

Loop:  Thought (LLM reasons)  ->  Action (LLM emits a tool call)
       -> Observation (MCP server executes the tool, result goes back into the context)
       ... repeated until the LLM gives a final answer.

The agent knows NOTHING about the tools at compile time - they are discovered
dynamically from the MCP server (list_tools) and converted to OpenAI tool schemas.

Usage:
    python agent.py "Which products need to be reordered?"
    python agent.py --demo                      # runs the 3 demo scenarios (for the execution logs)
    python agent.py --transport http --url http://127.0.0.1:8000/mcp "..."
    python agent.py                             # interactive mode
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from llm_provider import create_llm

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
load_dotenv(BASE_DIR / ".env", override=True)  # .env wins over stale terminal variables

SYSTEM_PROMPT = """You are a ReAct sales & inventory assistant for an industrial automation supplier.
You have access to MCP tools for the inventory database, a tiered discount engine and an audit log.

Follow the ReAct pattern strictly:
- Before EVERY tool call write one short line starting with "Thought:" explaining what you need and why.
- Then call exactly the tool(s) you need. Never invent prices, stock levels or discounts - always use tools.
- After receiving observations, reason again ("Thought: ...") and decide the next step.
- Record business-relevant results (quotes, low-stock alerts) with append_audit_log.
- When you have everything, reply with a line starting with "Final Answer:" followed by a concise answer
  (use EUR, 2 decimals)."""

DEMO_SCENARIOS: list[dict[str, Any]] = [
    {
        "title": "Scenario 1 - Inventory lookup + audit (SQL tool, log tool)",
        "prompt": "Which products are at or below their reorder level? List SKU, name, stock and reorder level, "
                  "and write one LOW_STOCK_ALERT audit entry (level WARNING) that lists the affected SKUs.",
    },
    {
        "title": "Scenario 2 - Quote via server-provided MCP prompt (all three tools)",
        "server_prompt": ("sales_quote_workflow", {
            "customer": "Alpenwerk GmbH (customer tier: gold)",
            "request": "300 units of the inductive proximity sensor M8",
        }),
    },
    {
        "title": "Scenario 3 - Category cap + stock shortfall (formula engine edge case)",
        "prompt": "A platinum customer wants 500 units of SAF-RLY01. What discount do they get, is the category "
                  "cap applied, and can we deliver from stock? Log the quote as QUOTE_CREATED and, if stock is "
                  "insufficient, also a LOW_STOCK_ALERT.",
    },
]


# ---------------------------------------------------------------------------
# Logging (console + file per run)
# ---------------------------------------------------------------------------
def setup_logging() -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"agent_run_{datetime.now():%Y%m%d_%H%M%S}.log"
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    root = logging.getLogger("agent")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    for noisy in ("httpx", "mcp", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file


log = logging.getLogger("agent")


def _short(text: str, n: int = 1500) -> str:
    return text if len(text) <= n else text[:n] + f" ... [{len(text) - n} more chars]"


# ---------------------------------------------------------------------------
# MCP connection
# ---------------------------------------------------------------------------
@asynccontextmanager
async def mcp_session(transport: str, url: str | None):
    if transport == "http":
        url = url or os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
        log.info("[MCP] connecting via streamable HTTP to %s", url)
        async with streamable_http_client(url) as streams:  # SDK 1.x yields 3 values, 2.x yields 2
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session
    else:
        params = StdioServerParameters(command=sys.executable, args=[str(BASE_DIR / "server.py")], cwd=str(BASE_DIR))
        log.info("[MCP] spawning server via stdio: %s server.py", Path(sys.executable).name)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Field access compatible with MCP SDK 1.x (camelCase) and 2.x (snake_case)."""
    for n in names:
        try:
            v = getattr(obj, n)
        except AttributeError:
            continue
        if v is not None:
            return v
    return default


def mcp_tools_to_openai(tools) -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description or "",
            "parameters": _attr(t, "input_schema", "inputSchema", default={"type": "object", "properties": {}}),
        },
    } for t in tools.tools]


async def discover(session: ClientSession) -> list[dict]:
    tools = await session.list_tools()
    resources = await session.list_resources()
    templates = await session.list_resource_templates()
    prompts = await session.list_prompts()
    log.info("[DISCOVERY] tools:              %s", [t.name for t in tools.tools])
    log.info("[DISCOVERY] resources:          %s", [str(r.uri) for r in resources.resources])
    log.info("[DISCOVERY] resource templates: %s",
             [_attr(t, "uri_template", "uriTemplate") for t in _attr(templates, "resource_templates", "resourceTemplates", default=[])])
    log.info("[DISCOVERY] prompts:            %s", [p.name for p in prompts.prompts])
    return mcp_tools_to_openai(tools)


def _tool_result_text(result) -> str:
    parts = [c.text for c in (result.content or []) if getattr(c, "text", None)]
    text = "\n".join(parts) if parts else json.dumps(_attr(result, "structured_content", "structuredContent"))
    return ("ERROR: " + text) if _attr(result, "is_error", "isError", default=False) else text


def _msg_to_dict(msg: Any) -> dict:
    """Serialise the assistant message with only the fields every provider accepts."""
    return {
        "role": "assistant",
        "content": msg.content or "",
        "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
            for tc in msg.tool_calls
        ],
    }


# ---------------------------------------------------------------------------
# ReAct loop
# ---------------------------------------------------------------------------
async def react(session: ClientSession, llm, tools: list[dict], user_prompt: str, max_steps: int) -> str:
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    log.info("[USER] %s", user_prompt)

    for step in range(1, max_steps + 1):
        log.info("---- step %d: querying LLM (%s / %s) ----", step, llm.provider, llm.model)
        msg = llm.chat(messages, tools)
        content = (msg.content or "").strip()

        if not msg.tool_calls:
            log.info("[FINAL] %s", content)
            return content

        if content:
            log.info("[THOUGHT] %s", content)
        messages.append(_msg_to_dict(msg))

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            log.info("[ACTION] %s(%s)", name, json.dumps(args, ensure_ascii=False))
            try:
                result = await session.call_tool(name, arguments=args)
                observation = _tool_result_text(result)
            except Exception as exc:  # tool errors are fed back so the LLM can recover
                observation = f"ERROR executing {name}: {exc}"
            log.info("[OBSERVATION] %s", _short(observation))
            messages.append({"role": "tool", "tool_call_id": tc.id, "name": name, "content": observation})

    log.info("[STOP] max_steps (%d) reached without final answer", max_steps)
    return "(no final answer - max steps reached)"


async def run(prompts: list[dict], transport: str, url: str | None, max_steps: int) -> None:
    llm = create_llm()
    log.info("[CONFIG] LLM provider=%s model=%s transport=%s", llm.provider, llm.model, transport)
    async with mcp_session(transport, url) as session:
        tools = await discover(session)

        for item in prompts:
            log.info("=" * 100)
            log.info(item.get("title", "Request"))
            log.info("=" * 100)
            if "server_prompt" in item:  # use the MCP *prompt* primitive as task template
                name, args = item["server_prompt"]
                p = await session.get_prompt(name, arguments=args)
                user_prompt = p.messages[0].content.text
                log.info("[MCP PROMPT] fetched template '%s' with %s", name, args)
            else:
                user_prompt = item["prompt"]
            await react(session, llm, tools, user_prompt, max_steps)

        # Read back the audit log through the MCP *resource* primitive
        res = await session.read_resource("audit://events/5")
        log.info("-" * 100)
        log.info("[RESOURCE] audit://events/5 (last 5 audit entries):\n%s", res.contents[0].text)


def main() -> None:
    ap = argparse.ArgumentParser(description="ReAct agent for the MCI Inventory MCP server")
    ap.add_argument("prompt", nargs="*", help="question for the agent (omit for interactive mode)")
    ap.add_argument("--demo", action="store_true", help="run the predefined demo scenarios")
    ap.add_argument("--transport", choices=["stdio", "http"], default=os.getenv("MCP_TRANSPORT", "stdio"))
    ap.add_argument("--url", default=None, help="MCP server URL for --transport http")
    ap.add_argument("--max-steps", type=int, default=int(os.getenv("MAX_REACT_STEPS", "10")))
    ap.add_argument("--provider", choices=["openai", "litellm", "azure", "ollama", "mock"],
                    help="override LLM_PROVIDER from .env for this run")
    args = ap.parse_args()
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider

    log_file = setup_logging()
    if args.demo:
        prompts = DEMO_SCENARIOS
    elif args.prompt:
        prompts = [{"title": "Request", "prompt": " ".join(args.prompt)}]
    else:
        q = input("Ask the agent: ").strip()
        prompts = [{"title": "Request", "prompt": q}]

    asyncio.run(run(prompts, args.transport, args.url, args.max_steps))
    log.info("[LOG] execution log written to %s", log_file.relative_to(BASE_DIR))


if __name__ == "__main__":
    main()
