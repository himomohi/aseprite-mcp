"""WebSocket transport for live Aseprite control.

Derived from ZhangDongyang800/Aseprite_MCP's WebSocket bridge (MIT).
Adapted for diivi/aseprite-mcp's inline Lua execution model.

The MCP process hosts a local WebSocket server. An Aseprite extension connects
as a client, receives an absolute temporary Lua script path, executes it inside
the running Aseprite instance, and returns captured stdout/stderr.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Optional

try:
    from websockets.asyncio.server import serve

    _WEBSOCKETS_AVAILABLE = True
except ImportError:
    serve = None
    _WEBSOCKETS_AVAILABLE = False


def _unescape_text(value: str) -> str:
    if not value:
        return ""

    result: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "t":
                result.append("\t")
            elif nxt == "n":
                result.append("\n")
            elif nxt == "r":
                result.append("\r")
            elif nxt == "\\":
                result.append("\\")
            else:
                result.extend((value[i], nxt))
            i += 2
        else:
            result.append(value[i])
            i += 1
    return "".join(result)


class WebSocketBridge:
    """Threaded WebSocket bridge with a synchronous request API."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9001):
        if not _WEBSOCKETS_AVAILABLE:
            raise ImportError(
                "WebSocket live mode requires the 'websockets' package. "
                "Install project dependencies again or run: pip install websockets"
            )

        self.host = host
        self.port = port
        self._client = None
        self._client_lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict]] = {}
        self._pending_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self._started = threading.Event()
        self._start_error: Optional[str] = None

    def start(self, timeout: float = 5.0) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return self._started.is_set() and self._start_error is None

        self._thread = threading.Thread(
            target=self._run_server,
            name="aseprite-mcp-websocket",
            daemon=True,
        )
        self._thread.start()
        self._started.wait(timeout)
        return self._started.is_set() and self._start_error is None

    def _run_server(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
            self._loop.run_forever()
        except Exception as exc:
            self._start_error = str(exc)
            self._started.set()
            print(f"[Aseprite MCP Live] WebSocket server error: {exc}", flush=True)
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        self._server = await serve(self._handle_client, self.host, self.port)
        self._started.set()
        print(
            f"[Aseprite MCP Live] Listening on ws://{self.host}:{self.port}; "
            "waiting for the Aseprite extension...",
            flush=True,
        )

    async def _handle_client(self, websocket) -> None:
        with self._client_lock:
            previous = self._client
            self._client = websocket

        if previous is not None and previous is not websocket:
            try:
                await previous.close()
            except Exception:
                pass

        print("[Aseprite MCP Live] Aseprite connected", flush=True)
        try:
            async for message in websocket:
                self._handle_response(message)
        except Exception as exc:
            print(f"[Aseprite MCP Live] Connection error: {exc}", flush=True)
        finally:
            with self._client_lock:
                if self._client is websocket:
                    self._client = None
            print("[Aseprite MCP Live] Aseprite disconnected", flush=True)

    def _handle_response(self, message: str) -> None:
        parts = message.split("\t", 3)
        if len(parts) != 4:
            print(f"[Aseprite MCP Live] Malformed response: {message!r}", flush=True)
            return

        request_id, success_text, stdout_text, stderr_text = parts
        result = {
            "success": success_text.lower() == "true",
            "stdout": _unescape_text(stdout_text),
            "stderr": _unescape_text(stderr_text),
            "transport_error": False,
        }

        with self._pending_lock:
            pending = self._pending.pop(request_id, None)

        if pending:
            event, box = pending
            box["result"] = result
            event.set()

    def is_connected(self) -> bool:
        with self._client_lock:
            return self._client is not None

    def send_script(self, script_path: str, timeout: float = 30.0) -> dict:
        if self._start_error:
            return self._transport_error(f"WebSocket server failed to start: {self._start_error}")
        if not self.is_connected():
            return self._transport_error(
                "Aseprite extension is not connected. Open Aseprite and run "
                "'MCP Live Bridge: Toggle Connection'."
            )
        if self._loop is None:
            return self._transport_error("WebSocket bridge is not running")

        request_id = str(uuid.uuid4())
        event = threading.Event()
        box: dict = {}
        with self._pending_lock:
            self._pending[request_id] = (event, box)

        with self._client_lock:
            client = self._client
        if client is None:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            return self._transport_error("Aseprite connection was lost")

        message = f"{request_id}\t{script_path}"
        try:
            future = asyncio.run_coroutine_threadsafe(client.send(message), self._loop)
            future.result(timeout=5.0)
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            return self._transport_error(f"Failed to send script to Aseprite: {exc}")

        if not event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            return self._transport_error(
                f"Live script timed out after {timeout:.0f}s. Aseprite may be busy or unfocused."
            )

        return box.get("result", self._transport_error("No result received from Aseprite"))

    @staticmethod
    def _transport_error(message: str) -> dict:
        return {
            "success": False,
            "stdout": "",
            "stderr": message,
            "transport_error": True,
        }

    def stop(self) -> None:
        if not self._loop:
            return

        async def _shutdown() -> None:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=2.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
