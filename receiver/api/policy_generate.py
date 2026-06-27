"""Generate CEL policy from plain English description.

Uses an external AI API (Anthropic or OpenAI) to convert a natural
language policy description into a valid CEL expression.

Requires STRATHON_AI_API_KEY environment variable. If not configured,
returns 400 with instructions to use the CEL reference instead.
"""

from __future__ import annotations

import os
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import auth as auth_mod
from ._deps import require_scope

router = APIRouter(prefix="/v1/policies", tags=["policies"])
logger = logging.getLogger("strathon.receiver.policy_generate")

CEL_SYSTEM_PROMPT = """You are a Strathon CEL policy generator. Convert the user's
plain English description into a CEL expression for the Strathon AI agent firewall.

Available attributes in every span:
- attrs["gen_ai.tool.name"] — tool being called (string)
- attrs["strathon.tool.args"] — tool input arguments, as a JSON string
- attrs["gen_ai.agent.name"] — agent name (string)
- attrs["gen_ai.prompt"] — prompt text, on LLM spans (string)
- attrs["gen_ai.completion"] — response text, on LLM spans (string)
- attrs["gen_ai.request.model"] — model name (string)
- attrs["gen_ai.usage.cost"] — dollar cost of the call, on LLM spans (float)
- attrs["gen_ai.usage.total_tokens"] — total token count (int)
- attrs["gen_ai.workflow.name"] — workflow name (string)
- now — current UTC timestamp

CEL syntax: == for equality, && for AND, || for OR, ! for NOT,
"in" for list membership, .matches("regex"), .contains("substring"),
.startsWith("prefix"), .endsWith("suffix").

Respond with ONLY a JSON object:
{"match_expression": "<CEL expression>", "action": "<block|alert|log|require_approval>", "name": "<kebab-case-policy-name>"}

No explanation. No markdown. Only the JSON object."""


class GenerateRequest(BaseModel):
    description: str = Field(min_length=5, max_length=1000)
    model_config = {"extra": "forbid"}


class GenerateResponse(BaseModel):
    match_expression: str
    action: str
    name: str
    description: str


@router.post("/generate", response_model=GenerateResponse)
async def generate_policy_from_english(
    body: GenerateRequest,
    ctx: auth_mod.ApiKeyContext = Depends(require_scope(auth_mod.SCOPE_POLICIES_WRITE)),
):
    """Generate a CEL policy from a plain English description.

    Requires the ``policies:write`` scope. This endpoint calls a paid LLM
    API using the server's STRATHON_AI_API_KEY, so it must be authenticated
    to prevent credit-burning abuse and use as an open LLM proxy.
    """
    ai_key = os.environ.get("STRATHON_AI_API_KEY")
    if not ai_key:
        raise HTTPException(
            400,
            "STRATHON_AI_API_KEY not configured. Set this environment variable "
            "with your Anthropic or OpenAI API key to enable AI policy generation. "
            "Alternatively, use the CEL reference: getstrathon.com/docs/cel-reference",
        )

    import json

    # Try Anthropic first (sk-ant- or starts with sk- and has anthropic key format).
    try:
        import httpx
        if ai_key.startswith("sk-ant-"):
            resp = await _call_anthropic(ai_key, body.description)
        else:
            resp = await _call_openai(ai_key, body.description)

        parsed = json.loads(resp)
        return GenerateResponse(
            match_expression=parsed.get("match_expression", ""),
            action=parsed.get("action", "block"),
            name=parsed.get("name", "generated-policy"),
            description=body.description,
        )
    except json.JSONDecodeError:
        raise HTTPException(500, "AI returned invalid JSON. Try rephrasing.")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"AI API error: {exc.response.status_code}")
    except Exception:
        logger.exception("Policy generation failed")
        raise HTTPException(500, "Policy generation failed. Please try again.")


async def _call_anthropic(api_key: str, description: str) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "system": CEL_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": description}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


async def _call_openai(api_key: str, description: str) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 300,
                "messages": [
                    {"role": "system", "content": CEL_SYSTEM_PROMPT},
                    {"role": "user", "content": description},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
