-- Aseprite MCP Live Bridge
-- Derived from ZhangDongyang800/Aseprite_MCP (MIT License, Copyright 2026 张东阳).
-- Adapted for diivi/aseprite-mcp's generated inline Lua scripts.
--
-- Protocol:
--   request:  <id>\t<absolute_script_path>
--   response: <id>\t<success>\t<stdout_escaped>\t<stderr_escaped>

local BRIDGE_HOST = os.getenv("ASEPRITE_WS_HOST") or "127.0.0.1"
local BRIDGE_PORT = tonumber(os.getenv("ASEPRITE_WS_PORT") or "9001")
local BRIDGE_URL = "ws://" .. BRIDGE_HOST .. ":" .. BRIDGE_PORT

local function escape_text(s)
    s = s or ""
    s = s:gsub("\\", "\\\\")
    s = s:gsub("\t", "\\t")
    s = s:gsub("\n", "\\n")
    s = s:gsub("\r", "\\r")
    return s
end

local function handle_request(message)
    local tab = message:find("\t", 1, true)
    if not tab then
        return "error\tfalse\t\t" .. escape_text("invalid request format")
    end

    local request_id = message:sub(1, tab - 1)
    local script_path = message:sub(tab + 1)
    if request_id == "" or script_path == "" then
        return (request_id ~= "" and request_id or "error")
            .. "\tfalse\t\t" .. escape_text("missing request id or script path")
    end

    local file = io.open(script_path, "r")
    if not file then
        return request_id .. "\tfalse\t\t" .. escape_text("script not found: " .. script_path)
    end
    file:close()

    local captured = {}
    local original_print = _G.print
    _G.print = function(...)
        local args = { ... }
        local values = {}
        for i = 1, #args do
            values[i] = tostring(args[i])
        end
        captured[#captured + 1] = table.concat(values, "\t")
    end

    local ok, err = pcall(function()
        dofile(script_path)
        app.refresh()
    end)

    _G.print = original_print

    local stdout = table.concat(captured, "\n")
    local stderr = ok and "" or tostring(err)
    return request_id
        .. "\t" .. tostring(ok)
        .. "\t" .. escape_text(stdout)
        .. "\t" .. escape_text(stderr)
end

if _G._aseprite_mcp_live_ws then
    local ws = _G._aseprite_mcp_live_ws
    _G._aseprite_mcp_live_ws = nil
    pcall(function() ws:close() end)
    app.alert("Aseprite MCP Live: Disconnected from " .. BRIDGE_URL)
    return
end

local ws = WebSocket {
    url = BRIDGE_URL,
    deflate = false,
    minreconnectwait = 0.5,
    maxreconnectwait = 3.0,
    onreceive = function(message_type, data)
        if message_type == WebSocketMessageType.OPEN then
            app.alert("Aseprite MCP Live: Connected to " .. BRIDGE_URL)
        elseif message_type == WebSocketMessageType.TEXT then
            local response = handle_request(data)
            if response and _G._aseprite_mcp_live_ws then
                _G._aseprite_mcp_live_ws:sendText(response)
            end
        elseif message_type == WebSocketMessageType.CLOSE then
            _G._aseprite_mcp_live_ws = nil
            app.alert("Aseprite MCP Live: Connection closed")
        end
    end,
}

ws:connect()
_G._aseprite_mcp_live_ws = ws
app.alert(
    "Aseprite MCP Live: Connecting to " .. BRIDGE_URL .. " ...\n"
    .. "Run this command again to disconnect.\n"
    .. "Start the MCP server with ASEPRITE_MCP_MODE=ws."
)
