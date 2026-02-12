"""
Incremental JSON parser for streaming LLM responses.
Yields complete obligation objects as they arrive in the token stream (so the client sees them over time).
"""
import json
import re
import logging
from typing import AsyncIterator, Dict, Any, List

logger = logging.getLogger(__name__)


def _strip_markdown_wrapper(text: str) -> str:
    """Strip ```json or ``` wrapper if present."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


async def parse_obligations_stream(token_stream: AsyncIterator[str]) -> AsyncIterator[Dict[str, Any]]:
    """
    Parse streaming JSON from LLM and yield complete obligation objects as they're detected.
    Uses ijson for incremental parsing so each obligation is yielded as soon as it is complete
    in the stream (client sees results over time). Falls back to buffer-then-parse if ijson
    is unavailable or fails.
    """
    try:
        import ijson
    except ImportError:
        # Fallback: buffer full response then parse
        async for ob in _parse_obligations_buffered(token_stream):
            yield ob
        return

    events = ijson.sendable_list()
    try:
        coro = ijson.items_coro(events, "results.item")
    except Exception as e:
        logger.warning(f"ijson items_coro failed, using buffered parse: {e}")
        async for ob in _parse_obligations_buffered(token_stream):
            yield ob
        return

    buffer = ""
    started = False
    async for chunk in token_stream:
        buffer += chunk
        # Skip markdown wrapper until we see the first '{'
        if not started:
            if "{" not in buffer:
                continue
            start = buffer.index("{")
            to_send = buffer[start:]
            buffer = ""
            started = True
            try:
                coro.send(to_send.encode("utf-8"))
            except Exception as e:
                logger.debug("ijson send: %s", e)
        else:
            if buffer:
                try:
                    coro.send(buffer.encode("utf-8"))
                except Exception as e:
                    logger.debug("ijson send: %s", e)
                buffer = ""
        for item in list(events):
            if isinstance(item, dict):
                yield item
        events.clear()

    if buffer:
        try:
            coro.send(buffer.encode("utf-8"))
        except Exception:
            pass
        for item in list(events):
            if isinstance(item, dict):
                yield item


async def _parse_obligations_buffered(token_stream: AsyncIterator[str]) -> AsyncIterator[Dict[str, Any]]:
    """Accumulate full response then parse. Used when ijson not available or for fallback."""
    buffer = ""
    async for chunk in token_stream:
        buffer += chunk
    try:
        result_text = _strip_markdown_wrapper(buffer)
        data = json.loads(result_text)
        for obligation in data.get("results", []):
            if isinstance(obligation, dict):
                yield obligation
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse streaming JSON: %s", e)
        logger.error("Buffer content: %s...", buffer[:500])


async def parse_obligations_stream_simple(token_stream: AsyncIterator[str]) -> AsyncIterator[Dict[str, Any]]:
    """
    Simpler approach: buffer until we see complete obligation objects using regex.
    Yields obligations as they're completed in the stream.
    """
    buffer = ""
    # Pattern to match complete obligation objects (greedy, matches { ... })
    # This is a heuristic; works when LLM outputs one obligation per line or with clear structure
    obligation_pattern = re.compile(r'\{[^{}]*"DutyType"[^{}]*\}', re.DOTALL)
    
    async for chunk in token_stream:
        buffer += chunk
        
        # Try to extract complete obligations from buffer
        while True:
            match = obligation_pattern.search(buffer)
            if not match:
                break
            
            ob_text = match.group(0)
            try:
                ob = json.loads(ob_text)
                if isinstance(ob, dict):
                    yield ob
                    # Remove the matched obligation from buffer
                    buffer = buffer[match.end():]
            except json.JSONDecodeError:
                # Not valid JSON, skip this match
                buffer = buffer[match.end():]
