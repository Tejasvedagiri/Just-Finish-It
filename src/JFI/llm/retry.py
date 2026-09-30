"""What happens when an LLM request fails: which errors are retried
automatically, how they're shown, and the pause between retries. Used by
the episode engine (JFI.episode.engine) for every role's model call."""

import time

from openai import APIConnectionError, APIStatusError

from JFI.manager.abstract_manager import ResponseTooLongError

# How many times a transient LLM-server failure (connection drop, 5xx) is
# retried before the user is asked to retry or stop.
LLM_RETRY_LIMIT = 2
LLM_RETRY_DELAY_SECONDS = 3.0


def is_retryable(e: Exception) -> bool:
    """
    Worth retrying: a network-level failure (server down/restarting, a
    dropped connection), a 5xx-class server error — both are typically
    transient on a local LLM server; observed on stui run 12: LM Studio's
    "failed to decode" 500 under load — or a response that blew past
    STREAM_OUTPUT_CAP (see ResponseTooLongError): the model was still
    going, not finished, so another shot is worth it. NOT a 4xx client
    error: retrying an identical request the server already rejected (bad
    request, auth, context-length) won't produce a different result.
    """
    if isinstance(e, (APIConnectionError, ResponseTooLongError)):
        return True
    if isinstance(e, APIStatusError):
        return e.status_code >= 500
    return False


def format_error(e: Exception) -> str:
    """
    Turns an LLM call failure into a message worth reading.

    The openai SDK embeds the raw response body verbatim into str(e) when a
    server error isn't valid JSON: a server crash that falls back to a
    framework's generic HTML error page dumps that whole page into the
    exception message instead of a clean API error. Detected via
    status_code/response (present on openai.APIStatusError), not by
    string-sniffing the message.
    """
    status_code = getattr(e, "status_code", None)
    response = getattr(e, "response", None)
    body_text = getattr(response, "text", None) if response is not None else None

    if status_code is not None and body_text and body_text.strip()[:15].lower().lstrip().startswith(("<!doctype", "<html")):
        preview = " ".join(body_text.split())[:200]
        ellipsis = "…" if len(body_text) > 200 else ""
        return (
            f"HTTP {status_code} — the server returned an HTML error page instead of a "
            f"proper API error. This is almost always a crash or restart on the LLM "
            f"server's own side, not something this request caused; check its logs. "
            f"Preview: {preview}{ellipsis}"
        )
    return str(e)


def interruptible_sleep(console, seconds: float, poll: float = 0.2) -> None:
    """time.sleep(seconds), but checks console.should_stop() every `poll`
    seconds so Ctrl+C during a retry delay is responsive instead of waiting
    out the full delay first."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not console.should_stop():
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
