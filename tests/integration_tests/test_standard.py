"""langchain-tests standard integration suite (task 4.3).

Full run is quota-heavy on a subscription — CI runs it nightly/manually:
    pytest tests/integration_tests/test_standard.py -m integration

Known deviations (level B/C parity) are xfailed with reasons below.
"""

from __future__ import annotations

import pytest
from langchain_tests.integration_tests import ChatModelIntegrationTests

from langchain_claude_cli import ChatClaudeCli

pytestmark = pytest.mark.integration


class TestChatClaudeCliStandard(ChatModelIntegrationTests):
    @property
    def chat_model_class(self) -> type[ChatClaudeCli]:
        return ChatClaudeCli

    @property
    def chat_model_params(self) -> dict:
        return {"model": "claude-haiku-4-5", "timeout": 180}

    @property
    def has_tool_calling(self) -> bool:
        return True

    @property
    def has_structured_output(self) -> bool:
        return True

    @property
    def supports_image_inputs(self) -> bool:
        return True

    @property
    def supports_pdf_inputs(self) -> bool:
        return True

    @property
    def returns_usage_metadata(self) -> bool:
        return True

    @property
    def supports_anthropic_inputs(self) -> bool:
        return True

    # cache_read/cache_creation ARE reported in usage_metadata, but the
    # harness detail-check requires provider-specific fixture invocations
    # (invoke_with_cache_read_input) that don't map to CLI-side caching.

    # ── Documented deviations (parity levels B/C) ─────────────

    @pytest.mark.xfail(
        reason="CLI cannot force tool use; tool_choice is instruction+retry (level B)",
        strict=False,
    )
    def test_tool_choice(self, *args, **kwargs):
        super().test_tool_choice(*args, **kwargs)

    @pytest.mark.xfail(
        reason="logprobs not available via the Claude CLI (level C)", strict=False
    )
    def test_logprobs(self, *args, **kwargs):
        super().test_logprobs(*args, **kwargs)
