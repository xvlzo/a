local Rayfield = loadstring(game:HttpGet("https://sirius.menu/rayfield"))()

-- Ensure math.random is seeded uniquely each session.
-- Some executors use a fixed default seed; this prevents deterministic jitter.
math.randomseed(tick() * 1e5 + os.clock() * 1e8)
math.random(); math.random()   -- discard first two values (common Lua practice)

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
-- Base values are then fuzzed at load time so no two sessions share the same
-- behavioral fingerprint — an AC building a signature from timing histograms
-- will get a different distribution each run.

local Stealth = {
    Enabled        = true,
    JitterMs       = 22,    -- add 0–2×JitterMs ms random delay after trigger fires
    MissChance     = 0.05,  -- 0–1 probability to intentionally skip a parry
    MinReactionMs  = 85,    -- never fire when TTI < this (no human reacts faster)
    MaxConsecutive = 14,    -- force a miss after this many parries in a row
    HoldVariance   = true,  -- randomize key hold duration
    CooldownNoise  = 55,    -- ±ms of noise added to cooldown gate each check
    CurveNoiseDeg  = 5,     -- ±degrees of random offset on ball curve angle
    MouseRatio     = 0.35,  -- probability to use LMB instead of F key
    WarmupSec      = 1.8,   -- extra reaction delay for first N seconds after spawn
    FailsafeTTI    = 0.10,  -- TTI threshold for ability failsafe trigger
}

-- Session fingerprint randomization: each script load gets slightly different
-- timing parameters so no two sessions produce the same statistical distribution.
do
    local r = math.random
    Stealth.JitterMs       = 17 + r(0, 10)          -- 17–27ms
    Stealth.MissChance     = 0.035 + r() * 0.025    -- 3.5–6%
    Stealth.MinReactionMs  = 78 + r(0, 14)          -- 78–92ms
    Stealth.MaxConsecutive = 11 + r(0, 6)           -- 11–17
    Stealth.CooldownNoise  = 42 + r(0, 26)          -- 42–68ms
    Stealth.CurveNoiseDeg  = 3  + r(0, 4)           -- 3–7°
    Stealth.MouseRatio     = 0.25 + r() * 0.20      -- 25–45%
    Stealth.WarmupSec      = 1.2 + r() * 1.2        -- 1.2–2.4s
    Stealth.FailsafeTTI    = 0.08 + r() * 0.04      -- 80–120ms
end

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

local function isAlive()
    local char = LocalPlayer.Character
    local hum  = char and char:FindFirstChildOfClass("Humanoid")
    return hum ~= nil and hum.Health > 0
end

-- ── Ball + prediction state ─────────────────────────────────────────────────
-- Declared here so getRealBall() (which reads lastBallPos) and pushBallHistory()
-- (which writes it) both close over the same local — Lua scoping requires
-- declarations to precede the functions that reference them.

local HISTORY_SIZE   = 8
local ballHistory    = {}
local lastBallPos    = nil
local lastBounceTime = 0    -- tick() of last direction-reversal reset; used for post-bounce jitter

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
    local best, bestScore = nil, -1
    for part in pairs(cachedBalls) do
        if isRealBall(part) then
            -- Primary signal: prefer the ball we're already tracking in history
            -- (continuity beats size — Gazo's fake appears at a different location)
            local score = part.Size.Magnitude
            if lastBallPos and (part.Position - lastBallPos).Magnitude < 12 then
                score = score + 100
            end
            if score > bestScore then best, bestScore = part, score end
        end
    end
    return best
end

-- ── Prediction engine ─────────────────────────────────────────────────────────

-- isBounce=true: mid-flight direction change or re-serve; sets lastBounceTime so
-- humanDelay() can add post-bounce hesitation.  Call without the flag for
-- spawn-resets or post-parry flushes where no hesitation is wanted.
local function resetBallHistory(isBounce)
    if isBounce and #ballHistory >= 2 then lastBounceTime = tick() end
    ballHistory = {}
    lastBallPos = nil
end

local function pushBallHistory(ball)
    if lastBallPos then
        local delta = ball.Position - lastBallPos
        -- Large jump: ball re-served or teleported — re-acquire from scratch
        if delta.Magnitude > 60 then
            resetBallHistory(true)
        -- Direction reversal (>120°): ball bounced off another player — flush old
        -- trajectory so the velocity estimate corrects in 1–2 frames instead of ~8
        elseif #ballHistory >= 2 and delta.Magnitude > 0.01 then
            local prev = ballHistory[#ballHistory].pos - ballHistory[math.max(1, #ballHistory-1)].pos
            if prev.Magnitude > 0.01 and delta.Unit:Dot(prev.Unit) < -0.5 then
                resetBallHistory(true)
            end
        end
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
local postMissBump       = 0     -- extra miss chance for next N parries after a miss
local roundPerformance   = 1.0   -- per-round multiplier on miss chance (0.7–1.3)
local roundTTIOffsetMs   = 0     -- per-round TTI trigger offset (–8 to +8 ms) for histogram variance
local perParryStartupMs  = 0     -- per-parry startup offset, resampled each fire
local spawnTime          = 0     -- tick() at last respawn, used for warm-up window
local lastAbilityTime    = 0     -- prevents double-firing ability on successive frames

-- Per-parry startup offset: resampled each time we fire, ±15ms.
-- Defined here (before its call at the end of this block) so the local is in scope.
local function resamplePerParryStartup()
    perParryStartupMs = (math.random(-150, 150)) / 10   -- –15 to +15ms
end

-- Resample round-level performance — called on each new character / round
local function resampleRoundPerformance()
    -- Uniform [0.7, 1.3]: some rounds you play better, some worse
    roundPerformance   = 0.7 + math.random() * 0.6
    -- Small per-round shift of when we pull the trigger — spreads the per-session
    -- TTI-at-fire histogram so it doesn't cluster at a fixed offset from parryStartup
    roundTTIOffsetMs   = math.random(-8, 8)
end

-- Extra startup added in the first WarmupSec seconds after spawning.
-- Simulates a player orienting themselves before full reaction speed.
-- Capped at 75ms so we never fire parry when ball is unreasonably far away.
local function warmupExtraMs()
    if not Stealth.Enabled or Stealth.WarmupSec <= 0 then return 0 end
    local elapsed = tick() - spawnTime
    if elapsed >= Stealth.WarmupSec then return 0 end
    return math.min(75, 75 * (1 - elapsed / Stealth.WarmupSec))
end

resampleRoundPerformance()  -- initial sample at load
resamplePerParryStartup()   -- so first parry isn't always at 0ms offset

local function shouldMiss()
    if not Stealth.Enabled then return false end

    -- Forced miss after long streak
    if consecutiveParries >= Stealth.MaxConsecutive then
        consecutiveParries = 0
        postMissBump = math.random(1, 3)   -- humans fumble 1–3 more after a streak break
        return true
    end

    -- Base miss chance scaled by round performance + post-miss cluster bump
    local chance = Stealth.MissChance * roundPerformance
    if postMissBump > 0 then
        chance = chance + 0.09             -- elevated ~9% extra while fumbling
        postMissBump -= 1
    end

    if math.random() < chance then
        consecutiveParries = 0
        postMissBump = math.random(0, 2)   -- might fumble another 0–2 more
        return true
    end

    return false
end

-- Never fire faster than a human physically can react.
-- Clamps effective floor to 90% of current ParryStartup so that if the user
-- sets MinReactionMs > ParryStartup (e.g. 200ms + 50ms startup), the gate
-- doesn't block all parries and silently break the script.
local function tooFastForHuman(tti)
    if not Stealth.Enabled then return false end
    local floor = math.min(Stealth.MinReactionMs / 1000, Settings.ParryStartup * 0.9)
    return tti < floor
end

-- Random delay added after trigger condition is met.
-- Adds extra hesitation immediately after a ball direction-reversal (bounce),
-- simulating the moment a human loses and then re-acquires ball tracking.
local function humanDelay()
    if not Stealth.Enabled or Stealth.JitterMs <= 0 then return 0 end
    local base      = gaussMs(Stealth.JitterMs * 2)
    local bounceAge = tick() - lastBounceTime
    if bounceAge < 0.25 then
        -- Up to 30ms extra jitter, decaying linearly over 250ms post-bounce
        base = base + gaussMs(30) * (1 - bounceAge / 0.25)
    end
    return base
end

local function currentParryStartup(base)
    return base + (perParryStartupMs / 1000)
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

-- Randomly alternate between F key and LMB so neither input method dominates.
-- Falls back to F key if mouse1press/mouse1release are unavailable on this executor.
local function pressParry(hold)
    hold = hold or 0.05
    local useMouse = Stealth.Enabled and math.random() < Stealth.MouseRatio
    if useMouse then
        local ok = pcall(function()
            mouse1press()
            task.delay(hold, function() pcall(mouse1release) end)
        end)
        if ok then return end
        -- mouse functions unavailable — fall through to F key
    end
    pcall(function()
        keypress(0x46)
        task.delay(hold, function() keyrelease(0x46) end)
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

    -- Restore orientation and camera on the very next engine step.
    -- Both happen before the next character replication packet (~50ms cadence),
    -- so the server almost never captures the rotated state.
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

-- Returns the effective parry startup for this frame.
-- roundTTIOffsetMs varies each round so the per-session TTI-at-fire histogram
-- never converges to a single spike that an AC could use as a signature.
local function currentEffectiveStartup(base)
    return currentParryStartup(base) + warmupExtraMs() / 1000 + roundTTIOffsetMs / 1000
end

-- ── Auto Parry loop ───────────────────────────────────────────────────────────

task.spawn(function()
    while true do
        RunService.Heartbeat:Wait()

        local ball = getRealBall()
        if ball then pushBallHistory(ball) end

        if not Settings.AutoParry then continue end

        -- Don't fire inputs while dead (suspicious + useless)
        if not isAlive() then Stats.TTI = math.huge; continue end

        local hrp = getRootPart()
        if not hrp or not ball then Stats.TTI = math.huge; continue end

        -- Snapshot settings once per frame to insulate against mid-frame slider changes
        local parryStartup  = currentEffectiveStartup(Settings.ParryStartup)
        local parryCooldown = effectiveCooldown(Settings.ParryCooldown)

        local tti = predictTTI(ball, hrp)
        Stats.TTI = tti

        local now        = tick()
        local onCooldown = (now - Stats.LastParryTime) < parryCooldown

        if onCooldown then
            local abilityReady = (now - lastAbilityTime) > (Settings.ParryCooldown + 0.3)
            if Settings.AbilityFailsafe and tti < Stealth.FailsafeTTI and abilityReady then
                lastAbilityTime = now   -- gate immediately, before the delay fires
                task.delay(gaussMs(40), function()
                    if isAlive() then
                        pressE()
                        Stats.AbilitiesUsed += 1
                    end
                end)
            end
            continue
        end

        -- Gate: physically impossible reaction time
        if tooFastForHuman(tti) then continue end

        if tti <= parryStartup then
            -- Intentional miss — happens BEFORE marking cooldown so ball actually hits
            if shouldMiss() then
                Stats.Misses += 1
                continue
            end

            -- Resample per-parry startup for the NEXT fire (not this one)
            resamplePerParryStartup()

            -- Pre-mark cooldown so jitter delay can't cause a double-fire
            Stats.LastParryTime = now

            local savedLook = hrp.CFrame.LookVector

            local function executeParry()
                local h = getRootPart()
                if not h or not isAlive() then return end

                if Settings.BallCurve then applyAndRestoreCurve(h, savedLook) end

                pressParry(holdDuration())

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
-- Interval is jittered 0.9–1.8s so it doesn't fire at a machine-regular cadence.
-- After clicking ready, backs off for 6–10s before trying again — humans click
-- it once, not every second.

local lastReadyClick = 0

task.spawn(function()
    while true do
        task.wait(0.9 + math.random() * 0.9)
        if not Settings.AutoReady then continue end

        local now = tick()
        local debounce = 6 + math.random() * 4   -- 6–10s between ready clicks
        if (now - lastReadyClick) < debounce then continue end

        pcall(function()
            for _, gui in ipairs(LocalPlayer.PlayerGui:GetDescendants()) do
                if gui:IsA("TextButton") or gui:IsA("ImageButton") then
                    local nameHit = gui.Name:lower():find("ready")
                    local textHit = gui:IsA("TextButton") and gui.Text:lower():find("ready")
                    if nameHit or textHit then
                        gui:Activate()
                        lastReadyClick = now
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
    local t             = tick()
    Stats.LastParryTime = 0
    lastAbilityTime     = 0
    consecutiveParries  = 0
    postMissBump        = 0
    lastBounceTime      = 0
    spawnTime           = t
    resetBallHistory()
    resampleRoundPerformance()
    resamplePerParryStartup()
end)

spawnTime = tick()  -- initialise for the first life

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
                 .. "Streak: %d/%d   Perf: %.0f%%   Fumble: %d\n"
                 .. "Stealth:%s  Parry:%s  Fail:%s  Curve:%s",
                    Stats.FPS, ttiStr, cdStr,
                    Stats.Parries, Stats.Misses, Stats.AbilitiesUsed,
                    consecutiveParries, Stealth.MaxConsecutive,
                    roundPerformance * 100, postMissBump,
                    sw(Stealth.Enabled), sw(Settings.AutoParry),
                    sw(Settings.AbilityFailsafe), sw(Settings.BallCurve)
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

-- Note: CurrentValue mirrors the session-fingerprinted value so the slider
-- display matches reality and any on-init callback doesn't override it.
StealthTab:CreateSlider({
    Name = "Timing Jitter", Range = { 0, 60 }, Increment = 1,
    Suffix = " ms", CurrentValue = Stealth.JitterMs, Flag = "StealthJitter",
    Callback = function(v) Stealth.JitterMs = v end,
})

StealthTab:CreateSlider({
    Name = "Min Reaction Time", Range = { 50, 200 }, Increment = 5,
    Suffix = " ms", CurrentValue = Stealth.MinReactionMs, Flag = "StealthMinReact",
    Callback = function(v) Stealth.MinReactionMs = v end,
})

StealthTab:CreateSlider({
    Name = "Cooldown Noise", Range = { 0, 150 }, Increment = 5,
    Suffix = " ms", CurrentValue = Stealth.CooldownNoise, Flag = "StealthCDNoise",
    Callback = function(v) Stealth.CooldownNoise = v end,
})

StealthTab:CreateSection("Miss Pattern")

StealthTab:CreateSlider({
    Name = "Miss Chance", Range = { 0, 20 }, Increment = 1,
    Suffix = " %", CurrentValue = math.floor(Stealth.MissChance * 100 + 0.5),
    Flag = "StealthMiss",
    Callback = function(v) Stealth.MissChance = v / 100 end,
})

StealthTab:CreateSlider({
    Name = "Max Consecutive Parries", Range = { 5, 35 }, Increment = 1,
    Suffix = "", CurrentValue = Stealth.MaxConsecutive, Flag = "StealthMaxStreak",
    Callback = function(v) Stealth.MaxConsecutive = v end,
})

StealthTab:CreateSection("Curve Variation")

StealthTab:CreateSlider({
    Name = "Curve Angle Noise", Range = { 0, 15 }, Increment = 1,
    Suffix = " °", CurrentValue = Stealth.CurveNoiseDeg, Flag = "StealthCurveNoise",
    Callback = function(v) Stealth.CurveNoiseDeg = v end,
})

Rayfield:Notify({
    Title   = "Death Ball Loaded",
    Content = "All systems ready. Stealth ON.",
    Duration = 5,
})
