import subprocess
import tempfile
import os
import dotenv

_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
dotenv.load_dotenv(dotenv_path=_ENV_PATH)

_LIVE_BRIDGE = None
_LIVE_BRIDGE_ERROR = None


def lua_escape(s: str) -> str:
    """Escape a string for safe embedding inside a Lua double-quoted string literal."""
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
         .replace("\0", "\\0")
    )


def reject_traversal(path: str) -> str | None:
    """Reject parent-directory traversal in a user-supplied path.

    Returns an error message string when the path contains a `..`
    component, or None when the path looks safe.

    The check works on normalized path components, so it does not
    false-positive on filenames like `foo..bar.aseprite` (the previous
    `'..' in path` substring check did). Absolute paths and tilde
    expansion are not rejected here: this function targets traversal
    only, not access scoping.
    """
    parts = os.path.normpath(path).replace("\\", "/").split("/")
    if ".." in parts:
        return "Invalid filename: parent directory traversal not allowed"
    return None


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _live_mode_enabled() -> bool:
    return os.getenv("ASEPRITE_MCP_MODE", "cli").strip().lower() in {"ws", "websocket", "live"}


def _get_live_bridge():
    """Create/start the WebSocket bridge lazily so CLI users pay no startup cost."""
    global _LIVE_BRIDGE, _LIVE_BRIDGE_ERROR

    if _LIVE_BRIDGE is not None:
        return _LIVE_BRIDGE
    if _LIVE_BRIDGE_ERROR is not None:
        return None

    try:
        from .live_bridge import WebSocketBridge

        host = os.getenv("ASEPRITE_WS_HOST", "127.0.0.1")
        port = int(os.getenv("ASEPRITE_WS_PORT", "9001"))
        bridge = WebSocketBridge(host=host, port=port)
        if not bridge.start(timeout=5.0):
            _LIVE_BRIDGE_ERROR = "WebSocket bridge did not start"
            return None
        _LIVE_BRIDGE = bridge
        return _LIVE_BRIDGE
    except Exception as exc:
        _LIVE_BRIDGE_ERROR = str(exc)
        return None


def _live_target_prelude(filename: str | None) -> str:
    """Make the requested file active in the running Aseprite instance.

    diivi's tools intentionally operate on app.activeSprite because in CLI mode
    the filename is opened before the Lua script runs. Live mode recreates that
    invariant without spawning a new Aseprite process for every tool call.
    """
    if not filename:
        return ""

    target = lua_escape(os.path.abspath(filename).replace("\\", "/"))
    return f'''
-- Aseprite MCP live-mode target selection.
local __mcp_target = "{target}"
local function __mcp_norm_path(p)
    if not p then return "" end
    return string.lower(string.gsub(p, "\\\\", "/"))
end
local __mcp_active = app.activeSprite
if (not __mcp_active) or (__mcp_norm_path(__mcp_active.filename) ~= __mcp_norm_path(__mcp_target)) then
    local __mcp_match = nil
    for _, __mcp_sprite in ipairs(app.sprites) do
        if __mcp_norm_path(__mcp_sprite.filename) == __mcp_norm_path(__mcp_target) then
            __mcp_match = __mcp_sprite
            break
        end
    end
    if __mcp_match then
        local __mcp_switched = pcall(function() app.activeSprite = __mcp_match end)
        if not __mcp_switched then app.open(__mcp_target) end
    else
        app.open(__mcp_target)
    end
end
'''


class AsepriteCommand:
    """Helper class for running Aseprite commands."""

    @staticmethod
    def run_command(args):
        """Run an Aseprite command with proper error handling.

        Args:
            args: List of command arguments

        Returns:
            tuple: (success, output) where success is a boolean and output is the command output
        """
        try:
            cmd = [os.getenv('ASEPRITE_PATH', 'aseprite')] + args
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            return False, e.stderr
        except OSError as e:
            return False, str(e)

    @staticmethod
    def _execute_cli_script(script_path: str, filename=None):
        args = ["--batch"]
        if filename and os.path.exists(filename):
            args.append(filename)
        args.extend(["--script", script_path])
        return AsepriteCommand.run_command(args)

    @staticmethod
    def _execute_live_script(script_path: str):
        bridge = _get_live_bridge()
        if bridge is None:
            return False, _LIVE_BRIDGE_ERROR or "WebSocket bridge unavailable", True

        timeout = float(os.getenv("ASEPRITE_WS_TIMEOUT", "30"))
        result = bridge.send_script(script_path, timeout=timeout)
        if result.get("success"):
            return True, result.get("stdout", ""), False

        output = result.get("stderr") or result.get("stdout") or "Live script failed"
        return False, output, bool(result.get("transport_error"))

    @staticmethod
    def execute_lua_script(script_content, filename=None):
        """Execute a Lua script in Aseprite.

        CLI mode (default) starts Aseprite in batch mode for each call.
        Live mode (ASEPRITE_MCP_MODE=ws) forwards the same generated Lua script
        to the running Aseprite UI through the bundled WebSocket extension.

        If live transport is unavailable, CLI fallback is enabled by default.
        Set ASEPRITE_MCP_WS_FALLBACK=0 to fail instead of falling back.
        """
        content = script_content
        if _live_mode_enabled():
            content = _live_target_prelude(filename) + script_content

        with tempfile.NamedTemporaryFile(
            suffix='.lua', delete=False, mode='w', encoding='utf-8'
        ) as tmp:
            tmp.write(content)
            script_path = os.path.abspath(tmp.name)

        try:
            if _live_mode_enabled():
                success, output, transport_error = AsepriteCommand._execute_live_script(script_path)
                if success:
                    return True, output

                fallback = _env_flag("ASEPRITE_MCP_WS_FALLBACK", default=True)
                if not (transport_error and fallback):
                    return False, output

                return AsepriteCommand._execute_cli_script(script_path, filename)

            return AsepriteCommand._execute_cli_script(script_path, filename)
        finally:
            try:
                os.remove(script_path)
            except OSError:
                pass

    @staticmethod
    def execute_lua_script_checked(script_content, filename=None):
        """Execute a Lua script and surface in-script errors.

        Scripts using this helper signal failure by printing a line
        starting with "ERROR:" (batch-mode scripts cannot affect the
        process exit code from Lua).

        Returns:
            tuple: (success, output) where output is the error message
            when an ERROR: line was printed, or the raw stdout otherwise.
        """
        success, output = AsepriteCommand.execute_lua_script(script_content, filename)
        if not success:
            return False, output
        for line in output.splitlines():
            if line.startswith("ERROR:"):
                return False, line[len("ERROR:"):]
        return True, output
