import re

# Raw model/chat-template special tokens that occasionally leak into a
# stream instead of being consumed by the server before it reaches JFI --
# seen in practice corrupting plain assistant narrative text, and (via a
# malformed tool call) file paths and file contents. None of this is ever
# a real deliverable; it's server/model plumbing that escaped. Observed
# forms are inconsistent about which side carries the pipe -- <tool_call|>,
# <|channel>, <channel|> have all shown up, never both-sided reliably -- so
# each side is matched independently.
#
# The pipe adjacent to '<' or '>' is the deliberately conservative
# signature: plain HTML/JSX (<div>, <Hero prop="x">, ...) never has one
# there, so ordinary generated markup is never at risk of being stripped.
_LEAKED_TOKEN_RE = re.compile(
    r"</?s>"                       # <s>, </s> -- common BOS/EOS markers
    r"|<\|[A-Za-z_][\w./-]{0,30}>"  # <|channel>, <|endoftext>, ...
    r"|<[A-Za-z_][\w./-]{0,30}\|>"  # <tool_call|>, <channel|>, ...
)


def strip_leaked_special_tokens(text: str) -> str:
    """Removes raw special/chat-template tokens that leaked into `text`
    unparsed. Safe to call on any string; a clean string passes through
    unchanged."""
    if not text:
        return text
    return _LEAKED_TOKEN_RE.sub("", text)
