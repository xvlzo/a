--[[
  No Hesi Bot — CSP Lua overlay
  Communicates with the C++ bot process via UDP (127.0.0.1:27015)
  Displays live status and exposes sliders for tuning.

  Install: copy lua/nohesi_bot/ to
    assettocorsa/apps/lua/nohesi_bot/
]]

local socket = (function()
    local ok, s = pcall(require, 'socket')
    if ok then return s end
    ok, s = pcall(require, 'shared/socket')
    if ok then return s end
    error('No socket library found — install lua-socket or check CSP version')
end)()

-- ── UDP connection ────────────────────────────────────────────────────────────
local udp = socket.udp()
udp:settimeout(0)  -- non-blocking
udp:setpeername('127.0.0.1', 27015)

-- ── State ────────────────────────────────────────────────────────────────────
local status = {
    on         = false,
    speed      = 0,
    target     = 160,
    d          = 0,
    passes_3x  = 0,
    passes_1x  = 0,
}

local settings = {
    enabled      = false,
    humanization = 0.7,
    smoothness   = 0.6,
    target_kph   = 160,
    safety       = 1.2,
    close_3x     = 4.0,
    close_1x     = 7.0,
    pass_dist    = 3.0,
}

local dirty = true           -- send initial settings on first update to establish connection
local last_recv = 0          -- last status receive time
local connected = false

-- ── Send a setting to the bot ─────────────────────────────────────────────────
local function sendSetting(key, value)
    local msg = key .. '=' .. tostring(value)
    local ok, err = udp:send(msg)
    if ok then connected = true end
end

-- ── Parse status line from bot ────────────────────────────────────────────────
-- Format: "speed=158.3|target=160.0|d=0.42|3x=14|1x=8|on=1"
local function parseStatus(line)
    for pair in line:gmatch('[^|]+') do
        local k, v = pair:match('(.-)=(.+)')
        if k and v then
            local n = tonumber(v)
            if k == 'speed'  then status.speed    = n or status.speed
            elseif k == 'target' then status.target = n or status.target
            elseif k == 'd'  then status.d        = n or status.d
            elseif k == '3x' then status.passes_3x = n or status.passes_3x
            elseif k == '1x' then status.passes_1x = n or status.passes_1x
            elseif k == 'on' then
                status.on        = (v == '1')
                settings.enabled = status.on  -- keep toggle button in sync with F5 hotkey
            end
        end
    end
end

-- ── Update (called every frame by CSP) ───────────────────────────────────────
function script.update(dt)
    -- Receive status from bot
    local line, err = udp:receive()
    if line then
        parseStatus(line)
        last_recv = os.clock()
        connected = true
    end

    -- Mark disconnected if no data for 2s
    if os.clock() - last_recv > 2 then
        connected = false
    end

    -- Send dirty settings
    if dirty then
        sendSetting('enabled',   settings.enabled and '1' or '0')
        sendSetting('hum',       string.format('%.2f', settings.humanization))
        sendSetting('smooth',    string.format('%.2f', settings.smoothness))
        sendSetting('speed',     string.format('%.0f', settings.target_kph))
        sendSetting('close3x',   string.format('%.1f', settings.close_3x))
        sendSetting('close1x',   string.format('%.1f', settings.close_1x))
        sendSetting('passdist',  string.format('%.1f', settings.pass_dist))
        dirty = false
    end
end

-- ── Draw overlay ──────────────────────────────────────────────────────────────
-- CSP app callback: script.windowMain(dt) is called each frame inside the app window.
-- 'ui' is a CSP global — do not require() or alias it.
function script.windowMain(dt)
    -- ── Header ──────────────────────────────────────────────────────────────
    local bot_color = status.on and rgbm(0.2, 1.0, 0.3, 1) or rgbm(1, 0.3, 0.3, 1)
    local conn_str  = connected and '' or ' [NO CONN]'
    ui.pushStyleColor(ui.StyleColor.Text, bot_color)
    ui.text(string.format('NO HESI BOT  [%s]%s',
        status.on and 'ON' or 'OFF', conn_str))
    ui.popStyleColor()

    ui.separator()

    -- ── Live stats ───────────────────────────────────────────────────────────
    ui.text(string.format('Speed   : %5.1f kph', status.speed))
    ui.text(string.format('Target  : %5.1f kph', status.target))
    ui.text(string.format('Offset d: %+5.2f m',  status.d))
    ui.text(string.format('3x passes: %d   1x: %d',
                          status.passes_3x, status.passes_1x))

    ui.separator()

    -- ── Toggle button ────────────────────────────────────────────────────────
    if ui.button(settings.enabled and 'Disable Bot [F5]' or 'Enable Bot  [F5]',
                 vec2(210, 22)) then
        settings.enabled = not settings.enabled
        dirty = true
    end

    ui.separator()

    -- ── Sliders ───────────────────────────────────────────────────────────────
    local h_new = ui.sliderFloat('Human', settings.humanization, 0, 1,
                                 string.format('%.0f%%', settings.humanization * 100))
    if math.abs(h_new - settings.humanization) > 0.01 then
        settings.humanization = h_new
        dirty = true
    end

    local s_new = ui.sliderFloat('Smooth', settings.smoothness, 0, 1,
                                 string.format('%.0f%%', settings.smoothness * 100))
    if math.abs(s_new - settings.smoothness) > 0.01 then
        settings.smoothness = s_new
        dirty = true
    end

    local v_new = ui.sliderFloat('Speed kph', settings.target_kph, 80, 220,
                                 string.format('%.0f', settings.target_kph))
    if math.abs(v_new - settings.target_kph) > 0.5 then
        settings.target_kph = v_new
        dirty = true
    end

    ui.separator()

    local c3_new = ui.sliderFloat('3x gap m', settings.close_3x, 1, 10,
                                  string.format('%.1f', settings.close_3x))
    if math.abs(c3_new - settings.close_3x) > 0.05 then
        settings.close_3x = c3_new
        dirty = true
    end

    local c1_new = ui.sliderFloat('1x gap m', settings.close_1x, 1, 15,
                                  string.format('%.1f', settings.close_1x))
    if math.abs(c1_new - settings.close_1x) > 0.05 then
        settings.close_1x = c1_new
        dirty = true
    end

    local pd_new = ui.sliderFloat('Pass dist m', settings.pass_dist, 0.5, 8,
                                   string.format('%.1f', settings.pass_dist))
    if math.abs(pd_new - settings.pass_dist) > 0.05 then
        settings.pass_dist = pd_new
        dirty = true
    end
end

-- ── Keyboard shortcut: F5 inside AC window ───────────────────────────────────
function script.onKeyDown(key)
    if key == ac.KeyIndex.F5 then
        settings.enabled = not settings.enabled
        dirty = true
    elseif key == ac.KeyIndex.F6 then
        settings.humanization = math.min(1.0, settings.humanization + 0.1)
        dirty = true
    elseif key == ac.KeyIndex.F7 then
        settings.humanization = math.max(0.0, settings.humanization - 0.1)
        dirty = true
    elseif key == ac.KeyIndex.F8 then
        settings.target_kph = math.min(220, settings.target_kph + 10)
        dirty = true
    elseif key == ac.KeyIndex.F9 then
        settings.target_kph = math.max(80,  settings.target_kph - 10)
        dirty = true
    end
end
