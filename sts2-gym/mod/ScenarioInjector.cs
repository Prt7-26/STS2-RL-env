using System;
using System.Collections.Generic;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Rngs;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves.Runs;

namespace Sts2Gym;

/// <summary>
/// Day-6 P0: scenario injection (dev plan §2.2 Combat-level scope).
///
/// Day-6 Level-A scope (this file): two operations exposed via /reset:
///   - Jump to a named encounter (via RunManager.EnterRoomDebug)
///   - Restore RunRngSet counter state (so determinism tests can re-roll the
///     same encounter from the same RNG point)
///
/// What this DOES NOT do (deferred to Day-7 / Week-2):
///   - Start a fresh run with custom character / ascension / deck / hp / relics
///     from main menu (requires UI navigation à la AutoSlay PlayMainMenuAsync,
///     plus RunManager.Abandon coordination since SetUpNewSinglePlayer throws
///     when State != null)
///   - PlayerRngSet restore (rewards / shops / transformations RNG)
///   - Mid-combat injection (would require constructing CombatState directly
///     and calling CombatManager.SetUpCombat)
///
/// Caller invariants:
///   - RunManager.Instance.IsInProgress must be true (we don't drive UI to
///     start a new run yet — user enters a run manually first)
///   - All operations dispatch on the game main thread via GameThread helper
/// </summary>
internal static class ScenarioInjector
{
    private const string Tag = "[sts2gym/inject]";

    /// <summary>
    /// Entry point: parse a JSON scenario request and apply it.
    /// Body schema (Day-6 Level-A):
    ///   {
    ///     "encounter": "EXOSKELETONS_WEAK",       // optional — jump to this encounter
    ///     "rng_counters": {                        // optional — restore RunRngSet state
    ///       "seed": "MYSEED",                      // must match current run.rng.seed (no
    ///                                              // mid-run reseed; LoadFromSerializable
    ///                                              // throws on seed mismatch)
    ///       "counters": { "shuffle": 12, "combat_targets": 7, ... }
    ///     }
    ///   }
    /// </summary>
    public static async Task<(int status, string body)> ApplyAsync(JsonElement cmd)
    {
        if (!RunManager.Instance.IsInProgress)
        {
            return (409, "{\"ok\":false,\"error\":\"not in a run — start a run from the main menu first, then call /reset\"}");
        }

        // Defensive: read fields up-front so we can fail fast on bad input.
        string? encounterName = null;
        if (cmd.TryGetProperty("encounter", out var encProp) && encProp.ValueKind == JsonValueKind.String)
        {
            encounterName = encProp.GetString();
        }

        JsonElement rngBlock = default;
        var hasRng = cmd.TryGetProperty("rng_counters", out rngBlock) && rngBlock.ValueKind == JsonValueKind.Object;

        if (encounterName == null && !hasRng)
        {
            return (400, "{\"ok\":false,\"error\":\"empty scenario — provide at least 'encounter' or 'rng_counters'\"}");
        }

        var sb = new StringBuilder(256);
        sb.Append("{\"ok\":true");

        // ---------- 1) restore RunRngSet if requested (BEFORE encounter so the
        //              encounter's first-time monster generation uses the
        //              restored RNG state, dev plan §2.5)
        if (hasRng)
        {
            var rngError = TryRestoreRng(rngBlock);
            if (rngError != null) return (400, rngError);
            sb.Append(",\"rng_restored\":true");
        }

        // ---------- 2) jump to encounter
        if (encounterName != null)
        {
            try
            {
                var encId = new ModelId(ModelId.SlugifyCategory<EncounterModel>(), encounterName.ToUpperInvariant());
                EncounterModel encounter;
                try
                {
                    encounter = ModelDb.GetById<EncounterModel>(encId).ToMutable();
                }
                catch (Exception ex)
                {
                    return (400, "{\"ok\":false,\"error\":\"unknown encounter\",\"encounter\":\"" + encounterName +
                        "\",\"message\":" + JsonStr(ex.Message) + "}");
                }
                // Deliberately NOT calling encounter.DebugRandomizeRng() — that's the
                // wall-clock-seeded outlier dev plan §2.5 / task D wanted us to avoid.
                // With _rng null, EncounterModel.GenerateMonsters will derive the
                // encounter's RNG from RunState.Rng.Seed + TotalFloor + encId hash,
                // i.e. deterministically.

                Log.Info($"{Tag} EnterRoomDebug Monster encounter={encounterName}");
                var roomTask = RunManager.Instance.EnterRoomDebug(RoomType.Monster, MapPointType.Unassigned, encounter);

                // Wait for the room transition + initial combat setup to complete.
                // EnterRoomDebug returns when the new AbstractRoom is created but
                // animations and CombatSetUp event firing may still be in progress.
                await roomTask;
                if (RunManager.Instance.ActionQueueSet != null)
                {
                    try { await RunManager.Instance.ActionQueueSet.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(10)); }
                    catch (TimeoutException) { Log.Warn($"{Tag} ActionQueue did not drain after EnterRoomDebug"); }
                }

                sb.Append(",\"encounter\":").Append(JsonStr(encounterName));
                sb.Append(",\"phase_after\":\"").Append(CombatManager.Instance.IsInProgress ? "combat" : "transitioning").Append('"');
            }
            catch (Exception ex)
            {
                Log.Error($"{Tag} jump-to-encounter failed: {ex}");
                return (500, "{\"ok\":false,\"error\":\"jump_to_encounter failed\",\"message\":" + JsonStr(ex.Message) + "}");
            }
        }

        // Refresh observation cache so client's next /observe sees the new state.
        HttpBridge.RefreshObservation();

        sb.Append('}');
        return (200, sb.ToString());
    }

    /// <summary>
    /// Restore RunRngSet counters from a JSON block. Returns null on success,
    /// or a JSON error body on failure.
    /// </summary>
    private static string? TryRestoreRng(JsonElement rngBlock)
    {
        var runState = RunManager.Instance.DebugOnlyGetState();
        if (runState == null) return "{\"ok\":false,\"error\":\"runState null\"}";

        // Seed must match the current run's seed — RunRngSet.LoadFromSerializable
        // throws NotImplementedException on seed mismatch. This is by design:
        // RunRngSet doesn't support mid-run reseed.
        string? seed = null;
        if (rngBlock.TryGetProperty("seed", out var seedProp) && seedProp.ValueKind == JsonValueKind.String)
        {
            seed = seedProp.GetString();
        }
        if (seed == null) return "{\"ok\":false,\"error\":\"rng_counters.seed missing\"}";
        if (seed != runState.Rng.StringSeed)
        {
            return "{\"ok\":false,\"error\":\"seed mismatch — RunRngSet cannot be reseeded mid-run\"," +
                "\"current\":" + JsonStr(runState.Rng.StringSeed) + ",\"requested\":" + JsonStr(seed) + "}";
        }

        if (!rngBlock.TryGetProperty("counters", out var countersProp) || countersProp.ValueKind != JsonValueKind.Object)
        {
            return "{\"ok\":false,\"error\":\"rng_counters.counters object missing\"}";
        }

        var save = new SerializableRunRngSet { Seed = seed };
        foreach (var prop in countersProp.EnumerateObject())
        {
            // Property names come in snake_case (matching SerializableRunRngSet JSON output).
            // Convert to RunRngType enum value (PascalCase).
            var enumName = ToPascalCase(prop.Name);
            if (!Enum.TryParse<RunRngType>(enumName, ignoreCase: true, out var rngType))
            {
                return "{\"ok\":false,\"error\":\"unknown rng stream\",\"name\":" + JsonStr(prop.Name) + "}";
            }
            if (prop.Value.ValueKind != JsonValueKind.Number)
            {
                return "{\"ok\":false,\"error\":\"rng counter must be int\",\"name\":" + JsonStr(prop.Name) + "}";
            }
            save.Counters[rngType] = prop.Value.GetInt32();
        }

        try
        {
            runState.Rng.LoadFromSerializable(save);
            Log.Info($"{Tag} restored RunRngSet — {save.Counters.Count} streams");
            return null;
        }
        catch (Exception ex)
        {
            return "{\"ok\":false,\"error\":\"LoadFromSerializable threw\",\"message\":" + JsonStr(ex.Message) + "}";
        }
    }

    private static string ToPascalCase(string snake)
    {
        var sb = new StringBuilder(snake.Length);
        var upperNext = true;
        foreach (var c in snake)
        {
            if (c == '_') { upperNext = true; continue; }
            sb.Append(upperNext ? char.ToUpperInvariant(c) : c);
            upperNext = false;
        }
        return sb.ToString();
    }

    private static string JsonStr(string? s)
    {
        if (s == null) return "null";
        var sb = new StringBuilder(s.Length + 2);
        sb.Append('"');
        foreach (var c in s)
        {
            if (c == '"') sb.Append("\\\"");
            else if (c == '\\') sb.Append("\\\\");
            else if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
            else sb.Append(c);
        }
        sb.Append('"');
        return sb.ToString();
    }
}
