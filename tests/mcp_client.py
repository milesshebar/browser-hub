"""A minimal MCP client for the streamable-HTTP transport playwright-mcp serves. Test use only."""
import json
import urllib.error
import urllib.request


class MCPError(Exception):
    pass


class Session:
    def __init__(self, port, timeout=60):
        self.url = f"http://localhost:{port}/mcp"
        self.timeout = timeout
        self.sid = None
        self._id = 0
        init = self._rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "browser-hub-tests", "version": "1"}})
        self.server = init["serverInfo"]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _post(self, payload):
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        if self.sid:
            req.add_header("mcp-session-id", self.sid)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            self.sid = resp.headers.get("mcp-session-id") or self.sid
            body = resp.read().decode()
        if not body.strip():
            return None
        if body.lstrip().startswith("{"):
            return json.loads(body)
        # Server-sent events: the response is the last `data:` line carrying our id.
        msgs = [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]
        return msgs[-1] if msgs else None

    def _rpc(self, method, params):
        self._id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if msg is None:
            raise MCPError(f"{method}: empty response")
        if "error" in msg:
            raise MCPError(f"{method}: {msg['error']}")
        return msg["result"]

    def tools(self):
        return [t["name"] for t in self._rpc("tools/list", {})["tools"]]

    def call(self, name, **args):
        """-> the tool's text output. Raises MCPError when the tool reports an error."""
        res = self._rpc("tools/call", {"name": name, "arguments": args})
        text = "\n".join(c.get("text", "") for c in res.get("content", []))
        if res.get("isError"):
            raise MCPError(f"{name}: {text[:500]}")
        return text

    def close(self):
        if not self.sid:
            return
        req = urllib.request.Request(self.url, method="DELETE")
        req.add_header("mcp-session-id", self.sid)
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except (urllib.error.URLError, OSError):
            pass
        self.sid = None
