"""Sends requests to Hermes's dashboard backend, the one the Desktop app talks to, in a fresh process: like the
real backend, it reads the plugins of ``$HERMES_HOME`` when it starts.

    python desktop_backend.py REQUESTS OUTPUT

REQUESTS is a JSON list of ``[method, path, body?]`` HTTP requests or ``["RPC", method, params]`` JSON-RPC
calls (what the Desktop sends over ``/api/ws``); the responses are written to OUTPUT as a JSON list.
"""
import json
import sys

from starlette.testclient import TestClient

from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
from tui_gateway.server import handle_request

client = TestClient(app, headers={_SESSION_HEADER_NAME: _SESSION_TOKEN})
responses = []
for method, target, *body in json.loads(sys.argv[1]):
    if method == "RPC":
        responses.append(handle_request({"jsonrpc": "2.0", "id": 1, "method": target, "params": body[0]}))
    else:
        response = client.request(method, target, json=body[0] if body else None)
        responses.append({"status": response.status_code, **response.json()})
with open(sys.argv[2], "w") as f:
    json.dump(responses, f)
