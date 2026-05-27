using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Unlocks;

namespace Sts2Gym;

/// <summary>
/// Day-9.2: drive a fresh single-player run directly via RunManager.SetUpNewSinglePlayer.
///
/// The game's own main-menu UI path (NCharacterSelectScreen → SetUpNewSinglePlayer)
/// is what we copy here, minus the UI driving. AutoSlay uses the slower UI-click
/// path (PlayMainMenuAsync), but for our purposes the direct API is faster + more
/// deterministic — no animation polling, no race conditions.
///
/// Wire protocol: POST /start_run
///   {
///     "character": "IRONCLAD" | "SILENT" | "DEFECT" | "NECROBINDER" | "REGENT",
///     "ascension": 0..10,
///     "seed":     "MYSEED"     (optional, defaults to a random run-shaped seed)
///   }
/// </summary>
internal static class RunStarter
{
    private const string Tag = "[sts2gym/start_run]";

    public static async Task<(int status, string body)> HandleStartRunAsync(HttpListenerContext ctx)
    {
        string raw;
        using (var sr = new System.IO.StreamReader(ctx.Request.InputStream, Encoding.UTF8))
        {
            raw = await sr.ReadToEndAsync();
        }

        JsonElement payload;
        try { payload = JsonDocument.Parse(raw).RootElement; }
        catch (Exception ex) { return (400, $"{{\"ok\":false,\"error\":\"invalid JSON\",\"message\":{JsonStr(ex.Message)}}}"); }

        if (!payload.TryGetProperty("character", out var charProp) || charProp.ValueKind != JsonValueKind.String)
            return (400, "{\"ok\":false,\"error\":\"missing 'character' field\"}");

        var charName = charProp.GetString()!.ToUpperInvariant();
        int ascension = 0;
        if (payload.TryGetProperty("ascension", out var ascProp) && ascProp.ValueKind == JsonValueKind.Number)
            ascension = ascProp.GetInt32();
        if (ascension < 0 || ascension > 10)
            return (400, $"{{\"ok\":false,\"error\":\"ascension must be 0..10, got {ascension}\"}}");

        string? seed = null;
        if (payload.TryGetProperty("seed", out var seedProp) && seedProp.ValueKind == JsonValueKind.String)
            seed = seedProp.GetString();
        // Use a deterministic-but-fresh seed if caller didn't supply one. STS2's
        // SeedHelper would also work; this is good enough for our purposes.
        seed ??= $"GYM{DateTime.UtcNow.Ticks}";

        // Day-9.2: this call ultimately touches Godot scene-tree state (the game
        // constructs rooms/Acts/maps which subscribe to scene events). Marshal to
        // the main thread, just like /step.
        return await GameThread.RunOnMainAsync(async () => await StartOnMainThread(charName, ascension, seed));
    }

    private static async Task<(int status, string body)> StartOnMainThread(string charName, int ascension, string seed)
    {
        if (RunManager.Instance.IsInProgress)
            return (409, "{\"ok\":false,\"error\":\"a run is already in progress — call CleanUp / abandon first\"}");

        CharacterModel? character;
        try
        {
            character = ModelDb.AllCharacters.FirstOrDefault(c =>
                string.Equals(c.Id.Entry, charName, StringComparison.OrdinalIgnoreCase));
        }
        catch (Exception ex)
        {
            return (500, $"{{\"ok\":false,\"error\":\"character lookup threw\",\"message\":{JsonStr(ex.Message)}}}");
        }

        if (character == null)
        {
            var known = string.Join(",", ModelDb.AllCharacters.Select(c => $"\"{c.Id.Entry}\""));
            return (400, $"{{\"ok\":false,\"error\":\"unknown character\",\"got\":{JsonStr(charName)},\"known\":[{known}]}}");
        }

        try
        {
            // Day-9.2.3: use NGame.Instance.StartNewSingleplayerRun — the canonical
            // public API the game itself doesn't even use directly from UI (the UI
            // duplicates the code in NCharacterSelectScreen, but this entrypoint is
            // public and does ALL the steps: RunState creation, SetUpNewSinglePlayer,
            // PreloadManager.LoadRunAssets (populates AssetSets.RunSet),
            // PreloadManager.LoadActAssets (populates AssetSets.Act), FinalizeStartingRelics,
            // Launch, SetCurrentScene(NRun.Create()), EnterAct(0).
            //
            // Our previous attempt called SetUpNewSinglePlayer + SetCurrentScene only,
            // missing PreloadManager + Launch + EnterAct. The asset preload gap was
            // what NRE'd inside EnterRoomDebug → CombatRoom.StartCombat →
            // PreloadManager.LoadAssetSets (SelectMany over null AssetSets.RunSet/.Act).
            var acts = ActModel.GetDefaultList();
            var modifiers = Array.Empty<ModifierModel>();

            var runState = await NGame.Instance.StartNewSingleplayerRun(
                character,
                shouldSave: false,
                acts,
                modifiers,
                seed,
                GameMode.Standard,
                ascension);

            Log.Info($"{Tag} StartNewSingleplayerRun done: character={character.Id.Entry} ascension={ascension} seed={seed}");

            // Brief settle for NRun + Act 0 transition.
            await Task.Delay(200);
            HttpBridge.RefreshObservation();

            var state = RunManager.Instance.DebugOnlyGetState();
            var curRoom = state?.CurrentRoom?.RoomType.ToString() ?? "unknown";
            return (200, $"{{\"ok\":true,\"character\":{JsonStr(character.Id.Entry)},\"ascension\":{ascension}," +
                $"\"seed\":{JsonStr(seed)},\"current_room\":{JsonStr(curRoom)}}}");
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} start failed: {ex}");
            return (500, $"{{\"ok\":false,\"error\":\"start_run threw\",\"message\":{JsonStr(ex.Message)}," +
                $"\"type\":{JsonStr(ex.GetType().FullName)},\"stack\":{JsonStr(ex.StackTrace ?? "")}}}");
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
