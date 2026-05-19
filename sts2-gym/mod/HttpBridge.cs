using System;
using System.IO;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Threading;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Logging;
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

    // We cache two views built atomically from the same point-in-time game state.
    // This avoids any cross-thread game-state read from the HTTP listener thread.
    private const string EmptyObservation =
        "{\"phase\":\"main_menu\",\"in_run\":false,\"snapshot_age_ms\":-1,\"reason\":\"no snapshot yet\"}";
    private static volatile string _cachedFullObs = EmptyObservation;
    private static volatile string _cachedPartialObs = EmptyObservation;
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
        sb.Append(",\"run\":").Append(runJson).Append('}');
        return sb.ToString();
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
}
