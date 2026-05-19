"""Claude LLM wrapper for the voice path.

Sends transcribed user text + tool declarations to Claude, executes any
tool_use blocks via a caller-supplied dispatcher, and loops until Claude
returns a pure-text response.  Maintains per-session conversation history
so Tolly can handle follow-ups ("and the fuel for that?") without each
turn starting fresh.

History resets on process restart.  Cap is conservative (20 messages, ~10
turns) to keep context windows bounded for the boat's pay-per-token use.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import anthropic


LOGGER = logging.getLogger(__name__)

ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
ToolStartCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


# Max tool-use rounds per user turn.  Claude usually finishes a route plan
# in 1-2 rounds.  10 is a safety ceiling; if we ever hit it something is
# pathological.
MAX_TOOL_ROUNDS = 10

# Max model-output tokens per Claude call.  Tolly's responses are short
# (~50 tokens for spoken summary), but tool_use blocks can balloon when
# arguments are large.  1024 is roomy.
MAX_TOKENS = 1024


class ClaudeLLM:
    """One Claude conversation per process, with tools."""

    def __init__(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
        *,
        fallback_model: str | None = None,
        max_history_messages: int = 20,
        request_timeout_s: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        # Default Anthropic SDK timeout is 600s with 2 retries — would hang
        # a /talk for 25 min when offline.  Tight per-request cap + one
        # retry keeps the boat usable when the tether drops.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=request_timeout_s,
            max_retries=max_retries,
        )
        self._model = model
        self._fallback_model = fallback_model
        self._system_prompt = system_prompt
        self._tools = tools
        self._max_history = max_history_messages
        self._history: list[dict[str, Any]] = []

    def reset_history(self) -> None:
        self._history = []

    @property
    def history_length(self) -> int:
        return len(self._history)

    async def ping(self) -> bool:
        """Free reachability check via models.list().  Avoid messages.create()
        here: /healthz polls every 60s and each messages.create call burns
        tokens (~$7/month idle).  models.list() is metadata-only and free."""
        try:
            await self._client.models.list(limit=1)
            return True
        except anthropic.APIError as err:
            LOGGER.warning("Claude ping failed: %s", err)
            return False

    async def turn(
        self,
        user_text: str,
        dispatcher: ToolDispatcher,
        *,
        on_tool_start: ToolStartCallback | None = None,
    ) -> str:
        """Run one user utterance through Claude, executing any tool_use
        rounds via the dispatcher.  Returns the final assistant text."""
        if not user_text.strip():
            return ""

        self._history.append({"role": "user", "content": user_text})

        final_text = ""
        for round_idx in range(MAX_TOOL_ROUNDS):
            try:
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=MAX_TOKENS,
                    system=self._system_prompt,
                    tools=self._tools,
                    messages=self._history,
                )
            except anthropic.RateLimitError:
                if self._fallback_model and round_idx == 0:
                    LOGGER.warning(
                        "Claude rate-limited on %s; falling back to %s",
                        self._model, self._fallback_model,
                    )
                    response = await self._client.messages.create(
                        model=self._fallback_model,
                        max_tokens=MAX_TOKENS,
                        system=self._system_prompt,
                        tools=self._tools,
                        messages=self._history,
                    )
                else:
                    raise

            # Record the assistant's content verbatim — Claude needs to see
            # its own previous tool_use blocks in subsequent turns.
            content = [self._block_to_dict(b) for b in response.content]
            self._history.append({"role": "assistant", "content": content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_parts = [b.text for b in response.content if b.type == "text"]
            final_text = " ".join(t.strip() for t in text_parts if t.strip())

            if not tool_uses:
                self._trim_history()
                LOGGER.info(
                    "Claude turn done (round %d, history=%d): %s",
                    round_idx + 1, len(self._history),
                    final_text[:120],
                )
                return final_text

            LOGGER.info(
                "Claude tool round %d: %s",
                round_idx + 1,
                ", ".join(f"{b.name}({_brief_args(b.input)})" for b in tool_uses),
            )
            # Fire the on_tool_start callback BEFORE awaiting the tools so the
            # caller (voice session) can play a "working on it" ack for slow
            # tools.  Sequentially-awaited so the ack finishes before the
            # actual tool work starts blocking the speaker queue.
            if on_tool_start is not None:
                for b in tool_uses:
                    await on_tool_start(b.name, dict(b.input))
            results = await asyncio.gather(*(
                self._run_tool(b, dispatcher) for b in tool_uses
            ))
            self._history.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": json.dumps(r, default=str),
                    }
                    for b, r in zip(tool_uses, results)
                ],
            })

        LOGGER.warning(
            "Claude exceeded MAX_TOOL_ROUNDS=%d; returning last text",
            MAX_TOOL_ROUNDS,
        )
        self._trim_history()
        return final_text or "Sorry, I got stuck working on that. Try again?"

    async def _run_tool(
        self,
        tool_use_block: Any,
        dispatcher: ToolDispatcher,
    ) -> dict[str, Any]:
        try:
            return await dispatcher(tool_use_block.name, dict(tool_use_block.input))
        except (TypeError, ValueError, KeyError) as err:
            LOGGER.warning(
                "Tool %s rejected args (%s): %r",
                tool_use_block.name, err, tool_use_block.input,
            )
            return {"error": f"{tool_use_block.name} could not be run: {err}"}
        except Exception as err:
            LOGGER.exception("Tool %s raised", tool_use_block.name)
            return {"error": f"{tool_use_block.name} failed: {err}"}

    @staticmethod
    def _block_to_dict(block: Any) -> dict[str, Any]:
        """Convert an Anthropic SDK content block back to the dict shape
        the API accepts on the way IN."""
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        # Anthropic occasionally returns thinking/server_tool_use blocks too.
        # Fall back to model_dump if available.
        if hasattr(block, "model_dump"):
            return block.model_dump()
        raise ValueError(f"Unknown content block type: {block.type!r}")

    def _trim_history(self) -> None:
        """Keep history under cap.  A logical turn can span 2, 4, 6+ messages
        depending on how many tool rounds Claude did:
            user(text) → assistant(text)                          # plain
            user(text) → assistant(tool_use) → user(tool_result)
                       → assistant(text)                          # 1 round
            user(text) → assistant(tool_use) → user(tool_result)
                       → assistant(tool_use) → user(tool_result)
                       → assistant(text)                          # 2 rounds
        We drop whole logical turns from the head (until under cap or empty),
        never split a turn.  A turn ends at the assistant message that has
        NO tool_use blocks — that's the final-text reply to the user."""
        while len(self._history) > self._max_history:
            # Pop until we've consumed one full user→...→assistant(text) turn.
            popped_any = False
            while self._history:
                msg = self._history.pop(0)
                popped_any = True
                if msg["role"] == "assistant" and not _has_tool_use(msg.get("content")):
                    break
            if not popped_any:
                break


def _has_tool_use(content: Any) -> bool:
    """True if the message content (list of block dicts) contains a tool_use."""
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_use"
        for b in content
    )


def _brief_args(args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    bits = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        bits.append(f"{k}={s}")
    return ", ".join(bits)
