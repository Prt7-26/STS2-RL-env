using System;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Multiplayer;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;

namespace Sts2Gym;

/// <summary>
/// Day-13: GET /save_run + POST /restore_run.
///
/// Backed by the same RunManager.ToSave / RunState.FromSerializable + LoadRun
/// pair that NMainMenu.OnContinueButtonPressedAsync uses for "Continue" — that
/// is, this is the game's own save/load path with the UI fade-in/out stripped.
///
/// Scope (per dev plan §2.1):
///   • Between-rooms state is fully captured by SerializableRun (deck, HP, gold,
///     potions, relics, RNG state, map, modifiers, visited rooms — 18+ fields).
///     Save/restore here is bit-exact for any decision point at room boundaries.
///   • Mid-combat state is NOT captured (game's own save system also doesn't —
///     multiplayer sync relies on deterministic replay, not state checkpoints).
///     /save_run returns 409 if the player is currently in a combat round —
///     restoration would land at the start of the room, not the current turn.
///
/// Wire protocol:
///
///     GET  /save_run
///     →    200 { "ok": true, "save": { ...SerializableRun JSON... } }
///          409 { "ok": false, "error": "no run in progress" }
///          409 { "ok": false, "error": "cannot save mid-combat" }
///
///     POST /restore_run
///          { "save": { ...SerializableRun JSON... } }
///     →    200 { "ok": true, "character": "Ironclad", "ascension": 0,
///                "current_room": "Map" }
///          400 / 500 with diagnostic detail on parse / load failures
/// </summary>
internal static class SaveRestore
{
    private const string Tag = "[sts2gym/save_restore]";

    // -------------------------------- /save_run -------------------------------

    public static (int status, string body) HandleSave()
    {
        if (!RunManager.Instance.IsInProgress)
            return (409, "{\"ok\":false,\"error\":\"no run in progress\"}");

        // Reject mid-combat: SerializableRun doesn't capture CombatState (round,
        // hand, draw/discard piles, current enemies' HP/intent). Restoring would
        // teleport the player back to the start of the room, silently dropping
        // the in-progress combat — surface that as an error rather than corrupt
        // the trajectory.
        try
        {
            if (MegaCrit.Sts2.Core.Combat.CombatManager.Instance?.IsInProgress == true)
            {
                return (409, "{\"ok\":false,\"error\":\"cannot save mid-combat\",\"hint\":\"SerializableRun does not capture CombatState; save at room boundaries (map/event/reward/shop/rest)\"}");
            }
        }
        catch { /* CombatManager singleton may not be up; treat as not-in-combat */ }

        try
        {
            var save = RunManager.Instance.ToSave(preFinishedRoom: null);
            var saveJson = JsonSerializer.Serialize(save, JsonSerializationUtility.GetTypeInfo<SerializableRun>());
            var meta = $"\"schema_version\":{save.SchemaVersion},\"ascension\":{save.Ascension}," +
                       $"\"current_act_index\":{save.CurrentActIndex}," +
                       $"\"rng_streams\":{save.SerializableRng.Counters.Count}," +
                       $"\"deck_size\":{save.Players[0].Deck.Count}," +
                       $"\"hp\":{save.Players[0].CurrentHp}";
            return (200, $"{{\"ok\":true,{meta},\"save\":{saveJson}}}");
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} ToSave threw: {ex}");
            return (500, $"{{\"ok\":false,\"error\":\"ToSave threw\",\"message\":{JsonStr(ex.Message)},\"stack\":{JsonStr(ex.StackTrace ?? "")}}}");
        }
    }

    // ------------------------------ /restore_run ------------------------------

    public static async Task<(int status, string body)> HandleRestoreAsync(HttpListenerContext ctx)
    {
        string raw;
        using (var sr = new System.IO.StreamReader(ctx.Request.InputStream, Encoding.UTF8))
            raw = await sr.ReadToEndAsync();

        JsonElement saveElement;
        try
        {
            var payload = JsonDocument.Parse(raw).RootElement;
            if (!payload.TryGetProperty("save", out saveElement))
                return (400, "{\"ok\":false,\"error\":\"missing 'save' field\"}");
        }
        catch (Exception ex)
        {
            return (400, $"{{\"ok\":false,\"error\":\"invalid JSON\",\"message\":{JsonStr(ex.Message)}}}");
        }

        SerializableRun save;
        try
        {
            // saveElement is already a parsed JsonElement; re-serialize to bytes
            // so we can deserialize through JsonSerializationUtility's TypeInfo
            // (which handles game-specific converters).
            var saveBytes = Encoding.UTF8.GetBytes(saveElement.GetRawText());
            save = JsonSerializer.Deserialize<SerializableRun>(
                saveBytes, JsonSerializationUtility.GetTypeInfo<SerializableRun>())!;
            if (save == null)
                return (400, "{\"ok\":false,\"error\":\"save deserialized to null\"}");
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} deserialize threw: {ex}");
            return (400, $"{{\"ok\":false,\"error\":\"deserialize SerializableRun threw\",\"message\":{JsonStr(ex.Message)}}}");
        }

        return await GameThread.RunOnMainAsync(async () => await RestoreOnMainThread(save));
    }

    private static async Task<(int status, string body)> RestoreOnMainThread(SerializableRun save)
    {
        try
        {
            // Match NMainMenu.OnContinueButtonPressedAsync ([NMainMenu.cs:511-533])
            // minus the visual fade-out/in transitions.

            if (RunManager.Instance.IsInProgress)
            {
                Log.Info($"{Tag} run in progress; CleanUp(graceful: false) before restore");
                RunManager.Instance.CleanUp(graceful: false);
            }

            var runState = RunState.FromSerializable(save);
            RunManager.Instance.SetUpSavedSinglePlayer(runState, save);

            // NMainMenu also calls ReactionContainer.InitializeNetworking with a
            // fresh NetSingleplayerGameService — SetUpSavedSinglePlayer already
            // installed one inside InitializeShared, but ReactionContainer is a
            // separate subscriber that needs its own wiring. Replicate that.
            try
            {
                NGame.Instance.ReactionContainer.InitializeNetworking(new NetSingleplayerGameService());
            }
            catch (Exception ex)
            {
                Log.Warn($"{Tag} ReactionContainer.InitializeNetworking warning: {ex.Message}");
            }

            await NGame.Instance.LoadRun(runState, save.PreFinishedRoom);

            // Brief settle so NRun + Act preload finish before we report state.
            await FastDelay.Of(200);
            HttpBridge.RefreshObservation();

            var curRoom = runState.CurrentRoom?.RoomType.ToString() ?? "unknown";
            var player = runState.Players[0];
            return (200, $"{{\"ok\":true,\"character\":{JsonStr(player.Character?.Id.Entry ?? "?")},\"ascension\":{runState.AscensionLevel},\"current_room\":{JsonStr(curRoom)},\"current_act_index\":{runState.CurrentActIndex},\"deck_size\":{player.Deck.Cards.Count},\"hp\":{player.Creature.CurrentHp},\"max_hp\":{player.Creature.MaxHp}}}");
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} restore threw: {ex}");
            return (500, $"{{\"ok\":false,\"error\":\"restore_run threw\",\"message\":{JsonStr(ex.Message)},\"type\":{JsonStr(ex.GetType().FullName)},\"stack\":{JsonStr(ex.StackTrace ?? "")}}}");
        }
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
            else if (c == '\n') sb.Append("\\n");
            else if (c == '\r') sb.Append("\\r");
            else if (c == '\t') sb.Append("\\t");
            else if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
            else sb.Append(c);
        }
        sb.Append('"');
        return sb.ToString();
    }
}
