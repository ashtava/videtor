"""A headless Chrome driven over the DevTools protocol with the standard library only: open a page, run script in
it, take pictures. For checking the app's page as a person would use it."""
import base64, json, os, socket, struct, subprocess, time, urllib.request


class Chrome:
    def __init__(self, profile, port=9333, size=(1500, 1300)):
        self.port = port
        self.proc = subprocess.Popen(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--headless=new", "--disable-gpu", "--hide-scrollbars",
                                      f"--remote-debugging-port={port}", f"--user-data-dir={profile}", f"--window-size={size[0]},{size[1]}",
                                      "--autoplay-policy=no-user-gesture-required", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1))
                page = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("Chrome did not start")
        url = page["webSocketDebuggerUrl"]
        host, path = url[5:].split("/", 1)
        self.s = socket.create_connection(("127.0.0.1", port))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.n = 0
        self.call("Page.enable"); self.call("Runtime.enable")

    def _send(self, text):
        data = text.encode(); mask = os.urandom(4)
        head = bytes([0x81]) + (bytes([0x80 | len(data)]) if len(data) < 126 else bytes([0x80 | 126]) + struct.pack(">H", len(data)) if len(data) < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", len(data)))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(1 << 16)
            if not chunk:
                raise RuntimeError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _recv(self):
        msg = b""
        while True:
            b0, b1 = self._need(2)
            n = b1 & 0x7F
            if n == 126: n = struct.unpack(">H", self._need(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._need(8))[0]
            msg += self._need(n)
            if b0 & 0x80:
                return json.loads(msg) if (b0 & 0x0F) in (0, 1) else {}

    def call(self, method, params=None):
        self.n += 1
        self._send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            m = self._recv()
            if m.get("id") == self.n:
                if "error" in m: raise RuntimeError(m["error"])
                return m.get("result", {})

    def js(self, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
        if r.get("exceptionDetails"): raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description") or str(r["exceptionDetails"]))
        return r.get("result", {}).get("value")

    def go(self, url, wait=2.0):
        self.call("Page.navigate", {"url": url}); time.sleep(wait)

    def shot(self, path, clip=None):
        params = {"format": "png"}
        if clip: params["clip"] = {**clip, "scale": 1}
        open(path, "wb").write(base64.b64decode(self.call("Page.captureScreenshot", params)["data"]))

    def close(self):
        try: self.call("Browser.close")
        except Exception: pass
        self.proc.terminate()
