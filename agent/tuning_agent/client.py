"""HTTP client for the tuner web service (stdlib only)."""
import json
import ssl
import time
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

    RETRY_STATUS = (502, 503, 504)
    RETRY_DELAYS = (2, 4, 8, 15, 30, 30, 30, 60)  # ~3 minutes: rides out web app restarts and network blips

    def call(self, method, path, body=None, timeout=None, retry_timeouts=True):
        for i, delay in enumerate(self.RETRY_DELAYS + (None,)):
            try:
                return self._call(method, path, body, timeout)
            except APIError as e:
                if delay is None or not getattr(e, "retryable", False) or (getattr(e, "timeout", False) and not retry_timeouts):
                    raise
                time.sleep(delay)

    def _call(self, method, path, body=None, timeout=None):
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(self.server + "/api/v1/agent" + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", f"tuning-agent/{__version__}")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self.ctx) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except Exception:
                detail = e.reason
            err = APIError(f"{method} {path}: HTTP {e.code}: {detail}")
            err.retryable = e.code in self.RETRY_STATUS
            raise err from None
        except (urllib.error.URLError, OSError) as e:
            err = APIError(f"{method} {path}: {e}")
            err.retryable = True
            err.timeout = isinstance(e, TimeoutError) or "timed out" in str(e)
            raise err from None

    def post(self, path, body, timeout=None, retry_timeouts=True):
        return self.call("POST", path, body, timeout, retry_timeouts)

    # AI model calls (planner, ideas, analyses) take 20-90 s on the server: a longer wait, and a timed-out call is not sent
    # again (each retry would be another paid model call; the rules' choice stands instead)
    AI_TIMEOUT = 540  # planner + reviewer: two model calls of at most 240 s each on the server

    def post_ai(self, path, body):
        return self.post(path, body, timeout=self.AI_TIMEOUT, retry_timeouts=False)

    def get(self, path):
        return self.call("GET", path)
