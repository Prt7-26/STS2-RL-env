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

            Log.Info($"{LogTag} subscriptions: RunStarted, CombatSetUp, TurnStarted, TurnEnded");
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

            // FastMode toggle. dev plan §2.4 / §11 P1 milestone: verify Instant is bit-exact equivalent
            // to Normal for trajectory determinism. We flip the switch here; Day 2 实测 compares
            // CombatHistory event sequences across modes.
            var prevFast = SaveManager.Instance.PrefsSave.FastMode;
            SaveManager.Instance.PrefsSave.FastMode = FastModeType.Instant;
            Log.Info($"{LogTag} FastMode: {prevFast} -> Instant");

            // dev plan §2.1 path (a): SerializableRun reuse for between-rooms state.
            // This call should be near-free; confirm it doesn't blow up at run-start.
            var save = RunManager.Instance.ToSave(preFinishedRoom: null);
            Log.Info($"{LogTag} SerializableRun snapshot OK: schema={save.SchemaVersion} ascension={save.Ascension} game_mode={save.GameMode} rng_streams={save.SerializableRng.Counters.Count} players={save.Players.Count}");
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
        }
        catch (Exception ex)
        {
            Log.Error($"{LogTag} OnTurnEnded exception: {ex}");
        }
    }
}
