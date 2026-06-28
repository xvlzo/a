--[[
  No Hesi Bot — CSP Lua overlay
  Communicates with the C++ bot process via UDP (127.0.0.1:27015)
  Displays live status and exposes sliders for tuning.

  Install: copy lua/nohesi_bot/ to
    assettocorsa/apps/lua/nohesi_bot/
]]

-- ── UDP connection ────────────────────────────────────────────────────────────
local socket_err = nil
local udp = nil

local socket = (function()
    local ok, s = pcall(require, 'socket')
    if ok and s then return s end
    ok, s = pcall(require, 'shared/socket')
    if ok and s then return s end
    socket_err = 'luasocket not available in this CSP build'
    return nil
end)()

if socket then
    local ok, err = pcall(function()
        udp = socket.udp()
        udp:settimeout(0)
        udp:setpeername('127.0.0.1', 27015)
    end)
    if not ok then
        socket_err = 'socket init failed: ' .. tostring(err)
        udp = nil
    end
end

-- Diagnostics shown in the window
local pkt_count = 0
local last_ego  = { x = 0, z = 0, n = 0 }

-- ── State ────────────────────────────────────────────────────────────────────
local status = {
    on        = false,
    speed     = 0,
    target    = 160,
    d         = 0,
    passes_3x = 0,
    passes_1x = 0,
}

local settings = {
    enabled      = false,
    humanization = 0.7,
    smoothness   = 0.6,
    target_kph   = 160,
    close_3x     = 4.0,
    close_1x     = 7.0,
    pass_dist    = 3.0,
}

local dirty     = true   -- send initial settings on first update
local last_recv = 0
local connected = false

-- ── Send a setting to the bot ─────────────────────────────────────────────────
local function sendSetting(key, value)
    if not udp then return end
    udp:send(key .. '=' .. tostring(value))
end

-- ── Parse status line from bot ────────────────────────────────────────────────
-- Format: "speed=158.3|target=160.0|d=0.42|3x=14|1x=8|on=1"
local function parseStatus(line)
    for pair in line:gmatch('[^|]+') do
        local k, v = pair:match('(.-)=(.+)')
        if k and v then
            local n = tonumber(v)
            if     k == 'speed'  then status.speed     = n or status.speed
            elseif k == 'target' then status.target    = n or status.target
            elseif k == 'd'      then status.d         = n or status.d
            elseif k == '3x'     then status.passes_3x = n or status.passes_3x
            elseif k == '1x'     then status.passes_1x = n or status.passes_1x
            elseif k == 'on'     then
                status.on        = (v == '1')
                settings.enabled = status.on  -- keep toggle in sync with F5 hotkey
            end
        end
    end
end

-- ── Stream live telemetry to the bot ─────────────────────────────────────────
-- CSP Lua is the authoritative position source online — the C++ side cannot
-- read player world position from shared memory in multiplayer.
-- Packet: t|ex,ez,eheading,espeed|idx,x,z,heading,speed;...
local TRAFFIC_RADIUS2 = 250 * 250
local function sendTelemetry()
    if not udp then return end
    local sim = ac.getSim()
    if not sim then return end
    local egoIdx = sim.focusedCar or 0
    local ego = ac.getCar(egoIdx)
    if not ego then return end

    -- math.atan2(y, x) — two-arg form for Lua 5.1 / LuaJIT
    local eh = math.atan2(ego.look.x, ego.look.z)
    local parts = { string.format('t|%.2f,%.2f,%.4f,%.2f|',
        ego.position.x, ego.position.z, eh, ego.speedKmh) }

    local n = sim.carsCount
    for i = 0, n - 1 do
        if i ~= egoIdx then
            local c = ac.getCar(i)
            if c and c.isConnected then
                local dx = c.position.x - ego.position.x
                local dz = c.position.z - ego.position.z
                if dx*dx + dz*dz < TRAFFIC_RADIUS2 then
                    local h = math.atan2(c.look.x, c.look.z)
                    parts[#parts+1] = string.format('%d,%.2f,%.2f,%.4f,%.2f;',
                        i, c.position.x, c.position.z, h, c.speedKmh)
                end
            end
        end
    end
    udp:send(table.concat(parts))
    pkt_count  = pkt_count + 1
    last_ego.x = ego.position.x
    last_ego.z = ego.position.z
    last_ego.n = n
end

-- ── Update (called every frame by CSP) ───────────────────────────────────────
function script.update(dt)
    if not udp then return end

    -- Drain all pending status packets (bot sends ~5 Hz, AC runs at 60+ Hz)
    local line = udp:receive()
    while line do
        parseStatus(line)
        last_recv = os.clock()
        connected = true
        line = udp:receive()
    end

    if os.clock() - last_recv > 2 then
        connected = false
    end

    sendTelemetry()

    if dirty then
        sendSetting('enabled',  settings.enabled and '1' or '0')
        sendSetting('hum',      string.format('%.2f', settings.humanization))
        sendSetting('smooth',   string.format('%.2f', settings.smoothness))
        sendSetting('speed',    string.format('%.0f', settings.target_kph))
        sendSetting('close3x',  string.format('%.1f', settings.close_3x))
        sendSetting('close1x',  string.format('%.1f', settings.close_1x))
        sendSetting('passdist', string.format('%.1f', settings.pass_dist))
        dirty = false
    end
end

-- ── Draw overlay ──────────────────────────────────────────────────────────────
function script.windowMain(dt)
    local bot_color = status.on and rgbm(0.2, 1.0, 0.3, 1) or rgbm(1, 0.3, 0.3, 1)
    ui.pushStyleColor(ui.StyleColor.Text, bot_color)
    ui.text(string.format('NO HESI BOT  [%s]%s',
        status.on and 'ON' or 'OFF', connected and '' or ' [NO CONN]'))
    ui.popStyleColor()

    ui.separator()

    if socket_err then
        ui.pushStyleColor(ui.StyleColor.Text, rgbm(1, 0.4, 0.2, 1))
        ui.text('SOCKET ERROR:')
        ui.text(socket_err)
        ui.popStyleColor()
    else
        ui.text(string.format('UDP sent : %d pkts', pkt_count))
        ui.text(string.format('Ego pos  : %.0f, %.0f', last_ego.x, last_ego.z))
        ui.text(string.format('Cars seen: %d', last_ego.n))
    end

    ui.separator()

    ui.text(string.format('Speed   : %5.1f kph', status.speed))
    ui.text(string.format('Target  : %5.1f kph', status.target))
    ui.text(string.format('Offset d: %+5.2f m',  status.d))
    ui.text(string.format('3x passes: %d   1x: %d', status.passes_3x, status.passes_1x))

    ui.separator()

    if ui.button(settings.enabled and 'Disable Bot [F5]' or 'Enable Bot  [F5]',
                 vec2(210, 22)) then
        settings.enabled = not settings.enabled
        dirty = true
    end

    ui.separator()

    local h_new = ui.sliderFloat('Human', settings.humanization, 0, 1,
                                 string.format('%.0f%%', settings.humanization * 100))
    if math.abs(h_new - settings.humanization) > 0.01 then
        settings.humanization = h_new; dirty = true
    end

    local s_new = ui.sliderFloat('Smooth', settings.smoothness, 0, 1,
                                 string.format('%.0f%%', settings.smoothness * 100))
    if math.abs(s_new - settings.smoothness) > 0.01 then
        settings.smoothness = s_new; dirty = true
    end

    local v_new = ui.sliderFloat('Speed kph', settings.target_kph, 80, 220,
                                 string.format('%.0f', settings.target_kph))
    if math.abs(v_new - settings.target_kph) > 0.5 then
        settings.target_kph = v_new; dirty = true
    end

    ui.separator()

    local c3_new = ui.sliderFloat('3x gap m', settings.close_3x, 1, 10,
                                  string.format('%.1f', settings.close_3x))
    if math.abs(c3_new - settings.close_3x) > 0.05 then
        settings.close_3x = c3_new; dirty = true
    end

    local c1_new = ui.sliderFloat('1x gap m', settings.close_1x, 1, 15,
                                  string.format('%.1f', settings.close_1x))
    if math.abs(c1_new - settings.close_1x) > 0.05 then
        settings.close_1x = c1_new; dirty = true
    end

    local pd_new = ui.sliderFloat('Pass dist m', settings.pass_dist, 0.5, 8,
                                  string.format('%.1f', settings.pass_dist))
    if math.abs(pd_new - settings.pass_dist) > 0.05 then
        settings.pass_dist = pd_new; dirty = true
    end
end

-- ── Keyboard shortcuts ────────────────────────────────────────────────────────
function script.onKeyDown(key)
    if key == ac.KeyIndex.F5 then
        settings.enabled = not settings.enabled; dirty = true
    elseif key == ac.KeyIndex.F6 then
        settings.humanization = math.min(1.0, settings.humanization + 0.1); dirty = true
    elseif key == ac.KeyIndex.F7 then
        settings.humanization = math.max(0.0, settings.humanization - 0.1); dirty = true
    elseif key == ac.KeyIndex.F8 then
        settings.target_kph = math.min(220, settings.target_kph + 10); dirty = true
    elseif key == ac.KeyIndex.F9 then
        settings.target_kph = math.max(80,  settings.target_kph - 10); dirty = true
    end
end
