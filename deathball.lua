local Rayfield = loadstring(game:HttpGet("https://sirius.menu/rayfield"))()

local Players       = game:GetService("Players")
local RunService    = game:GetService("RunService")

local LocalPlayer   = Players.LocalPlayer
local Camera        = workspace.CurrentCamera

local Settings = {
    AutoParry       = false,
    AbilityFailsafe = false,
    BallCurve       = false,
    AutoReady       = false,
    BallESP         = false,
    PlayerESP       = false,
    CurveAngle      = 180,
    ParryStartup    = 0.13,  -- fire parry this many seconds before predicted impact
    ParryCooldown   = 0.8,
}

local Stats = {
    Parries       = 0,
    AbilitiesUsed = 0,
    FPS           = 0,
    LastParryTime = 0,
    TTI           = math.huge,   -- time-to-impact in seconds (for status panel)
}

local ESPObjects = {}

local function getCharacter()
    return LocalPlayer.Character
end

local function getRootPart()
    local char = getCharacter()
    return char and char:FindFirstChild("HumanoidRootPart")
end

-- Fake ball detection (Gazo's Fake Ball ability)
local function isRealBall(part)
    if part.Transparency > 0.5                    then return false end
    if part:GetAttribute("Fake") == true          then return false end
    if part.Name:lower():find("fake")             then return false end
    if part:FindFirstChild("FakeTag")             then return false end
    return true
end

local BALL_NAMES = { ball = true, deathball = true, football = true, soccerball = true }

local function findBalls()
    local balls = {}
    for _, obj in ipairs(workspace:GetDescendants()) do
        if obj:IsA("BasePart") and BALL_NAMES[obj.Name:lower()] then
            table.insert(balls, obj)
        end
    end
    return balls
end

local function getRealBall()
    local balls = findBalls()
    if #balls == 0 then return nil end
    if #balls == 1 then return isRealBall(balls[1]) and balls[1] or nil end

    -- multiple balls (Gazo fake) — pick the largest real one
    local candidates = {}
    for _, b in ipairs(balls) do
        if isRealBall(b) then table.insert(candidates, b) end
    end
    if #candidates == 0 then return nil end
    table.sort(candidates, function(a, b) return a.Size.Magnitude > b.Size.Magnitude end)
    return candidates[1]
end

-- ── Prediction engine ────────────────────────────────────────────────────────
-- ball.Velocity is jittery over the network; we maintain a rolling history of
-- real positions sampled each frame and compute a weighted-average velocity from
-- it.  Recent frames are weighted more heavily so we react to direction changes
-- quickly without losing smoothness.

local HISTORY_SIZE = 8
local ballHistory  = {}      -- { pos: Vector3, t: number }[]
local lastBallPos  = nil

local function pushBallHistory(ball)
    -- discard stale history when the ball resets / is re-served
    if lastBallPos and (ball.Position - lastBallPos).Magnitude > 60 then
        ballHistory = {}
    end
    lastBallPos = ball.Position

    table.insert(ballHistory, { pos = ball.Position, t = tick() })
    if #ballHistory > HISTORY_SIZE then
        table.remove(ballHistory, 1)
    end
end

local function getSmoothedVelocity(ball)
    if #ballHistory < 2 then return ball.Velocity end

    local weightedVel = Vector3.zero
    local totalWeight = 0

    for i = 2, #ballHistory do
        local prev = ballHistory[i - 1]
        local curr = ballHistory[i]
        local dt   = curr.t - prev.t
        if dt > 0.0005 then
            local frameVel = (curr.pos - prev.pos) / dt
            -- linear ramp: oldest frame = weight 1, newest = weight HISTORY_SIZE
            local w = i
            weightedVel = weightedVel + frameVel * w
            totalWeight = totalWeight + w
        end
    end

    return totalWeight > 0 and (weightedVel / totalWeight) or ball.Velocity
end

-- Returns seconds until ball edge touches player hitbox edge.
-- Returns math.huge when the ball is not closing in.
local function predictTTI(ball, hrp)
    local vel    = getSmoothedVelocity(ball)
    local toHRP  = hrp.Position - ball.Position
    local dist   = toHRP.Magnitude

    if dist < 0.01 then return 0 end

    local ballRadius   = ball.Size.Magnitude * 0.5
    local playerRadius = hrp.Size.Magnitude  * 0.5
    local effectiveDist = math.max(0, dist - ballRadius - playerRadius)

    -- component of velocity pointing at us
    local closingSpeed = vel:Dot(toHRP.Unit)
    if closingSpeed < 0.5 then return math.huge end

    return effectiveDist / closingSpeed
end

-- UNC input (high UNC executor — no fallbacks needed)
local function fireParry()
    pcall(mouse1click)
    pcall(function()
        keypress(0x46)       -- F
        task.delay(0.05, function() keyrelease(0x46) end)
    end)
end

local function fireAbility()
    pcall(function()
        keypress(0x45)       -- E
        task.delay(0.05, function() keyrelease(0x45) end)
    end)
end

-- Rotate HRP to aim ball, restore camera so no visual jerk
local function applyBallCurve(hrp)
    local savedCam = Camera.CFrame
    hrp.CFrame = hrp.CFrame * CFrame.Angles(0, math.rad(Settings.CurveAngle), 0)
    Camera.CFrame = savedCam
end

-- ── Auto Parry loop ───────────────────────────────────────────────────────────
-- Runs every frame. Always pushes ball history (so the velocity buffer is warm
-- even before AutoParry is toggled on). Fires the parry input when the predicted
-- time-to-impact drops to or below Settings.ParryStartup, meaning the parry
-- active window will open exactly as the ball arrives.
task.spawn(function()
    while true do
        task.wait(0.016)

        local ball = getRealBall()
        if ball then pushBallHistory(ball) end

        if not Settings.AutoParry then continue end

        local hrp = getRootPart()
        if not hrp or not ball then Stats.TTI = math.huge; continue end

        local tti = predictTTI(ball, hrp)
        Stats.TTI = tti

        local now        = tick()
        local onCooldown = (now - Stats.LastParryTime) < Settings.ParryCooldown

        if onCooldown then
            -- parry is on CD — use ability as emergency if impact is imminent
            if Settings.AbilityFailsafe and tti < 0.12 then
                fireAbility()
                Stats.AbilitiesUsed += 1
            end
            continue
        end

        if tti <= Settings.ParryStartup then
            local savedHRP = hrp.CFrame

            if Settings.BallCurve then applyBallCurve(hrp) end

            fireParry()
            Stats.Parries      += 1
            Stats.LastParryTime = now

            -- clear history so the reversed ball starts fresh
            ballHistory = {}
            lastBallPos = nil

            if Settings.BallCurve then
                task.delay(0.05, function()
                    local h = getRootPart()
                    if h then h.CFrame = savedHRP end
                end)
            end
        end
    end
end)

-- ── Auto Ready loop ───────────────────────────────────────────────────────────
task.spawn(function()
    while true do
        task.wait(1)
        if not Settings.AutoReady then continue end
        pcall(function()
            for _, gui in ipairs(LocalPlayer.PlayerGui:GetDescendants()) do
                if gui:IsA("TextButton") or gui:IsA("ImageButton") then
                    local nameHit = gui.Name:lower():find("ready")
                    local textHit = gui:IsA("TextButton") and gui.Text:lower():find("ready")
                    if nameHit or textHit then
                        pcall(function() gui.MouseButton1Click:Fire() end)
                    end
                end
            end
        end)
    end
end)

-- ── ESP ───────────────────────────────────────────────────────────────────────
local function clearESPForKey(key)
    local data = ESPObjects[key]
    if not data then return end
    pcall(function() if data.highlight then data.highlight:Destroy() end end)
    pcall(function() if data.billboard then data.billboard:Destroy() end end)
    ESPObjects[key] = nil
end

local function makeHighlight(adornee, fill, outline)
    local h = Instance.new("Highlight")
    h.FillColor          = fill
    h.OutlineColor       = outline
    h.FillTransparency   = 0.55
    h.OutlineTransparency = 0
    h.DepthMode          = Enum.HighlightDepthMode.AlwaysOnTop
    h.Adornee            = adornee
    h.Parent             = adornee
    return h
end

local function createBallESP(ball)
    if ESPObjects[ball] then return end
    ESPObjects[ball] = {
        highlight = makeHighlight(ball, Color3.fromRGB(255, 230, 0), Color3.fromRGB(255, 200, 0))
    }
end

local function createPlayerESP(player)
    if player == LocalPlayer then return end
    local char = player.Character
    if not char then return end

    clearESPForKey(player)

    local highlight = makeHighlight(char, Color3.fromRGB(255, 50, 50), Color3.fromRGB(255, 0, 0))
    local billboard, label

    local head = char:FindFirstChild("Head") or char:FindFirstChild("HumanoidRootPart")
    if head then
        billboard = Instance.new("BillboardGui")
        billboard.Size         = UDim2.new(0, 150, 0, 36)
        billboard.StudsOffset  = Vector3.new(0, 3, 0)
        billboard.AlwaysOnTop  = true
        billboard.Adornee      = head
        billboard.Parent       = head

        label = Instance.new("TextLabel")
        label.Size                  = UDim2.new(1, 0, 1, 0)
        label.BackgroundTransparency = 1
        label.TextColor3            = Color3.fromRGB(255, 110, 110)
        label.TextStrokeTransparency = 0
        label.Font                  = Enum.Font.GothamBold
        label.TextSize              = 13
        label.Text                  = player.Name
        label.Parent                = billboard

        -- live distance update
        task.spawn(function()
            local hrpRef = char:FindFirstChild("HumanoidRootPart")
            while billboard and billboard.Parent and hrpRef and hrpRef.Parent do
                task.wait(0.1)
                local myHRP = getRootPart()
                if myHRP then
                    local d = math.floor((myHRP.Position - hrpRef.Position).Magnitude)
                    label.Text = player.Name .. "  " .. d .. " st"
                end
            end
        end)
    end

    ESPObjects[player] = { highlight = highlight, billboard = billboard }
end

task.spawn(function()
    while true do
        task.wait(0.5)

        -- prune stale entries
        for key, data in pairs(ESPObjects) do
            local alive = pcall(function() return key.Parent ~= nil end)
            if not alive then clearESPForKey(key) end
        end

        if Settings.BallESP then
            for _, ball in ipairs(findBalls()) do
                if ball.Parent and isRealBall(ball) then createBallESP(ball) end
            end
        end

        if Settings.PlayerESP then
            for _, p in ipairs(Players:GetPlayers()) do
                if p ~= LocalPlayer and p.Character then createPlayerESP(p) end
            end
        end
    end
end)

-- FPS counter
local fpsFrames, fpsTimer = 0, 0
RunService.RenderStepped:Connect(function(dt)
    fpsFrames += 1
    fpsTimer  += dt
    if fpsTimer >= 1 then
        Stats.FPS = fpsFrames
        fpsFrames = 0
        fpsTimer  = 0
    end
end)

-- reset cooldown on respawn
LocalPlayer.CharacterAdded:Connect(function()
    Stats.LastParryTime = 0
end)

-- cleanup ESP when a player leaves
Players.PlayerRemoving:Connect(function(p)
    clearESPForKey(p)
end)

-- ── GUI ───────────────────────────────────────────────────────────────────────
local Window = Rayfield:CreateWindow({
    Name            = "Death Ball / Vibe Code",
    LoadingTitle    = "Death Ball",
    LoadingSubtitle = "Vibe Code",
    ConfigurationSaving = { Enabled = false },
    KeySystem       = false,
})

local MainTab = Window:CreateTab("Main", 4483362458)
local ESPTab  = Window:CreateTab("ESP",  4483362458)

MainTab:CreateSection("Combat")

MainTab:CreateToggle({
    Name = "Auto Parry  (+ Gazo Prediction)",
    CurrentValue = false, Flag = "AutoParry",
    Callback = function(v)
        Settings.AutoParry = v
        Rayfield:Notify({ Title = "Auto Parry", Content = v and "Enabled" or "Disabled", Duration = 3 })
    end,
})

MainTab:CreateToggle({
    Name = "Ability Failsafe  (on parry CD)",
    CurrentValue = false, Flag = "AbilityFailsafe",
    Callback = function(v)
        Settings.AbilityFailsafe = v
        Rayfield:Notify({ Title = "Ability Failsafe", Content = v and "Enabled" or "Disabled", Duration = 3 })
    end,
})

MainTab:CreateToggle({
    Name = "Ball Curve  (no cam movement)",
    CurrentValue = false, Flag = "BallCurve",
    Callback = function(v)
        Settings.BallCurve = v
        Rayfield:Notify({ Title = "Ball Curve", Content = v and "Enabled" or "Disabled", Duration = 3 })
    end,
})

MainTab:CreateSection("Automation")

MainTab:CreateToggle({
    Name = "Auto Ready  (lobby)",
    CurrentValue = false, Flag = "AutoReady",
    Callback = function(v)
        Settings.AutoReady = v
        Rayfield:Notify({ Title = "Auto Ready", Content = v and "Enabled" or "Disabled", Duration = 3 })
    end,
})

MainTab:CreateSection("Tuning")

MainTab:CreateSlider({
    Name = "Curve Angle", Range = { 0, 360 }, Increment = 5,
    Suffix = "°", CurrentValue = 180, Flag = "CurveAngle",
    Callback = function(v) Settings.CurveAngle = v end,
})

-- Parry startup = how many ms before impact to fire the input.
-- Tune this to match your ping + the game's parry animation startup.
-- Lower  = fires closer to impact (better for low ping).
-- Higher = fires earlier (compensates for high ping or slow parry startup).
MainTab:CreateSlider({
    Name = "Parry Startup Offset", Range = { 50, 400 }, Increment = 5,
    Suffix = " ms", CurrentValue = 130, Flag = "ParryStartup",
    Callback = function(v) Settings.ParryStartup = v / 1000 end,
})

MainTab:CreateSlider({
    Name = "Parry Cooldown", Range = { 3, 20 }, Increment = 1,
    Suffix = " t/10s", CurrentValue = 8, Flag = "ParryCooldown",
    Callback = function(v) Settings.ParryCooldown = v / 10 end,
})

MainTab:CreateSection("Status")

local StatusPara = MainTab:CreateParagraph({ Title = "Live", Content = "loading..." })

task.spawn(function()
    while true do
        task.wait(0.25)
        local ttiStr = Stats.TTI == math.huge and "—"
            or string.format("%.2fs", Stats.TTI)
        local cdLeft = Settings.ParryCooldown - (tick() - Stats.LastParryTime)
        local cdStr  = cdLeft > 0 and string.format("%.1fs", cdLeft) or "Ready"

        local function sw(b) return b and "ON" or "off" end

        pcall(function()
            StatusPara:Set({
                Title = "Live",
                Content = string.format(
                    "FPS: %d   TTI: %s   CD: %s\n"
                 .. "Parries: %d   Abilities: %d\n"
                 .. "Parry:%s  Fail:%s  Curve:%s  Ready:%s",
                    Stats.FPS, ttiStr, cdStr,
                    Stats.Parries, Stats.AbilitiesUsed,
                    sw(Settings.AutoParry), sw(Settings.AbilityFailsafe),
                    sw(Settings.BallCurve),  sw(Settings.AutoReady)
                ),
            })
        end)
    end
end)

ESPTab:CreateSection("Visual")

ESPTab:CreateToggle({
    Name = "Ball ESP  (yellow)", CurrentValue = false, Flag = "BallESP",
    Callback = function(v)
        Settings.BallESP = v
        if not v then
            for key, _ in pairs(ESPObjects) do
                if typeof(key) == "Instance" and key:IsA("BasePart") then clearESPForKey(key) end
            end
        end
        Rayfield:Notify({ Title = "Ball ESP", Content = v and "Enabled" or "Disabled", Duration = 3 })
    end,
})

ESPTab:CreateToggle({
    Name = "Player ESP  (red + distance)", CurrentValue = false, Flag = "PlayerESP",
    Callback = function(v)
        Settings.PlayerESP = v
        if not v then
            for key, _ in pairs(ESPObjects) do
                if typeof(key) == "Instance" and key:IsA("Player") then clearESPForKey(key) end
            end
        end
        Rayfield:Notify({ Title = "Player ESP", Content = v and "Enabled" or "Disabled", Duration = 3 })
    end,
})

ESPTab:CreateButton({
    Name = "Destroy GUI",
    Callback = function()
        for key, _ in pairs(ESPObjects) do clearESPForKey(key) end
        Rayfield:Destroy()
    end,
})

Rayfield:Notify({
    Title    = "Death Ball Loaded",
    Content  = "All systems ready.",
    Duration = 5,
})
