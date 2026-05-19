using System;
using MegaCrit.Sts2.Core.Combat;
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

    static void Init()
    {
        try
        {
            Log.Info($"{LogTag} hello — ModInitializer.Init invoked");
            Log.Info($"{LogTag} stage = OneTimeInitialization.ExecuteVeryEarly (ModelDb / Godot not yet ready, only settings + ModManager ready)");

            // Subscribe to lifecycle events. These fire long after Init,
            // by which time the game world is fully constructed.
            RunManager.Instance.RunStarted += OnRunStarted;
            CombatManager.Instance.CombatSetUp += OnCombatSetUp;
            CombatManager.Instance.TurnStarted += OnTurnStarted;
            CombatManager.Instance.TurnEnded += OnTurnEnded;
            // PlayerActionsDisabledChanged fires AFTER the game's in-frame
            // "new turn" routine completes (energy reset + initial draw + buff
            // ticks). TurnStarted fires BEFORE that routine, so snapshots taken
            // there are stale (energy=0, hand=0 at start of new turn). Subscribing
            // to both gives us a fresh snapshot at the moment the player can act.
            CombatManager.Instance.PlayerActionsDisabledChanged += OnPlayerActionsDisabledChanged;

            Log.Info($"{LogTag} subscriptions: RunStarted, CombatSetUp, TurnStarted, TurnEnded, PlayerActionsDisabledChanged");

            // Day-3 P0 milestone: start the HTTP bridge so Python side can probe state.
            // HttpListener does NOT depend on Godot scene tree, safe to start in ExecuteVeryEarly.
            HttpBridge.Start();
        }
        catch (Exception ex)
        {
            Log.Error($"{LogTag} INIT FAILED: {ex}");
            throw;
        }
    }

    static void OnRunStarted(RunState run)
    {
        _runsObserved++;
        try
        {
            Log.Info($"{LogTag} RunStarted #{_runsObserved}: ascension={run.AscensionLevel} players={run.Players.Count} seed='{run.Rng.StringSeed}' acts={run.Acts.Count}");

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
            HttpBridge.RefreshObservation();
        }
        catch (Exception ex)
        {
            Log.Error($"{LogTag} OnCombatSetUp exception: {ex}");
        }
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
}
