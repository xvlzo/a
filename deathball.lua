local Rayfield = loadstring(game:HttpGet("https://sirius.menu/rayfield"))()

local Players    = game:GetService("Players")
local RunService = game:GetService("RunService")

local LocalPlayer = Players.LocalPlayer

-- ── Feature settings ──────────────────────────────────────────────────────────

local Settings = {
    AutoParry       = false,
    AbilityFailsafe = false,
    BallCurve       = false,
    AutoReady       = false,
    CurveAngle      = 180,
    ParryStartup    = 0.13,
    ParryCooldown   = 0.8,
}

-- ── Anti-detection settings ───────────────────────────────────────────────────
-- Every value here makes the parry pattern look less machine-generated.

local Stealth = {
    Enabled        = true,
    JitterMs       = 22,    -- add 0–2×JitterMs ms random delay after trigger fires
    MissChance     = 0.05,  -- 0–1 probability to intentionally skip a parry
    MinReactionMs  = 85,    -- never fire when TTI < this (no human reacts faster)
    MaxConsecutive = 14,    -- force a miss after this many parries in a row
    HoldVariance   = true,  -- randomize key hold duration
    CooldownNoise  = 55,    -- ±ms of noise added to cooldown gate each check
    CurveNoiseDeg  = 5,     -- ±degrees of random offset on ball curve angle
}

local Stats = {
    Parries       = 0,
    Misses        = 0,
    AbilitiesUsed = 0,
    FPS           = 0,
    LastParryTime = 0,
    TTI           = math.huge,
}

-- ── Character helpers ─────────────────────────────────────────────────────────

local function getRootPart()
    local char = LocalPlayer.Character
    return char and char:FindFirstChild("HumanoidRootPart")
end

-- ── Ball cache ────────────────────────────────────────────────────────────────

local BALL_NAMES  = { ball = true, deathball = true, football = true, soccerball = true }
local cachedBalls = {}

local function onDescendantAdded(obj)
    if obj:IsA("BasePart") and BALL_NAMES[obj.Name:lower()] then
        cachedBalls[obj] = true
    end
end

local function onDescendantRemoving(obj)
    cachedBalls[obj] = nil
end

workspace.DescendantAdded:Connect(onDescendantAdded)
workspace.DescendantRemoving:Connect(onDescendantRemoving)

for _, obj in ipairs(workspace:GetDescendants()) do onDescendantAdded(obj) end

local function isRealBall(part)
    if not part.Parent                then return false end
    if part.Transparency >= 0.5       then return false end
    if part:GetAttribute("Fake")      then return false end
    if part.Name:lower():find("fake") then return false end
    if part:FindFirstChild("FakeTag") then return false end
    return true
end

local function getRealBall()
    local best, bestSz = nil, -1
    for part in pairs(cachedBalls) do
        if isRealBall(part) then
            local sz = part.Size.Magnitude
            if sz > bestSz then best, bestSz = part, sz end
        end
    end
    return best
end

-- ── Prediction engine ─────────────────────────────────────────────────────────

local HISTORY_SIZE = 8
local ballHistory  = {}
local lastBallPos  = nil

local function resetBallHistory()
    ballHistory = {}
    lastBallPos = nil
end

local function pushBallHistory(ball)
    if lastBallPos and (ball.Position - lastBallPos).Magnitude > 60 then
        resetBallHistory()
    end
    lastBallPos = ball.Position
    table.insert(ballHistory, { pos = ball.Position, t = tick() })
    if #ballHistory > HISTORY_SIZE then table.remove(ballHistory, 1) end
end

local function getSmoothedVelocity(ball)
    if #ballHistory < 2 then return ball.Velocity end
    local wVel, wTotal = Vector3.zero, 0.0
    for i = 2, #ballHistory do
        local prev, curr = ballHistory[i-1], ballHistory[i]
        local dt = curr.t - prev.t
        if dt >= 0.001 then
            local w = i
            wVel   = wVel   + (curr.pos - prev.pos) / dt * w
            wTotal = wTotal + w
        end
    end
    return wTotal > 0 and (wVel / wTotal) or ball.Velocity
end

local function predictTTI(ball, hrp)
    local vel   = getSmoothedVelocity(ball)
    local toHRP = hrp.Position - ball.Position
    local dist  = toHRP.Magnitude
    if dist < 0.01 then return 0 end

    local ballRadius   = math.min(ball.Size.X, ball.Size.Y, ball.Size.Z) * 0.5
    local playerRadius = math.min(hrp.Size.X,  hrp.Size.Y,  hrp.Size.Z)  * 0.5
    local effDist      = math.max(0, dist - ballRadius - playerRadius)

    local closing = vel:Dot(toHRP.Unit)
    if closing <= 0 then return math.huge end
    return effDist / closing
end

-- ── Anti-detection helpers ────────────────────────────────────────────────────

-- Two-sample average gives a mild bell-curve distribution
local function gaussMs(maxMs)
    return (math.random(0, maxMs) + math.random(0, maxMs)) / 2 / 1000
end

local consecutiveParries = 0

local function shouldMiss()
    if not Stealth.Enabled then return false end
    if consecutiveParries >= Stealth.MaxConsecutive then
        consecutiveParries = 0
        return true   -- forced miss after long streak
    end
    if math.random() < Stealth.MissChance then
        consecutiveParries = 0
        return true
    end
    return false
end

-- Never fire faster than a human physically can react
local function tooFastForHuman(tti)
    return Stealth.Enabled and tti < (Stealth.MinReactionMs / 1000)
end

-- Random delay added after trigger condition is met
local function humanDelay()
    if not Stealth.Enabled or Stealth.JitterMs <= 0 then return 0 end
    return gaussMs(Stealth.JitterMs * 2)
end

-- Variable key hold: 38–76ms instead of always 50ms
local function holdDuration()
    if not Stealth.Enabled or not Stealth.HoldVariance then return 0.05 end
    return (38 + math.random(0, 38)) / 1000
end

-- Cooldown gate varies slightly each check so the rhythm isn't clockwork
local function effectiveCooldown(base)
    if not Stealth.Enabled or Stealth.CooldownNoise <= 0 then return base end
    local noise = (math.random(0, Stealth.CooldownNoise * 2) - Stealth.CooldownNoise) / 1000
    return base + noise
end

-- Curve angle with small random offset so it's never identical twice
local function curveAngle()
    local base = Settings.CurveAngle
    if not Stealth.Enabled or Stealth.CurveNoiseDeg <= 0 then return base end
    local noise = math.random(-Stealth.CurveNoiseDeg * 10, Stealth.CurveNoiseDeg * 10) / 10
    return base + noise
end

-- ── Input ─────────────────────────────────────────────────────────────────────

local function pressF(hold)
    pcall(function()
        keypress(0x46)
        task.delay(hold or 0.05, function() keyrelease(0x46) end)
    end)
end

local function pressE()
    local hold = holdDuration()
    pcall(function()
        keypress(0x45)
        task.delay(hold, function() keyrelease(0x45) end)
    end)
end

-- ── Ball curve ────────────────────────────────────────────────────────────────
-- The HRP rotation and camera restore both happen before the next physics step.
-- Roblox sends character packets at ~20Hz; rotating and restoring within one
-- engine step (~16ms) means the server almost certainly never receives the
-- rotated state in a character replication packet.

local function applyAndRestoreCurve(hrp, savedLook)
    local cam         = workspace.CurrentCamera
    local savedCamCF  = cam.CFrame
    local savedCamTyp = cam.CameraType

    cam.CameraType = Enum.CameraType.Scriptable
    hrp.CFrame = hrp.CFrame * CFrame.Angles(0, math.rad(curveAngle()), 0)
    cam.CFrame = savedCamCF

    -- Restore orientation and camera on the very next engine step
    task.defer(function()
        cam.CameraType = savedCamTyp
        local h = getRootPart()
        if h then
            local newLook = Vector3.new(savedLook.X, 0, savedLook.Z)
            if newLook.Magnitude > 0.01 then
                h.CFrame = CFrame.lookAt(h.Position, h.Position + newLook)
            end
        end
    end)
end

-- ── Auto Parry loop ───────────────────────────────────────────────────────────

task.spawn(function()
    while true do
        RunService.Heartbeat:Wait()

        local ball = getRealBall()
        if ball then pushBallHistory(ball) end

        if not Settings.AutoParry then continue end

        local hrp = getRootPart()
        if not hrp or not ball then Stats.TTI = math.huge; continue end

        -- Snapshot settings once per frame to insulate against mid-frame slider changes
        local parryStartup  = Settings.ParryStartup
        local parryCooldown = effectiveCooldown(Settings.ParryCooldown)

        local tti = predictTTI(ball, hrp)
        Stats.TTI = tti

        local now        = tick()
        local onCooldown = (now - Stats.LastParryTime) < parryCooldown

        if onCooldown then
            if Settings.AbilityFailsafe and tti < 0.1 then
                -- Ability also jittered — instant defensive ability use is suspicious
                task.delay(gaussMs(40), function()
                    pressE()
                    Stats.AbilitiesUsed += 1
                end)
            end
            continue
        end

        -- Gate: physically impossible reaction time
        if tooFastForHuman(tti) then continue end

        if tti <= parryStartup then
            -- Intentional miss check — happens BEFORE marking cooldown so the
            -- ball still hits and the player looks human
            if shouldMiss() then
                Stats.Misses += 1
                continue
            end

            -- Pre-mark cooldown so jitter delay doesn't allow a double-fire
            Stats.LastParryTime = now

            local savedLook = hrp.CFrame.LookVector

            local function executeParry()
                local h = getRootPart()
                if not h then return end

                if Settings.BallCurve then applyAndRestoreCurve(h, savedLook) end

                pressF(holdDuration())

                Stats.Parries      += 1
                consecutiveParries += 1
                resetBallHistory()
            end

            local delay = humanDelay()
            if delay > 0.001 then
                task.delay(delay, executeParry)
            else
                executeParry()
            end
        end
    end
end)

-- ── Auto Ready ────────────────────────────────────────────────────────────────

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
                        gui:Activate()
                        break
                    end
                end
            end
        end)
    end
end)

-- ── FPS counter ───────────────────────────────────────────────────────────────

local fpsFrames, fpsTimer = 0, 0
RunService.RenderStepped:Connect(function(dt)
    fpsFrames += 1; fpsTimer += dt
    if fpsTimer >= 1 then
        Stats.FPS = fpsFrames
        fpsFrames = 0; fpsTimer = 0
    end
end)

-- ── Respawn ───────────────────────────────────────────────────────────────────

LocalPlayer.CharacterAdded:Connect(function(char)
    char:WaitForChild("Humanoid")
    Stats.LastParryTime = 0
    consecutiveParries  = 0
    resetBallHistory()
end)

-- ── GUI ───────────────────────────────────────────────────────────────────────

local Window = Rayfield:CreateWindow({
    Name            = "Death Ball / Vibe Code",
    LoadingTitle    = "Death Ball",
    LoadingSubtitle = "Vibe Code",
    ConfigurationSaving = { Enabled = false },
    KeySystem       = false,
})

local MainTab    = Window:CreateTab("Main",    4483362458)
local StealthTab = Window:CreateTab("Stealth", 4483362458)

-- ── Main tab ──────────────────────────────────────────────────────────────────

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
    Name = "Ability Failsafe  (E on parry CD)",
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
                 .. "Parries: %d   Misses: %d   Abil: %d\n"
                 .. "Streak: %d / %d   Stealth: %s\n"
                 .. "Parry:%s  Fail:%s  Curve:%s  Ready:%s",
                    Stats.FPS, ttiStr, cdStr,
                    Stats.Parries, Stats.Misses, Stats.AbilitiesUsed,
                    consecutiveParries, Stealth.MaxConsecutive, sw(Stealth.Enabled),
                    sw(Settings.AutoParry), sw(Settings.AbilityFailsafe),
                    sw(Settings.BallCurve),  sw(Settings.AutoReady)
                ),
            })
        end)
    end
end)

MainTab:CreateButton({
    Name = "Destroy GUI",
    Callback = function() Rayfield:Destroy() end,
})

-- ── Stealth tab ───────────────────────────────────────────────────────────────

StealthTab:CreateSection("Anti-Detection")

StealthTab:CreateToggle({
    Name = "Stealth Mode  (humanize all timing)",
    CurrentValue = true, Flag = "StealthEnabled",
    Callback = function(v)
        Stealth.Enabled = v
        Rayfield:Notify({ Title = "Stealth", Content = v and "Enabled" or "DISABLED — obvious mode", Duration = 4 })
    end,
})

StealthTab:CreateSection("Timing Humanization")

StealthTab:CreateSlider({
    Name = "Timing Jitter", Range = { 0, 60 }, Increment = 1,
    Suffix = " ms", CurrentValue = 22, Flag = "StealthJitter",
    Callback = function(v) Stealth.JitterMs = v end,
})

StealthTab:CreateSlider({
    Name = "Min Reaction Time", Range = { 50, 200 }, Increment = 5,
    Suffix = " ms", CurrentValue = 85, Flag = "StealthMinReact",
    Callback = function(v) Stealth.MinReactionMs = v end,
})

StealthTab:CreateSlider({
    Name = "Cooldown Noise", Range = { 0, 150 }, Increment = 5,
    Suffix = " ms", CurrentValue = 55, Flag = "StealthCDNoise",
    Callback = function(v) Stealth.CooldownNoise = v end,
})

StealthTab:CreateSection("Miss Pattern")

StealthTab:CreateSlider({
    Name = "Miss Chance", Range = { 0, 20 }, Increment = 1,
    Suffix = " %", CurrentValue = 5, Flag = "StealthMiss",
    Callback = function(v) Stealth.MissChance = v / 100 end,
})

StealthTab:CreateSlider({
    Name = "Max Consecutive Parries", Range = { 5, 35 }, Increment = 1,
    Suffix = "", CurrentValue = 14, Flag = "StealthMaxStreak",
    Callback = function(v) Stealth.MaxConsecutive = v end,
})

StealthTab:CreateSection("Curve Variation")

StealthTab:CreateSlider({
    Name = "Curve Angle Noise", Range = { 0, 15 }, Increment = 1,
    Suffix = " °", CurrentValue = 5, Flag = "StealthCurveNoise",
    Callback = function(v) Stealth.CurveNoiseDeg = v end,
})

Rayfield:Notify({
    Title   = "Death Ball Loaded",
    Content = "All systems ready. Stealth ON.",
    Duration = 5,
})
