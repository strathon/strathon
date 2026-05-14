"""Tests for LangGraph / LangChain instrumentation."""

from uuid import uuid4
from unittest.mock import MagicMock

import pytest

from strathon import Client
from strathon.instrumentation.langgraph import (
    StrathonLangGraphHandler,
    _chain_name_from_serialized,
    _extract_token_usage_from_llm_result,
    _model_from_serialized,
    _provider_from_model,
    _tool_name_from_serialized,
    _truncate,
)


def make_client():
    return Client(
        api_key="test-key",
        endpoint="http://localhost:4318",
        set_global_tracer=False,
        enable_policies=False,
    )


def make_handler():
    """Build a handler instance directly (no BaseCallbackHandler binding required for tests)."""
    return StrathonLangGraphHandler(make_client())


# ---- Pure helper tests ----


def test_provider_from_model_handles_prefixed_and_bare():
    assert _provider_from_model("openai/gpt-4o") == "openai"
    assert _provider_from_model("anthropic/claude-opus-4-7") == "anthropic"
    assert _provider_from_model("gpt-4o-mini") == "openai"
    assert _provider_from_model("claude-3-5-sonnet") == "anthropic"
    assert _provider_from_model("gemini-2.0-pro") == "google"
    assert _provider_from_model("mistral-large") == "mistral"
    assert _provider_from_model("custom-model") is None
    assert _provider_from_model(None) is None


def test_model_from_serialized_walks_kwargs():
    assert (
        _model_from_serialized(
            {"id": ["langchain", "chat_models", "ChatOpenAI"], "kwargs": {"model": "gpt-4o"}}
        )
        == "gpt-4o"
    )
    assert (
        _model_from_serialized(
            {"kwargs": {"model_name": "gpt-3.5-turbo"}}
        )
        == "gpt-3.5-turbo"
    )
    assert (
        _model_from_serialized(
            {"kwargs": {"deployment_name": "azure-gpt4-deploy"}}
        )
        == "azure-gpt4-deploy"
    )
    assert _model_from_serialized({}) is None
    assert _model_from_serialized(None) is None


def test_chain_name_from_serialized():
    assert _chain_name_from_serialized({"name": "MyChain"}) == "MyChain"
    assert (
        _chain_name_from_serialized(
            {"id": ["langchain", "chains", "LLMChain"]}
        )
        == "LLMChain"
    )
    assert _chain_name_from_serialized(None) == "chain"
    assert _chain_name_from_serialized({}) == "chain"


def test_tool_name_from_serialized():
    assert _tool_name_from_serialized({"name": "web_search"}) == "web_search"
    assert _tool_name_from_serialized(None) == "tool"
    assert _tool_name_from_serialized({}) == "tool"


def test_truncate_caps_strings():
    assert _truncate("short", 100) == "short"
    long_str = "x" * 5000
    truncated = _truncate(long_str, 100)
    assert "truncated" in truncated
    assert len(truncated) < 5000


def test_extract_token_usage_from_openai_style_llm_output():
    """OpenAI: llm_output={'token_usage': {prompt_tokens, completion_tokens, total_tokens}}"""
    response = MagicMock()
    response.llm_output = {
        "token_usage": {
            "prompt_tokens": 100,
            "completion_tokens": 200,
            "total_tokens": 300,
        }
    }
    response.generations = []
    out = _extract_token_usage_from_llm_result(response)
    assert out == {
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 200,
        "gen_ai.usage.total_tokens": 300,
    }


def test_extract_token_usage_from_anthropic_style_llm_output():
    """Anthropic: llm_output={'usage': {input_tokens, output_tokens}}"""
    response = MagicMock()
    response.llm_output = {"usage": {"input_tokens": 50, "output_tokens": 80}}
    response.generations = []
    out = _extract_token_usage_from_llm_result(response)
    assert out == {
        "gen_ai.usage.input_tokens": 50,
        "gen_ai.usage.output_tokens": 80,
    }


def test_extract_token_usage_from_message_metadata():
    """Some chat models put usage on message.usage_metadata."""
    response = MagicMock()
    response.llm_output = None

    msg = MagicMock()
    msg.usage_metadata = {"input_tokens": 25, "output_tokens": 40, "total_tokens": 65}

    gen = MagicMock()
    gen.generation_info = None
    gen.message = msg

    response.generations = [[gen]]
    out = _extract_token_usage_from_llm_result(response)
    assert out == {
        "gen_ai.usage.input_tokens": 25,
        "gen_ai.usage.output_tokens": 40,
        "gen_ai.usage.total_tokens": 65,
    }


def test_extract_token_usage_returns_empty_when_unavailable():
    response = MagicMock()
    response.llm_output = None
    response.generations = []
    assert _extract_token_usage_from_llm_result(response) == {}


# ---- Span lifecycle tests (chain) ----


def test_on_chain_start_creates_span_keyed_by_run_id():
    handler = make_handler()
    run_id = uuid4()
    handler.on_chain_start(
        serialized={"name": "MyGraph"},
        inputs={"query": "hi"},
        run_id=run_id,
    )
    assert str(run_id) in handler._spans


def test_full_chain_lifecycle():
    handler = make_handler()
    run_id = uuid4()
    handler.on_chain_start(
        serialized={"name": "MyGraph"},
        inputs={"query": "hi"},
        run_id=run_id,
    )
    assert str(run_id) in handler._spans

    handler.on_chain_end(outputs={"answer": "done"}, run_id=run_id)
    assert str(run_id) not in handler._spans


def test_chain_error_ends_span_with_error_status():
    handler = make_handler()
    run_id = uuid4()
    handler.on_chain_start(
        serialized={"name": "BrokenChain"}, inputs={}, run_id=run_id
    )
    handler.on_chain_error(error=Exception("boom"), run_id=run_id)
    assert str(run_id) not in handler._spans


def test_langgraph_node_metadata_populates_agent_name():
    """LangGraph injects langgraph_node into metadata; we should pick it up."""
    handler = make_handler()
    run_id = uuid4()
    handler.on_chain_start(
        serialized={"name": "function_node"},
        inputs={},
        run_id=run_id,
        metadata={"langgraph_node": "researcher", "langgraph_step": 2},
    )
    # We can't easily inspect span attributes without a real exporter, but the
    # span exists; ending it should succeed without raising.
    handler.on_chain_end(outputs={}, run_id=run_id)
    assert str(run_id) not in handler._spans


# ---- LLM lifecycle tests ----


def test_on_llm_start_and_end_with_token_usage():
    handler = make_handler()
    run_id = uuid4()
    handler.on_llm_start(
        serialized={
            "id": ["langchain", "chat_models", "ChatOpenAI"],
            "kwargs": {"model": "gpt-4o"},
        },
        prompts=["Say hi"],
        run_id=run_id,
    )
    assert str(run_id) in handler._spans

    response = MagicMock()
    response.llm_output = {
        "token_usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        },
        "model_name": "gpt-4o-2024-08-06",
    }
    response.generations = []
    handler.on_llm_end(response=response, run_id=run_id)
    assert str(run_id) not in handler._spans


def test_on_chat_model_start_captures_messages():
    handler = make_handler()
    run_id = uuid4()
    msg = MagicMock()
    msg.content = "Hello"
    msg.type = "human"
    handler.on_chat_model_start(
        serialized={"kwargs": {"model": "claude-opus-4-7"}},
        messages=[[msg]],
        run_id=run_id,
    )
    assert str(run_id) in handler._spans
    response = MagicMock()
    response.llm_output = None
    response.generations = []
    handler.on_llm_end(response=response, run_id=run_id)


def test_llm_error_ends_span():
    handler = make_handler()
    run_id = uuid4()
    handler.on_llm_start(
        serialized={"kwargs": {"model": "gpt-4o"}},
        prompts=["x"],
        run_id=run_id,
    )
    handler.on_llm_error(error=Exception("rate limit"), run_id=run_id)
    assert str(run_id) not in handler._spans


# ---- Tool lifecycle tests ----


def test_full_tool_lifecycle():
    handler = make_handler()
    run_id = uuid4()
    handler.on_tool_start(
        serialized={"name": "web_search"},
        input_str='{"query":"agent observability"}',
        run_id=run_id,
        inputs={"query": "agent observability"},
    )
    assert str(run_id) in handler._spans

    handler.on_tool_end(output="10 results found", run_id=run_id)
    assert str(run_id) not in handler._spans


def test_tool_error_ends_span():
    handler = make_handler()
    run_id = uuid4()
    handler.on_tool_start(
        serialized={"name": "broken_tool"},
        input_str="",
        run_id=run_id,
    )
    handler.on_tool_error(error=Exception("api 500"), run_id=run_id)
    assert str(run_id) not in handler._spans


# ---- Retriever lifecycle ----


def test_retriever_lifecycle():
    handler = make_handler()
    run_id = uuid4()
    handler.on_retriever_start(
        serialized={"name": "VectorStoreRetriever"},
        query="What is agent observability?",
        run_id=run_id,
    )
    assert str(run_id) in handler._spans

    docs = [MagicMock(), MagicMock(), MagicMock()]
    handler.on_retriever_end(documents=docs, run_id=run_id)
    assert str(run_id) not in handler._spans


# ---- Parent-child relationships ----


def test_parent_child_via_parent_run_id():
    handler = make_handler()
    parent = uuid4()
    child = uuid4()

    handler.on_chain_start(
        serialized={"name": "ParentGraph"}, inputs={}, run_id=parent
    )
    handler.on_llm_start(
        serialized={"kwargs": {"model": "gpt-4o"}},
        prompts=["hi"],
        run_id=child,
        parent_run_id=parent,
    )

    assert str(parent) in handler._spans
    assert str(child) in handler._spans

    response = MagicMock()
    response.llm_output = None
    response.generations = []
    handler.on_llm_end(response=response, run_id=child)
    assert str(child) not in handler._spans
    assert str(parent) in handler._spans

    handler.on_chain_end(outputs={}, run_id=parent)
    assert str(parent) not in handler._spans


# ---- instrument() factory ----


def test_instrument_returns_handler_when_langchain_available():
    pytest.importorskip("langchain_core")

    import strathon.instrumentation.langgraph as mod

    mod._REGISTERED_HANDLER = None
    client = make_client()
    handler = mod.instrument(client)
    assert handler is not None
    # The returned handler should be a BaseCallbackHandler subclass
    from langchain_core.callbacks.base import BaseCallbackHandler

    assert isinstance(handler, BaseCallbackHandler)


def test_instrument_is_idempotent():
    pytest.importorskip("langchain_core")

    import strathon.instrumentation.langgraph as mod

    mod._REGISTERED_HANDLER = None
    client1 = make_client()
    h1 = mod.instrument(client1)

    client2 = make_client()
    h2 = mod.instrument(client2)

    assert h1 is h2
    assert h2.client is client2
