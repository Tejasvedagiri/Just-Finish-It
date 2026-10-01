"""http_request: a structured call to an API the project serves, instead of
curl through the shell (fragile quoting on Windows, hard-to-read output)."""
import http.server
import json
import threading

import pytest

from JFI.tool.http_tools import http_request


@pytest.fixture
def api():
    class Api(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self._reply(200 if self.path == "/items" else 404, {"items": [1, 2]} if self.path == "/items" else {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._reply(201, {"created": body, "type": self.headers["Content-Type"]})

        def _reply(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_status_headers_and_parsed_json(api):
    out = http_request("GET", f"{api}/items")
    assert out.startswith(f"GET {api}/items -> 200") and "Content-Type: application/json" in out
    assert '"items": [\n    1,\n    2\n  ]' in out


def test_a_404_is_an_answer_and_a_json_body_is_sent_as_json(api):
    assert http_request("GET", f"{api}/nope").startswith(f"GET {api}/nope -> 404")
    out = http_request("POST", f"{api}/items", body='{"name": "x"}')
    assert "-> 201" in out and '"type": "application/json"' in out and '"name": "x"' in out


def test_a_server_that_isnt_running_is_an_error_with_the_likely_cause():
    assert http_request("GET", "http://127.0.0.1:1/").startswith("Error:")
    assert http_request("GET", "ftp://x").startswith("Error: only http/https")
