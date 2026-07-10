"""langchain-tests standard unit-test suite."""

from langchain_tests.unit_tests import ChatModelUnitTests

from langchain_claude_cli import ChatClaudeCli


class TestChatClaudeCliStandardUnit(ChatModelUnitTests):
    @property
    def chat_model_class(self) -> type[ChatClaudeCli]:
        return ChatClaudeCli

    @property
    def chat_model_params(self) -> dict:
        return {"model": "claude-haiku-4-5"}

    @property
    def has_tool_calling(self) -> bool:
        return True

    @property
    def has_structured_output(self) -> bool:
        return True
