using System;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Settings;

namespace Sts2Gym;

[ModInitializer(nameof(Init))]
public static class Sts2GymMod
{
    public const string ModId = "sts2gym";
    public const string LogTag = "[sts2gym]";

    private static int _runsObserved;
    private static int _combatsObserved;
    private static int _turnsObserved;

    /// <summary>
    /// Day-8.1: our ICardSelector implementation. Day-9.1: installed on demand
    /// via /selector/enable so manual play isn't intercepted by default. The
    /// agent layer (gym.Env / random_agent) enables on session start, disables
    /// on close. <see cref="Sts2GymCardSelector"/>.
    /// </summary>
    public static Sts2GymCardSelector Selector { get; private set; } = new Sts2GymCardSelector();
    private static IDisposable? _selectorScope;
    private static bool _selectorEnabled;

    /// <summary>Whether our selector is currently pushed onto CardSelectCmd's stack.</summary>
    public static bool SelectorEnabled => _selectorEnabled;

    /// <summary>
    /// Day-9.1: push our ICardSelector onto the global stack. Idempotent — safe
    /// to call multiple times. Once enabled, also re-pushed on every RunStarted
    /// because RunManager.CleanUp wipes the stack between runs.
    /// </summary>
    public static void EnableSelector()
    {
        if (_selectorEnabled && _selectorScope != null) return;
        _selectorScope?.Dispose();
        _selectorScope = CardSelectCmd.PushSelector(Selector);
        _selectorEnabled = true;
        Log.Info($"{LogTag} ICardSelector ENABLED");
    }

    /// <summary>
    /// Day-9.1: pop our selector + force-resolve any pending request with min
    /// defaults so the engine's awaiting continuation doesn't deadlock.
    /// </summary>
    public static void DisableSelector()
    {
        if (!_selectorEnabled) return;
        _selectorEnabled = false;
        Selector.ForceResolveWithDefault();
        _selectorScope?.Dispose();
        _selectorScope = null;
        Log.Info($"{LogTag} ICardSelector DISABLED");
    }

    static void Init()
    {
        try
        {
            Log.Info($"{LogTag} hello — ModInitializer.Init invoked");
            Log.Info($"{LogTag} stage = OneTimeInitialization.ExecuteVeryEarly (ModelDb / Godot not yet ready, only settings + ModManager ready)");

            // Subscribe to lifecycle events. These fire long after Init,
            // by which time the game world is fully constructed.
            RunManager.Instance.RunStarted += OnRunStarted;
            RunManager.Instance.RoomEntered += OnRoomEntered;
            RunManager.Instance.RoomExited += OnRoomExited;
            CombatManager.Instance.CombatSetUp += OnCombatSetUp;
            CombatManager.Instance.CombatEnded += OnCombatEnded;
            CombatManager.Instance.CombatWon += OnCombatWon;
            CombatManager.Instance.TurnStarted += OnTurnStarted;
            CombatManager.Instance.TurnEnded += OnTurnEnded;
            // PlayerActionsDisabledChanged fires AFTER the game's in-frame
            // "new turn" routine completes (energy reset + initial draw + buff
            // ticks). TurnStarted fires BEFORE that routine, so snapshots taken
            // there are stale (energy=0, hand=0 at start of new turn). Subscribing
            // to both gives us a fresh snapshot at the moment the player can act.
            CombatManager.Instance.PlayerActionsDisabledChanged += OnPlayerActionsDisabledChanged;

            Log.Info($"{LogTag} subscriptions: RunStarted, RoomEntered, RoomExited, CombatSetUp, CombatEnded, CombatWon, TurnStarted, TurnEnded, PlayerActionsDisabledChanged");

            // Day-3 P0 milestone: start the HTTP bridge so Python side can probe state.
            // HttpListener does NOT depend on Godot scene tree, safe to start in ExecuteVeryEarly.
            HttpBridge.Start();
            // Day-9.1: selector is NOT auto-installed any more. Manual play stays
            // intact unless an agent explicitly POSTs /selector/enable.
        }
        catch (Exception ex)
        {
            Log.Error($"{LogTag} INIT FAILED: {ex}");
            throw;
        }
    }

    private static bool _overlayStackSubscribed;

    static void OnRunStarted(RunState run)
    {
        _runsObserved++;
        try
        {
            Log.Info($"{LogTag} RunStarted #{_runsObserved}: ascension={run.AscensionLevel} players={run.Players.Count} seed='{run.Rng.StringSeed}' acts={run.Acts.Count}");

            // Day-10.J: try-subscribe NOverlayStack.Changed here (RunStarted fires
            // BEFORE NRun._Ready in StartNewSingleplayerRun's chain — so the
            // singleton is often still null at this point). Retry in CombatSetUp
            // and other event hooks; the one-shot _overlayStackSubscribed flag
            // makes the work cheap on subsequent calls.
            TryEnsureOverlayStackSubscribed();

            // Day-9.1: only re-push if user explicitly enabled it (typically by an
            // agent session via /selector/enable). Manual-play runs leave the stack
            // untouched. RunManager.CleanUp clears CardSelectCmd._selectorStack
            // between runs so re-push is needed even though we did one earlier.
            if (_selectorEnabled)
            {
                _selectorScope?.Dispose();
                _selectorScope = CardSelectCmd.PushSelector(Selector);
                Selector.ForceResolveWithDefault();
                Log.Info($"{LogTag} ICardSelector re-pushed for run #{_runsObserved}");
            }

            // FastMode toggle. Day-1 实测发现 Instant 触发 NCreature.AnimDie 内 Node.MoveChild(null) 报 ERROR,
            // 这正是 dev plan §2.4 / 任务 C 标注的 AutoSlayer 也避开 Instant 的 corner case 之一。
            // 暂时降到 Fast (animation 仍快 ~2x), P1 milestone 再考虑用 Harmony 修 AnimDie 的 null 引用以重新启用 Instant.
            var prevFast = SaveManager.Instance.PrefsSave.FastMode;
            SaveManager.Instance.PrefsSave.FastMode = FastModeType.Fast;
            Log.Info($"{LogTag} FastMode: {prevFast} -> Fast (Instant deferred — triggers AnimDie null ref)");

            // dev plan §2.1 path (a): SerializableRun reuse for between-rooms state.
            // This call should be near-free; confirm it doesn't blow up at run-start.
            var save = RunManager.Instance.ToSave(preFinishedRoom: null);
            Log.Info($"{LogTag} SerializableRun snapshot OK: schema={save.SchemaVersion} ascension={save.Ascension} game_mode={save.GameMode} rng_streams={save.SerializableRng.Counters.Count} players={save.Players.Count}");

            HttpBridge.RefreshObservation();
        }
        catch (Exception ex)
        {
            Log.Error($"{LogTag} OnRunStarted exception: {ex}");
        }
    }

    static void OnCombatSetUp(CombatState s)
    {
        _combatsObserved++;
        try
        {
            Log.Info($"{LogTag} CombatSetUp #{_combatsObserved}: encounter={s.Encounter?.Id.Entry ?? "<none>"} enemies={s.Enemies.Count} round={s.RoundNumber}");
            // Day-10.J: ensure NOverlayStack subscription. By CombatSetUp time,
            // NRun is definitely up — its children (including NOverlayStack)
            // are constructed.
            TryEnsureOverlayStackSubscribed();
            HttpBridge.RefreshObservation();
        }
        catch (Exception ex)
        {
            Log.Error($"{LogTag} OnCombatSetUp exception: {ex}");
        }
    }

    /// <summary>
    /// Day-10.J: idempotent lazy NOverlayStack.Changed subscription. Called from
    /// multiple event entry points so we don't miss it if RunStarted fired before
    /// NOverlayStack.Instance was up.
    /// </summary>
    private static void TryEnsureOverlayStackSubscribed()
    {
        if (_overlayStackSubscribed) return;
        var stack = MegaCrit.Sts2.Core.Nodes.Screens.Overlays.NOverlayStack.Instance;
        if (stack == null) return;
        stack.Changed += OnOverlayStackChanged;
        _overlayStackSubscribed = true;
        Log.Info($"{LogTag} subscribed to NOverlayStack.Changed");
    }

    static void OnTurnStarted(CombatState s)
    {
        _turnsObserved++;
        try
        {
            Log.Info($"{LogTag} TurnStarted #{_turnsObserved}: round={s.RoundNumber} side={s.CurrentSide} playPhase={CombatManager.Instance.IsPlayPhase}");
            HttpBridge.RefreshObservation();
        }
        catch (Exception ex)
        {
            Log.Error($"{LogTag} OnTurnStarted exception: {ex}");
        }
    }

    static void OnTurnEnded(CombatState s)
    {
        try
        {
            Log.Info($"{LogTag} TurnEnded: round={s.RoundNumber} side={s.CurrentSide}");
            HttpBridge.RefreshObservation();
        }
        catch (Exception ex)
        {
            Log.Error($"{LogTag} OnTurnEnded exception: {ex}");
        }
    }

    static void OnPlayerActionsDisabledChanged(CombatState s)
    {
        try
        {
            // Refresh whenever the input-lock toggles — this is the canonical
            // "player can act / can't act" signal, fires AFTER the turn-start
            // routine (energy reset, hand draw, buff ticks) so snapshots taken
            // here reflect what the player actually sees.
            HttpBridge.RefreshObservation();
        }
        catch (Exception ex)
        {
            Log.Error($"{LogTag} OnPlayerActionsDisabledChanged exception: {ex}");
        }
    }

    // ---- Day-10.H: bridge-cache refresh on additional state transitions ----
    //
    // Without these, the agent gets stuck in 'combat_pending' after a victory:
    // CombatManager.IsInProgress flips false (no TurnEnded fires for the final
    // round), the NRewardsScreen pushes a frame or two later, but no subscribed
    // event fires in between → cached /observe stays stale forever.
    //
    // Same pattern for room transitions (combat→map) and overlay pushes
    // (reward sub-screens, relic-select, game-over).

    static void OnCombatEnded(MegaCrit.Sts2.Core.Rooms.CombatRoom room)
    {
        try
        {
            Log.Info($"{LogTag} CombatEnded fired (room={room?.GetType().Name})");
            TryEnsureOverlayStackSubscribed();
            HttpBridge.RefreshObservation();
            // The reward screen pushes a frame or two LATER. Schedule a delayed
            // re-refresh so the cache reflects the new overlay state even if
            // NOverlayStack.Changed isn't subscribed (race during run startup).
            _ = ScheduleDelayedRefreshAsync(700);
        }
        catch (Exception ex) { Log.Error($"{LogTag} OnCombatEnded: {ex}"); }
    }

    static void OnCombatWon(MegaCrit.Sts2.Core.Rooms.CombatRoom room)
    {
        try
        {
            Log.Info($"{LogTag} CombatWon fired");
            TryEnsureOverlayStackSubscribed();
            HttpBridge.RefreshObservation();
            _ = ScheduleDelayedRefreshAsync(700);
        }
        catch (Exception ex) { Log.Error($"{LogTag} OnCombatWon: {ex}"); }
    }

    static void OnRoomEntered()
    {
        try
        {
            TryEnsureOverlayStackSubscribed();
            HttpBridge.RefreshObservation();
        }
        catch (Exception ex) { Log.Error($"{LogTag} OnRoomEntered: {ex}"); }
    }

    static void OnRoomExited()
    {
        try
        {
            HttpBridge.RefreshObservation();
            _ = ScheduleDelayedRefreshAsync(500);
        }
        catch (Exception ex) { Log.Error($"{LogTag} OnRoomExited: {ex}"); }
    }

    static void OnOverlayStackChanged()
    {
        try { HttpBridge.RefreshObservation(); }
        catch (Exception ex) { Log.Error($"{LogTag} OnOverlayStackChanged: {ex}"); }
    }

    /// <summary>
    /// Day-10.J: post-event delayed refresh. Some state transitions (notably
    /// CombatEnded → NRewardsScreen push) complete one or two frames after the
    /// event we subscribe to. A follow-up refresh closes that gap.
    /// </summary>
    private static async System.Threading.Tasks.Task ScheduleDelayedRefreshAsync(int delayMs)
    {
        try
        {
            await System.Threading.Tasks.Task.Delay(delayMs);
            HttpBridge.RefreshObservation();
        }
        catch (Exception ex) { Log.Warn($"{LogTag} ScheduleDelayedRefreshAsync: {ex.Message}"); }
    }
}
