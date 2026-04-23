"""
Opik's Bedrock invoke_model wrapper replaces botocore StreamingBody.read globally.

The stock wrapper's `finally` block does `return None` when the body is not marked
`opik_tracked_instance`. In Python, `return` in `finally` discards the normal return
from `try`, so Mem0's plain boto3 bedrock client (and any other untracked caller) gets
`read()` → None and `json.loads(None)` fails.

We replace `wrap_invoke_model_response` with a version that only runs Opik's callback
for tracked bodies and never returns from `finally` for untracked ones.
"""
import functools
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_applied = False


def apply_opik_bedrock_streaming_body_read_fix() -> None:
    """Idempotent monkeypatch; safe if Opik is not installed."""
    global _applied
    if _applied:
        return
    try:
        import botocore.response
        from opik.decorator import error_info_collector
        from opik.integrations.bedrock.invoke_model import stream_wrappers
        from opik.types import ErrorInfoDict
    except ImportError as e:
        logger.debug("Opik streaming-body fix skipped (import error): %s", e)
        return

    __original_streaming_body_read = stream_wrappers.__original_streaming_body_read

    def wrap_invoke_model_response_fixed(
        output: Any,
        span_to_end: Any,
        trace_to_end: Optional[Any],
        finally_callback: Any,
    ) -> Any:
        response_metadata = output["ResponseMetadata"]
        streaming_body = output["body"]

        @functools.wraps(__original_streaming_body_read)
        def wrapped_read(self: botocore.response.StreamingBody, *args, **kwargs):  # type: ignore
            error_info: Optional[ErrorInfoDict] = None
            result = None
            try:
                result = __original_streaming_body_read(self, *args, **kwargs)
            except Exception as exception:
                logger.debug(
                    "Exception raised from botocore.response.StreamingBody: %s",
                    str(exception),
                    exc_info=True,
                )
                error_info = error_info_collector.collect(exception)
                raise
            finally:
                if getattr(self, "opik_tracked_instance", False):
                    delattr(self, "opik_tracked_instance")
                    if error_info is None and result is not None:
                        try:
                            parsed_body = json.loads(result)
                            out = {
                                "body": parsed_body,
                                "ResponseMetadata": response_metadata,
                            }
                            logger.debug(
                                "Successfully parsed response body with keys: %s",
                                list(parsed_body.keys()),
                            )
                        except (json.JSONDecodeError, TypeError) as e:
                            logger.debug("Failed to parse response body as JSON: %s", e)
                            out = {"body": {}, "ResponseMetadata": response_metadata}
                    else:
                        logger.debug("Error occurred or result is None, using empty body")
                        out = {"body": {}, "ResponseMetadata": response_metadata}

                    finally_callback(
                        output=out,
                        error_info=error_info,
                        generators_span_to_end=span_to_end,
                        generators_trace_to_end=trace_to_end,
                        capture_output=True,
                    )

            return result

        botocore.response.StreamingBody.read = wrapped_read
        streaming_body.opik_tracked_instance = True
        return output

    stream_wrappers.wrap_invoke_model_response = wrap_invoke_model_response_fixed
    _applied = True
    logger.info(
        "Patched Opik wrap_invoke_model_response so plain boto3 Bedrock clients can read response bodies"
    )
