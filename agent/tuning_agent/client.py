"""HTTP client for the tuner web service (stdlib only)."""
import json
import ssl
import urllib.error
import urllib.request

from . import __version__


class APIError(Exception):
    pass


class Client:
    def __init__(self, server, api_key, timeout=30):
        self.server = server.rstrip("/")
        self.key = api_key
        self.timeout = timeout
        self.ctx = ssl.create_default_context()

    def call(self, method, path, body=None):
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(self.server + "/api/v1/agent" + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", f"tuning-agent/{__version__}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except Exception:
                detail = e.reason
            raise APIError(f"{method} {path}: HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as e:
            raise APIError(f"{method} {path}: {e}") from None

    def post(self, path, body):
        return self.call("POST", path, body)

    def get(self, path):
        return self.call("GET", path)
