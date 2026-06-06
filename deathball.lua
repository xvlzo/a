local Rayfield = loadstring(game:HttpGet("https://sirius.menu/rayfield"))()

local Players    = game:GetService("Players")
local RunService = game:GetService("RunService")

local LocalPlayer = Players.LocalPlayer

local Settings = {
    AutoParry       = false,
    AbilityFailsafe = false,
    BallCurve       = false,
    AutoReady       = false,
    CurveAngle      = 180,
    ParryStartup    = 0.13,   -- fire parry this many seconds before predicted impact
    ParryCooldown   = 0.8,
}

local Stats = {
    Parries       = 0,
    AbilitiesUsed = 0,
    FPS           = 0,
    LastParryTime = 0,
    TTI           = math.huge,
}

-- ── Character helpers ─────────────────────────────────────────────────────────

local function getCharacter()
    return LocalPlayer.Character
end

local function getRootPart()
    local char = getCharacter()
    return char and char:FindFirstChild("HumanoidRootPart")
end

-- ── Ball cache (avoid scanning workspace every frame) ─────────────────────────

local BALL_NAMES = { ball = true, deathball = true, football = true, soccerball = true }
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

-- initial populate
for _, obj in ipairs(workspace:GetDescendants()) do
    onDescendantAdded(obj)
end

-- Fake ball detection: Gazo's Fake Ball is typically transparent, flagged,
-- or smaller than the real ball.
local function isRealBall(part)
    if not part.Parent               then return false end
    if part.Transparency >= 0.5      then return false end
    if part:GetAttribute("Fake")     then return false end
    if part.Name:lower():find("fake") then return false end
    if part:FindFirstChild("FakeTag") then return false end
    return true
end

local function getRealBall()
    local best, bestSize = nil, -1
    for part in pairs(cachedBalls) do
        if isRealBall(part) then
            local sz = part.Size.Magnitude
            if sz > bestSize then
                best, bestSize = part, sz
            end
        end
    end
    return best
end

-- ── Prediction engine ─────────────────────────────────────────────────────────
-- ball.Velocity replicates with network jitter; we track real positions and
-- compute a recency-weighted average velocity for stable prediction.

local HISTORY_SIZE = 8
local ballHistory  = {}
local lastBallPos  = nil

local function resetBallHistory()
    ballHistory = {}
    lastBallPos = nil
end

local function pushBallHistory(ball)
    -- reset history on large position jumps (ball re-served / teleported)
    if lastBallPos and (ball.Position - lastBallPos).Magnitude > 60 then
        resetBallHistory()
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
    local totalWeight = 0.0

    for i = 2, #ballHistory do
        local prev = ballHistory[i - 1]
        local curr = ballHistory[i]
        local dt   = curr.t - prev.t
        if dt >= 0.001 then
            local frameVel = (curr.pos - prev.pos) / dt
            local w = i   -- newer frames get higher index = higher weight
            weightedVel = weightedVel + frameVel * w
            totalWeight = totalWeight + w
        end
    end

    return totalWeight > 0 and (weightedVel / totalWeight) or ball.Velocity
end

-- Returns seconds until ball surface reaches player hitbox surface.
-- Returns math.huge when the ball is not closing in on the player.
local function predictTTI(ball, hrp)
    local vel   = getSmoothedVelocity(ball)
    local toHRP = hrp.Position - ball.Position
    local dist  = toHRP.Magnitude

    if dist < 0.01 then return 0 end

    -- Use the narrowest cross-section for a tighter (more accurate) radius.
    -- Size.Magnitude would be the diagonal (~1.73x too large).
    local ballRadius   = math.min(ball.Size.X, ball.Size.Y, ball.Size.Z) * 0.5
    local playerRadius = math.min(hrp.Size.X,  hrp.Size.Y,  hrp.Size.Z) * 0.5
    local effectiveDist = math.max(0, dist - ballRadius - playerRadius)

    local closingSpeed = vel:Dot(toHRP.Unit)
    if closingSpeed <= 0 then return math.huge end   -- not closing in

    return effectiveDist / closingSpeed
end

-- ── Input ─────────────────────────────────────────────────────────────────────

local function fireParry()
    -- F key only — avoids the risk of mouse1click() accidentally hitting GUI
    pcall(function()
        keypress(0x46)
        task.delay(0.05, function() keyrelease(0x46) end)
    end)
end

local function fireAbility()
    pcall(function()
        keypress(0x45)   -- E
        task.delay(0.05, function() keyrelease(0x45) end)
    end)
end

-- ── Ball curve ────────────────────────────────────────────────────────────────
-- Rotates HRP so the ball reflects in the desired direction, then restores the
-- original yaw at the current position so movement isn't teleported backward.
-- Locks the camera to Scriptable for one frame to prevent it fighting the rotation.

local function applyBallCurve(hrp, savedLookXZ)
    local cam = workspace.CurrentCamera
    local savedCamType = cam.CameraType
    local savedCamCF   = cam.CFrame

    cam.CameraType = Enum.CameraType.Scriptable
    hrp.CFrame = hrp.CFrame * CFrame.Angles(0, math.rad(Settings.CurveAngle), 0)
    cam.CFrame = savedCamCF   -- suppress visual jerk this frame

    task.defer(function()
        -- return camera to normal after one engine step
        cam.CameraType = savedCamType
    end)
end

local function restoreHRPOrientation(hrp, savedLook)
    -- Restore original yaw at *current* position — never snap the player back
    local newLook = Vector3.new(savedLook.X, 0, savedLook.Z)
    if newLook.Magnitude > 0.01 then
        hrp.CFrame = CFrame.lookAt(hrp.Position, hrp.Position + newLook)
    end
end

-- ── Auto Parry loop ───────────────────────────────────────────────────────────
-- History is always updated (runs even when AutoParry is off) so the velocity
-- buffer is warm the moment you enable the toggle.

task.spawn(function()
    while true do
        RunService.Heartbeat:Wait()   -- true per-frame, not approximate 16ms sleep

        local ball = getRealBall()
        if ball then pushBallHistory(ball) end

        if not Settings.AutoParry then continue end

        local hrp = getRootPart()
        if not hrp or not ball then Stats.TTI = math.huge; continue end

        -- snapshot settings once per frame so mid-frame slider changes don't interfere
        local parryStartup  = Settings.ParryStartup
        local parryCooldown = Settings.ParryCooldown

        local tti = predictTTI(ball, hrp)
        Stats.TTI = tti

        local now        = tick()
        local onCooldown = (now - Stats.LastParryTime) < parryCooldown

        if onCooldown then
            if Settings.AbilityFailsafe and tti < 0.1 then
                fireAbility()
                Stats.AbilitiesUsed += 1
            end
            continue
        end

        if tti <= parryStartup then
            local savedLook = hrp.CFrame.LookVector

            if Settings.BallCurve then
                applyBallCurve(hrp)
            end

            fireParry()
            Stats.Parries      += 1
            Stats.LastParryTime = now

            -- Reset history — the ball now travels in a new direction
            resetBallHistory()

            if Settings.BallCurve then
                task.delay(0.06, function()
                    local h = getRootPart()
                    if h then restoreHRPOrientation(h, savedLook) end
                end)
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
                        -- :Activate() is the correct API for programmatic button press
                        gui:Activate()
                        break   -- click once per cycle, not every matching element
                    end
                end
            end
        end)
    end
end)

-- ── FPS counter ───────────────────────────────────────────────────────────────

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

-- ── Respawn handling ──────────────────────────────────────────────────────────

LocalPlayer.CharacterAdded:Connect(function(char)
    -- Wait for humanoid so everything is loaded before resetting state
    char:WaitForChild("Humanoid")
    Stats.LastParryTime = 0
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

local MainTab = Window:CreateTab("Main", 4483362458)

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
    Name = "Ability Failsafe  (uses E when parry on CD)",
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

-- Lower = fires closer to impact (low ping). Higher = fires earlier (high ping / slow parry startup).
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

MainTab:CreateButton({
    Name = "Destroy GUI",
    Callback = function()
        Rayfield:Destroy()
    end,
})

Rayfield:Notify({
    Title    = "Death Ball Loaded",
    Content  = "All systems ready.",
    Duration = 5,
})
