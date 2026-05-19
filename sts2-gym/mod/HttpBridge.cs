using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.Events.Custom.CrystalSphere;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Saves;

namespace Sts2Gym;

/// <summary>
/// Day-3 P0 milestone: in-process HTTP server exposing read-only state snapshots.
///
/// Endpoints:
///   GET /health   — liveness probe, returns {"status":"ok",...}
///   GET /version  — protocol + mod version info
///   GET /observe  — current state snapshot (cached, refreshed on game events)
///
/// Threading: HttpListener spins a background thread for accept-loop. Request
/// handlers serve from a `volatile string` snapshot cache. The cache is refreshed
/// from game-thread event handlers (RunStarted / CombatSetUp / TurnStarted /
/// TurnEnded), so we never call into Godot from the HTTP thread.
///
/// Cache freshness model: events update on every meaningful state transition.
/// X-Snapshot-Age-Ms header tells the client how stale the cached payload is.
/// </summary>
internal static class HttpBridge
{
    private const string Tag = "[sts2gym/http]";
    private const string DefaultPort = "7777";
    private const int ProtocolVersion = 1;

    private static HttpListener? _listener;
    private static Thread? _thread;
    private static volatile bool _running;

    // We cache views built atomically from the same point-in-time game state.
    // This avoids any cross-thread game-state read from the HTTP listener thread
    // AND eliminates races between /observe and /action_mask — both serve from
    // the same snapshot. The action_mask is included in this set because the
    // agent picks its action based on it; if it raced with animation queue, the
    // /step that followed would land in a different state than the snapshot
    // implied, breaking trajectory determinism.
    private const string EmptyObservation =
        "{\"phase\":\"main_menu\",\"in_run\":false,\"snapshot_age_ms\":-1,\"reason\":\"no snapshot yet\"}";
    private const string EmptyActionMask =
        "{\"phase\":\"not_combat\",\"actions\":[]}";
    private static volatile string _cachedFullObs = EmptyObservation;
    private static volatile string _cachedPartialObs = EmptyObservation;
    private static volatile string _cachedActionMask = EmptyActionMask;
    private static long _lastSnapshotUtcMs;

    public static int Port { get; private set; }

    public static void Start()
    {
        try
        {
            Port = ResolvePort();
            _listener = new HttpListener();
            _listener.Prefixes.Add($"http://127.0.0.1:{Port}/");
            _listener.Start();
            _running = true;
            _thread = new Thread(AcceptLoop)
            {
                IsBackground = true,
                Name = "sts2gym-http",
            };
            _thread.Start();

            WritePortLockfile(Port);
            Log.Info($"{Tag} listening on http://127.0.0.1:{Port}/ (endpoints: /health /observe /version)");
        }
        catch (Exception ex)
        {
            // We do NOT want to break mod init if the port is busy or HttpListener fails.
            // Mod will still load, events will still fire — only the HTTP bridge will be dead.
            Log.Error($"{Tag} failed to start: {ex.Message}");
            _listener = null;
            _running = false;
        }
    }

    /// <summary>
    /// Refresh the cached observation snapshot. Called from game-thread event handlers.
    /// Builds BOTH the FullInfo and PartialObs views from the same in-memory game state
    /// — atomic from the client's perspective.
    /// Must be cheap — runs in the hot path of TurnStarted / TurnEnded.
    /// </summary>
    public static void RefreshObservation()
    {
        try
        {
            _cachedFullObs = BuildObservation(partial: false);
            _cachedPartialObs = BuildObservation(partial: true);
            _cachedActionMask = BuildActionMask();
            _lastSnapshotUtcMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        }
        catch (Exception ex)
        {
            // Do not let serialization failures cascade into the game's event chain.
            Log.Error($"{Tag} RefreshObservation failed: {ex.Message}");
            var err = "{\"phase\":\"error\",\"in_run\":false,\"error\":\"snapshot build failed\",\"message\":" +
                JsonEncodedString(ex.Message) + "}";
            _cachedFullObs = err;
            _cachedPartialObs = err;
            _cachedActionMask = "{\"phase\":\"error\",\"actions\":[]}";
        }
    }

    private static int ResolvePort()
    {
        var s = Environment.GetEnvironmentVariable("STS2GYM_PORT") ?? DefaultPort;
        if (!int.TryParse(s, out var p) || p < 1024 || p > 65535)
        {
            Log.Warn($"{Tag} invalid STS2GYM_PORT='{s}', falling back to {DefaultPort}");
            p = int.Parse(DefaultPort);
        }
        return p;
    }

    private static void WritePortLockfile(int port)
    {
        try
        {
            var path = Path.Combine(Path.GetTempPath(), "sts2_gym.port");
            File.WriteAllText(path, port.ToString());
            Log.Info($"{Tag} wrote port lockfile {path}");
        }
        catch (Exception ex)
        {
            Log.Warn($"{Tag} could not write port lockfile: {ex.Message}");
        }
    }

    private static string BuildObservation(bool partial)
    {
        if (!RunManager.Instance.IsInProgress)
        {
            return $"{{\"phase\":\"main_menu\",\"in_run\":false,\"snapshot_age_ms\":0,\"partial\":{(partial ? "true" : "false")}}}";
        }

        // Reuse game's source-generated JSON context via the public utility (dev plan §2.1 path a).
        var save = RunManager.Instance.ToSave(preFinishedRoom: null);
        var runJson = JsonSerializer.Serialize(save, JsonSerializationUtility.GetTypeInfo<SerializableRun>());

        var phase = ResolvePhase();
        var combatJson = CombatSnapshot.Build(partial);

        var sb = new StringBuilder(runJson.Length + 4096);
        sb.Append("{\"phase\":\"").Append(phase).Append("\"");
        sb.Append(",\"in_run\":true");
        sb.Append(",\"snapshot_age_ms\":0");
        sb.Append(",\"partial\":").Append(partial ? "true" : "false");
        if (combatJson != null)
        {
            sb.Append(",\"combat\":").Append(combatJson);
        }
        // Day-8.1: selector context overlays whatever phase we're in. The card-pick UI
        // can interrupt combat, reward, upgrade, transform — all routed through our
        // ICardSelector. selector_active=true takes precedence: the agent must clear
        // the selection before /step play_card / end_turn / next-phase actions become
        // legal again.
        AppendSelectorJson(sb);
        sb.Append(",\"run\":").Append(runJson).Append('}');
        return sb.ToString();
    }

    /// <summary>
    /// Day-8.1: emit "selector":{...} when our Sts2GymCardSelector is waiting for input.
    /// Field shape (kept stable for Python side):
    ///   {
    ///     "active": true,
    ///     "min_select": 1, "max_select": 1,
    ///     "options": [{"option_idx": 0, "card_id": "...", "cost": int, "is_upgraded": bool,
    ///                  "upgrade_level": int, "target_type": "..."}],
    ///     "accumulator": [int],
    ///     "can_confirm": bool, "can_skip": bool
    ///   }
    /// </summary>
    private static void AppendSelectorJson(StringBuilder sb)
    {
        var snap = Sts2GymMod.Selector?.Snapshot();
        if (snap == null)
        {
            sb.Append(",\"selector\":{\"active\":false}");
            return;
        }
        sb.Append(",\"selector\":{\"active\":true");
        sb.Append(",\"min_select\":").Append(snap.MinSelect);
        sb.Append(",\"max_select\":").Append(snap.MaxSelect);
        sb.Append(",\"options\":[");
        for (int i = 0; i < snap.Options.Count; i++)
        {
            if (i > 0) sb.Append(',');
            var c = snap.Options[i];
            sb.Append("{\"option_idx\":").Append(i);
            sb.Append(",\"card_id\":").Append(JsonEncodedString(c.Id.Entry));
            sb.Append(",\"cost\":").Append(c.EnergyCost.GetResolved());
            sb.Append(",\"is_upgraded\":").Append(c.IsUpgraded ? "true" : "false");
            sb.Append(",\"upgrade_level\":").Append(c.CurrentUpgradeLevel);
            sb.Append(",\"target_type\":").Append(JsonEncodedString(c.TargetType.ToString()));
            sb.Append('}');
        }
        sb.Append(']');
        sb.Append(",\"accumulator\":[").Append(string.Join(",", snap.Accumulator)).Append(']');
        sb.Append(",\"can_confirm\":").Append(snap.Accumulator.Count >= snap.MinSelect ? "true" : "false");
        sb.Append(",\"can_skip\":").Append(snap.MinSelect == 0 ? "true" : "false");
        sb.Append('}');
    }

    /// <summary>
    /// Day-4 12-phase resolver (dev plan §3.1). Order matters: screen overlays beat room
    /// type. Reads several Godot singletons, so this MUST be called on the game thread
    /// (event handlers honor this).
    /// </summary>
    private static string ResolvePhase()
    {
        if (!RunManager.Instance.IsInProgress) return "main_menu";
        if (RunManager.Instance.IsGameOver) return "game_over";

        // Screen overlay takes precedence — these are modal popups stacked over
        // the active room. Per AutoSlayer's _screenHandlers dictionary, these 12
        // screen types cover all P0 phases.
        var overlay = NOverlayStack.Instance?.Peek();
        if (overlay != null)
        {
            var t = overlay.GetType();
            if (t == typeof(NGameOverScreen)) return "game_over";
            if (t == typeof(NRewardsScreen)) return "reward";
            if (t == typeof(NCardRewardSelectionScreen)) return "reward";
            if (t == typeof(NDeckUpgradeSelectScreen)) return "upgrade";
            if (t == typeof(NDeckTransformSelectScreen)) return "transform";
            if (t == typeof(NDeckEnchantSelectScreen)) return "enchant";
            if (t == typeof(NDeckCardSelectScreen)) return "card_select";
            if (t == typeof(NSimpleCardSelectScreen)) return "card_select";
            if (t == typeof(NChooseACardSelectionScreen)) return "card_select";
            if (t == typeof(NChooseABundleSelectionScreen)) return "card_select";
            if (t == typeof(NChooseARelicSelection)) return "relic_select";
            if (t == typeof(NCrystalSphereScreen)) return "event";
        }

        if (CombatManager.Instance.IsInProgress) return "combat";

        // NMapScreen is a regular non-modal screen (not in overlay stack).
        if (NMapScreen.Instance?.IsOpen == true) return "map";

        var room = RunManager.Instance.DebugOnlyGetState()?.CurrentRoom;
        var roomType = room?.RoomType ?? RoomType.Unassigned;
        return roomType switch
        {
            RoomType.Monster or RoomType.Elite or RoomType.Boss => "combat_pending",
            RoomType.Event => "event",
            RoomType.Shop => "shop",
            RoomType.RestSite => "rest",
            RoomType.Treasure => "treasure",
            _ => "between_rooms",
        };
    }

    private static string JsonEncodedString(string? s)
    {
        if (s == null) return "null";
        // Conservative JSON string escape — good enough for IDs / encoder names; full
        // localized text goes through JsonSerializer (which we use for SerializableRun).
        var sb = new StringBuilder(s.Length + 2);
        sb.Append('"');
        foreach (var c in s)
        {
            if (c == '"') sb.Append("\\\"");
            else if (c == '\\') sb.Append("\\\\");
            else if (c == '\n') sb.Append("\\n");
            else if (c == '\r') sb.Append("\\r");
            else if (c == '\t') sb.Append("\\t");
            else if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
            else sb.Append(c);
        }
        sb.Append('"');
        return sb.ToString();
    }

    private static void AcceptLoop()
    {
        while (_running && _listener != null)
        {
            HttpListenerContext ctx;
            try
            {
                ctx = _listener.GetContext();
            }
            catch (HttpListenerException) when (!_running)
            {
                // Listener was stopped — clean exit.
                return;
            }
            catch (ObjectDisposedException)
            {
                return;
            }
            catch (Exception ex)
            {
                Log.Error($"{Tag} accept loop exception: {ex.Message}");
                Thread.Sleep(50);
                continue;
            }

            try
            {
                Handle(ctx);
            }
            catch (Exception ex)
            {
                Log.Error($"{Tag} handler exception: {ex}");
                try { ctx.Response.Abort(); } catch { /* ignore */ }
            }
        }
    }

    private static void Handle(HttpListenerContext ctx)
    {
        var path = ctx.Request.Url?.AbsolutePath ?? "/";
        var method = ctx.Request.HttpMethod;
        string body;
        int status;

        switch (path)
        {
            case "/health":
                body = $"{{\"status\":\"ok\",\"mod\":\"sts2gym\",\"version\":\"0.0.1\",\"protocol_version\":{ProtocolVersion},\"port\":{Port}}}";
                status = 200;
                break;

            case "/version":
                body = $"{{\"mod\":\"sts2gym\",\"version\":\"0.0.1\",\"protocol_version\":{ProtocolVersion}}}";
                status = 200;
                break;

            case "/observe":
                // ?partial=1 -> PartialObs view (dev plan §2.8: hides draw_pile contents, etc).
                // No query / partial=0 -> FullInfo view.
                var partialFlag = ctx.Request.QueryString["partial"];
                bool partial = partialFlag == "1" || partialFlag == "true";
                body = WithFreshAge(partial ? _cachedPartialObs : _cachedFullObs);
                status = 200;
                break;

            case "/action_mask":
                // Serve from cache (built atomically with /observe in RefreshObservation).
                // Live read would race with animation-queue: /observe sees post-event state,
                // /action_mask sees mid-animation state, agent picks action from a mask that
                // doesn't match its observation, /step lands in a different reality. That race
                // is exactly what Day-6.1 determinism test diverged on.
                body = _cachedActionMask;
                status = 200;
                break;

            case "/step":
                if (method != "POST")
                {
                    status = 405;
                    body = "{\"ok\":false,\"error\":\"/step requires POST\"}";
                    break;
                }
                (status, body) = HandleStep(ctx).GetAwaiter().GetResult();
                break;

            case "/reset":
                if (method != "POST")
                {
                    status = 405;
                    body = "{\"ok\":false,\"error\":\"/reset requires POST\"}";
                    break;
                }
                (status, body) = HandleReset(ctx).GetAwaiter().GetResult();
                break;

            default:
                body = $"{{\"error\":\"unknown endpoint\",\"path\":{JsonEncodedString(path)}}}";
                status = 404;
                break;
        }

        var bytes = Encoding.UTF8.GetBytes(body);
        ctx.Response.StatusCode = status;
        ctx.Response.ContentType = "application/json; charset=utf-8";
        ctx.Response.Headers["X-Sts2Gym-Protocol"] = ProtocolVersion.ToString();
        ctx.Response.Headers["X-Snapshot-Age-Ms"] = SnapshotAgeMs().ToString();
        ctx.Response.ContentLength64 = bytes.Length;
        ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
        ctx.Response.Close();
    }

    private static long SnapshotAgeMs()
    {
        if (_lastSnapshotUtcMs == 0) return -1;
        return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - _lastSnapshotUtcMs;
    }

    /// <summary>
    /// Patch the snapshot_age_ms field of the cached observation just before sending,
    /// so the client sees an accurate "how stale is this" value at response time.
    /// Cheap string replace — observation is only built once per game event.
    /// </summary>
    private static string WithFreshAge(string cached)
    {
        const string Sentinel = "\"snapshot_age_ms\":0";
        var idx = cached.IndexOf(Sentinel, StringComparison.Ordinal);
        if (idx < 0) return cached;
        var age = SnapshotAgeMs();
        return cached.Substring(0, idx) + "\"snapshot_age_ms\":" + age + cached.Substring(idx + Sentinel.Length);
    }

    // -------------------- /action_mask --------------------

    /// <summary>
    /// Build the legal action set for the current state. Day-5 minimal scope:
    /// combat phase only — play_card + end_turn. Day-6+ adds non-combat phase
    /// actions via the ICardSelector + 5-selector stack model (dev plan §2.3 / §3.4).
    ///
    /// Called from RefreshObservation (game thread, in event handler context) —
    /// MUST NOT be called from HTTP thread. HTTP handler serves _cachedActionMask
    /// instead. This keeps action_mask atomic with /observe (Day-6.2 fix —
    /// previously action_mask was a live read and raced with animation queue).
    /// </summary>
    private static string BuildActionMask()
    {
        var sb = new StringBuilder(1024);
        sb.Append('{');

        // ----- Day-8.1: selector takes precedence over combat -----
        // A pending selector can interrupt combat (Survivor's discard) OR fire
        // outside combat (reward screen, deck upgrade, etc.). Either way, until
        // we resolve it via /step select_*, play_card / end_turn are NOT legal —
        // the engine is blocked on our TCS waiting for the pick.
        var snap = Sts2GymMod.Selector?.Snapshot();
        if (snap != null)
        {
            sb.Append("\"phase\":\"card_select\"");
            sb.Append(",\"selector_active\":true");
            sb.Append(",\"min_select\":").Append(snap.MinSelect);
            sb.Append(",\"max_select\":").Append(snap.MaxSelect);
            sb.Append(",\"actions\":[");
            bool firstSel = true;
            for (int i = 0; i < snap.Options.Count; i++)
            {
                // Pick = available if not already in accumulator and accumulator has room.
                if (!snap.Accumulator.Contains(i) && snap.Accumulator.Count < snap.MaxSelect)
                {
                    if (!firstSel) sb.Append(',');
                    sb.Append("{\"type\":\"select_pick\",\"option_idx\":").Append(i);
                    sb.Append(",\"card_id\":").Append(JsonEncodedString(snap.Options[i].Id.Entry));
                    sb.Append("}");
                    firstSel = false;
                }
            }
            // Unpick = whatever's already in the accumulator.
            foreach (var pickedIdx in snap.Accumulator)
            {
                if (!firstSel) sb.Append(',');
                sb.Append("{\"type\":\"select_unpick\",\"option_idx\":").Append(pickedIdx).Append("}");
                firstSel = false;
            }
            // Confirm if we have enough picks.
            if (snap.Accumulator.Count >= snap.MinSelect)
            {
                if (!firstSel) sb.Append(',');
                sb.Append("{\"type\":\"select_confirm\"}");
                firstSel = false;
            }
            // Skip if min_select == 0.
            if (snap.MinSelect == 0)
            {
                if (!firstSel) sb.Append(',');
                sb.Append("{\"type\":\"select_skip\"}");
                firstSel = false;
            }
            sb.Append("]}");
            return sb.ToString();
        }

        if (!CombatManager.Instance.IsInProgress)
        {
            sb.Append("\"phase\":\"not_combat\",\"actions\":[]}");
            return sb.ToString();
        }

        var combat = CombatManager.Instance.DebugOnlyGetState();
        if (combat == null)
        {
            sb.Append("\"phase\":\"combat\",\"actions\":[],\"error\":\"combat state null\"}");
            return sb.ToString();
        }

        var inPlayPhase = CombatManager.Instance.IsPlayPhase;
        sb.Append("\"phase\":\"combat\"");
        sb.Append(",\"play_phase\":").Append(inPlayPhase ? "true" : "false");
        sb.Append(",\"round\":").Append(combat.RoundNumber);

        sb.Append(",\"actions\":[");
        if (!inPlayPhase)
        {
            // Not player's turn — no legal actions to take. Client should /observe
            // and re-poll /action_mask after TurnStarted fires.
            sb.Append("]}");
            return sb.ToString();
        }

        var player = combat.Players.FirstOrDefault();
        var pcs = player?.PlayerCombatState;
        if (pcs == null)
        {
            sb.Append("]}");
            return sb.ToString();
        }

        var hittableEnemies = combat.HittableEnemies.ToList();
        var alliesAlive = combat.Allies.Where(a => a.IsAlive).ToList();
        var playerCreature = combat.PlayerCreatures.FirstOrDefault(c => c.IsAlive);

        // ----- play_card actions -----
        bool firstAction = true;
        for (int i = 0; i < pcs.Hand.Cards.Count; i++)
        {
            var card = pcs.Hand.Cards[i];
            bool canPlay;
            try { canPlay = card.CanPlay(out _, out _); }
            catch { canPlay = false; }
            if (!canPlay) continue;

            // Enumerate legal targets for this card. Empty list = self / no-target /
            // AoE / random — caller passes null target.
            var legalTargets = LegalTargetsFor(card, hittableEnemies, alliesAlive, playerCreature);

            if (!firstAction) sb.Append(',');
            sb.Append("{\"type\":\"play_card\",\"card_idx\":").Append(i);
            sb.Append(",\"card_id\":").Append(JsonEncodedString(card.Id.Entry));
            sb.Append(",\"cost\":").Append(card.EnergyCost.GetResolved());
            sb.Append(",\"target_type\":\"").Append(card.TargetType).Append('"');
            sb.Append(",\"requires_target\":").Append(RequiresTarget(card.TargetType) ? "true" : "false");
            sb.Append(",\"legal_targets\":[");
            for (int t = 0; t < legalTargets.Count; t++)
            {
                if (t > 0) sb.Append(',');
                var tc = legalTargets[t];
                sb.Append("{\"combat_id\":").Append(tc.CombatId);
                sb.Append(",\"name\":").Append(JsonEncodedString(tc.Monster?.Id.Entry ?? tc.Player?.Character.Id.Entry));
                sb.Append("}");
            }
            sb.Append("]}");
            firstAction = false;
        }

        // ----- end_turn -----
        if (!firstAction) sb.Append(',');
        sb.Append("{\"type\":\"end_turn\"}");

        sb.Append("]}");
        return sb.ToString();
    }

    private static System.Collections.Generic.List<Creature> LegalTargetsFor(
        CardModel card,
        System.Collections.Generic.List<Creature> hittableEnemies,
        System.Collections.Generic.List<Creature> aliveAllies,
        Creature? playerCreature)
    {
        return card.TargetType switch
        {
            TargetType.AnyEnemy => hittableEnemies.Where(e => card.CanPlayTargeting(e)).ToList(),
            TargetType.AnyAlly => aliveAllies.Where(a => card.CanPlayTargeting(a)).ToList(),
            TargetType.AnyPlayer => aliveAllies.Where(a => a.IsPlayer && card.CanPlayTargeting(a)).ToList(),
            // Self / AllEnemies / RandomEnemy / AllAllies / TargetedNoCreature / Osty / None
            //   -> no caller-supplied target. We emit an empty list and the client
            //      knows not to pass target_combat_id.
            _ => new System.Collections.Generic.List<Creature>(),
        };
    }

    private static bool RequiresTarget(TargetType t)
    {
        return t == TargetType.AnyEnemy || t == TargetType.AnyAlly || t == TargetType.AnyPlayer;
    }

    // -------------------- /step --------------------

    private static async Task<(int, string)> HandleStep(HttpListenerContext ctx)
    {
        var (ok, cmd, errBody) = await ReadJsonBody(ctx);
        if (!ok) return (400, errBody!);
        return await StepRunner.ExecuteAsync(cmd);
    }

    private static async Task<(int, string)> HandleReset(HttpListenerContext ctx)
    {
        var (ok, cmd, errBody) = await ReadJsonBody(ctx);
        if (!ok) return (400, errBody!);
        // ScenarioInjector touches game-thread-only state (RunManager / EncounterModel),
        // so marshal via GameThread helper just like /step does.
        return await GameThread.RunOnMainAsync(() => ScenarioInjector.ApplyAsync(cmd));
    }

    private static async Task<(bool ok, JsonElement cmd, string? errBody)> ReadJsonBody(HttpListenerContext ctx)
    {
        string raw;
        using (var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8))
        {
            raw = await reader.ReadToEndAsync();
        }
        if (string.IsNullOrWhiteSpace(raw))
        {
            return (false, default, "{\"ok\":false,\"error\":\"empty POST body — JSON required\"}");
        }
        try
        {
            using var doc = JsonDocument.Parse(raw);
            return (true, doc.RootElement.Clone(), null);
        }
        catch (JsonException ex)
        {
            return (false, default, "{\"ok\":false,\"error\":\"invalid JSON\",\"message\":" + JsonEncodedString(ex.Message) + "}");
        }
    }
}
