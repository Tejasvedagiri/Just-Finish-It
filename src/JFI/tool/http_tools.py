"""http_request -- a structured call to an API the project serves: status,
headers and the parsed body, instead of `curl` through the shell (fragile
quoting on Windows, and its raw output is hard to read)."""

import json
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse

from JFI.tool.result_cap import cap_result

TIMEOUT_SECONDS_DEFAULT = 15
_SHOWN_HEADERS = ("content-type", "location", "set-cookie", "www-authenticate", "allow")


def http_request(method: str, url: str, body: Optional[str] = None, headers: Optional[dict] = None,
                 timeout: int = TIMEOUT_SECONDS_DEFAULT) -> str:
    if urlparse(url).scheme not in ("http", "https"):
        return f"Error: only http/https URLs are supported, not {url!r}."
    data, sent = None, dict(headers or {})
    if body is not None:
        data = body.encode("utf-8")
        if not any(k.lower() == "content-type" for k in sent):
            try:
                json.loads(body)
                sent["Content-Type"] = "application/json"
            except ValueError:
                sent["Content-Type"] = "text/plain; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=sent, method=(method or "GET").upper())
    try:
        with urllib.request.urlopen(request, timeout=max(1, int(timeout))) as response:
            status, reply_headers, raw = response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:  # a 4xx/5xx is an answer worth reading, not a failure of the tool
        status, reply_headers, raw = e.code, e.headers, e.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"Error: {method.upper()} {url} failed: {getattr(e, 'reason', e)}. Is the server running?"
    text = raw.decode("utf-8", errors="replace")
    try:
        text = json.dumps(json.loads(text), indent=2)
    except ValueError:
        pass
    shown = "\n".join(f"{k}: {v}" for k, v in reply_headers.items() if k.lower() in _SHOWN_HEADERS)
    return cap_result(f"{method.upper()} {url} -> {status}\n{shown}\n\n{text}".rstrip(),
                      "Request a narrower resource (a filter, a page, one id).")
