-- Aseprite MCP Live Bridge status
local BRIDGE_HOST = os.getenv("ASEPRITE_WS_HOST") or "127.0.0.1"
local BRIDGE_PORT = tonumber(os.getenv("ASEPRITE_WS_PORT") or "9001")
local BRIDGE_URL = "ws://" .. BRIDGE_HOST .. ":" .. BRIDGE_PORT

if _G._aseprite_mcp_live_ws then
    app.alert("Aseprite MCP Live: CONNECTED\nServer: " .. BRIDGE_URL)
else
    app.alert(
        "Aseprite MCP Live: DISCONNECTED\nExpected server: " .. BRIDGE_URL
        .. "\nRun 'MCP Live Bridge: Toggle Connection' to connect."
    )
end
