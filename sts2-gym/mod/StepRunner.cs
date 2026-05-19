using System;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2Gym;

/// <summary>
/// Day-5 P0: write-path. Dispatches a single structured action into the game
/// and waits for game state to stabilize before returning, mirroring dev plan
/// §2.3 ActionDispatcher synchronization semantics.
///
/// Supported actions (Day-5 minimal):
///   - play_card  {card_idx: int, target_combat_id: int?}
///   - end_turn   {}
///
/// Day-6+ will add: use_potion, discard_potion, choose_map_node, event/shop
/// actions (via the ICardSelector + 5 mod-introduced selector stack model
/// recorded in dev plan §2.3 / §3.4).
///
/// Threading: HttpListener runs request handlers on background threads.
/// AutoSlayer (game's own automation framework) successfully calls these
/// *Cmd APIs off-main-thread via TaskHelper.RunSafely, so we follow the
/// same pattern — kick off the async work and await its completion on the
/// HTTP thread. If we hit deadlocks / scene-tree mutation races, we'll
/// upgrade to Godot.Callable.CallDeferred marshaling.
/// </summary>
internal static class StepRunner
{
    private const string Tag = "[sts2gym/step]";

    /// <summary>
    /// Serializes step requests. Only one in flight at a time — the
    /// SemaphoreSlim ensures the game gets stable state between actions
    /// and our HTTP /step calls don't race each other.
    /// </summary>
    private static readonly SemaphoreSlim _stepLock = new SemaphoreSlim(1, 1);

    /// <summary>
    /// Execute a structured step command. Returns the JSON response body.
    /// </summary>
    public static async Task<(int status, string body)> ExecuteAsync(JsonElement cmd)
    {
        if (!await _stepLock.WaitAsync(TimeSpan.FromSeconds(15)))
        {
            return (503, "{\"ok\":false,\"error\":\"another step is in flight (timed out waiting for lock)\"}");
        }

        try
        {
            // Day-5.1: marshal DispatchAsync onto Godot main thread before
            // touching CardCmd / PlayerCmd. Without this, DISMANTLE and a few
            // other cards trigger
            //     "Changing the name to nodes inside the SceneTree is only
            //      allowed from the main thread."
            // which corrupts scene tree state and can wedge the combat phase
            // transition (TurnStarted may stop firing).
            return await GameThread.RunOnMainAsync(() => DispatchAsync(cmd));
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} step exception: {ex}");
            return (500, "{\"ok\":false,\"error\":\"exception\",\"message\":" + JsonStr(ex.Message) + "}");
        }
        finally
        {
            _stepLock.Release();
        }
    }

    // -------------------------------------------------------------------

    private static async Task<(int status, string body)> DispatchAsync(JsonElement cmd)
    {
        if (!cmd.TryGetProperty("type", out var typeProp) || typeProp.ValueKind != JsonValueKind.String)
        {
            return (400, "{\"ok\":false,\"error\":\"missing or non-string 'type' field\"}");
        }

        var type = typeProp.GetString();
        return type switch
        {
            "play_card" => await PlayCardAsync(cmd),
            "end_turn" => EndTurn(),
            "noop" => (200, "{\"ok\":true,\"action\":\"noop\"}"),
            _ => (400, "{\"ok\":false,\"error\":\"unknown action type\",\"type\":" + JsonStr(type ?? "") + "}"),
        };
    }

    // -------------------------- play_card --------------------------

    private static async Task<(int status, string body)> PlayCardAsync(JsonElement cmd)
    {
        // ----- preconditions -----
        if (!CombatManager.Instance.IsInProgress)
            return (409, "{\"ok\":false,\"error\":\"not in combat\"}");
        if (!CombatManager.Instance.IsPlayPhase)
            return (409, "{\"ok\":false,\"error\":\"not in play phase (Enemy turn or animations)\"}");

        var combat = CombatManager.Instance.DebugOnlyGetState();
        if (combat == null) return (500, "{\"ok\":false,\"error\":\"combat state is null\"}");

        // For now: only single-player (NetId == 1 conventionally for SP). Pick the first player.
        var player = combat.Players.FirstOrDefault();
        if (player == null) return (500, "{\"ok\":false,\"error\":\"no player in combat\"}");
        var pcs = player.PlayerCombatState;
        if (pcs == null) return (500, "{\"ok\":false,\"error\":\"player combat state is null\"}");

        // ----- arg parse -----
        if (!cmd.TryGetProperty("card_idx", out var idxProp) || idxProp.ValueKind != JsonValueKind.Number)
            return (400, "{\"ok\":false,\"error\":\"missing or non-int 'card_idx'\"}");
        var cardIdx = idxProp.GetInt32();
        if (cardIdx < 0 || cardIdx >= pcs.Hand.Cards.Count)
            return (400, "{\"ok\":false,\"error\":\"card_idx out of range\",\"hand_size\":" + pcs.Hand.Cards.Count + "}");

        var card = pcs.Hand.Cards[cardIdx];

        Creature? target = null;
        if (cmd.TryGetProperty("target_combat_id", out var targetProp)
            && targetProp.ValueKind == JsonValueKind.Number)
        {
            var targetId = (uint)targetProp.GetInt32();
            target = combat.Creatures.FirstOrDefault(c => c.CombatId == targetId);
            if (target == null)
                return (400, "{\"ok\":false,\"error\":\"target_combat_id not found\",\"target_combat_id\":" + targetId + "}");
        }

        // ----- legality re-check (defensive: cached snapshot may be slightly stale) -----
        if (!card.CanPlay(out var unplayableReason, out _))
        {
            return (409, "{\"ok\":false,\"error\":\"card not playable\",\"card_id\":" + JsonStr(card.Id.Entry) +
                ",\"unplayable_reason\":\"" + unplayableReason + "\"}");
        }
        if (!card.CanPlayTargeting(target))
        {
            // Covers both targeted-card-with-bad-target AND targetless-card-with-target.
            return (409, "{\"ok\":false,\"error\":\"card cannot be played against this target\",\"card_id\":" + JsonStr(card.Id.Entry) +
                ",\"target_combat_id\":" + (target?.CombatId.ToString() ?? "null") +
                ",\"card_target_type\":\"" + card.TargetType + "\"}");
        }

        // ----- dispatch -----
        Log.Info($"{Tag} play_card: {card.Id.Entry} (idx={cardIdx})" +
                 (target != null ? $" target={target.CombatId}({target.Monster?.Id.Entry ?? "player"})" : " (no target)"));

        var roundBefore = combat.RoundNumber;
        var sideBefore = combat.CurrentSide;
        var hpBefore = player.Creature.CurrentHp;
        var energyBefore = pcs.Energy;

        // CRITICAL: use TryManualPlay (the player-input path) rather than
        // CardCmd.AutoPlay. AutoPlay is for triggered/free plays — it sets
        // EnergySpent=0 and does NOT deduct energy (used by WhisperingEarring
        // relic, KnifeTrap card effect, SlyDiscard mechanic, etc.). The agent
        // hitting it as a stand-in for "click to play" produced the
        // free-energy bug observed in Day-5 first acceptance test:
        // 8 cards in round 2 with energy=3 max.
        //
        // TryManualPlay does the full pipeline:
        //   CanPlayTargeting check -> EnqueueManualPlay
        //     -> RunManager.Instance.ActionQueueSynchronizer.RequestEnqueue(PlayCardAction)
        //         -> ActionExecutor picks it up async -> OnPlayWrapper(isAutoPlay:false)
        //             -> SpendResources (deducts energy + stars correctly)
        bool played;
        try
        {
            played = card.TryManualPlay(target);
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} TryManualPlay threw: {ex}");
            return (500, "{\"ok\":false,\"error\":\"TryManualPlay threw\",\"message\":" + JsonStr(ex.Message) + "}");
        }
        if (!played)
        {
            return (409, "{\"ok\":false,\"error\":\"TryManualPlay returned false\",\"card_id\":" + JsonStr(card.Id.Entry) + "}");
        }

        // dev plan §2.3 sync invariant: wait for queue to drain — TryManualPlay
        // enqueues PlayCardAction asynchronously, so we await its completion
        // via the game's own sync primitive: ActionQueueSet.BecameEmpty() Task.
        var aqs = RunManager.Instance.ActionQueueSet;
        if (aqs != null)
        {
            try
            {
                await aqs.BecameEmpty().WaitAsync(TimeSpan.FromSeconds(10));
            }
            catch (TimeoutException)
            {
                Log.Warn($"{Tag} ActionQueueSet.BecameEmpty timed out after 10s — combat may be stuck");
                // Don't fail the step — let the client decide via /observe
            }
        }

        // Refresh observation cache so next /observe sees the post-play state.
        HttpBridge.RefreshObservation();

        var combatAfter = CombatManager.Instance.DebugOnlyGetState();
        var roundAfter = combatAfter?.RoundNumber ?? roundBefore;
        var hpAfter = player.Creature.CurrentHp;
        var energyAfter = pcs.Energy;
        var stillInCombat = CombatManager.Instance.IsInProgress;

        var sb = new StringBuilder(256);
        sb.Append("{\"ok\":true,\"action\":\"play_card\"");
        sb.Append(",\"card_id\":").Append(JsonStr(card.Id.Entry));
        if (target != null) sb.Append(",\"target_combat_id\":").Append(target.CombatId);
        sb.Append(",\"round_before\":").Append(roundBefore);
        sb.Append(",\"round_after\":").Append(roundAfter);
        sb.Append(",\"side_before\":\"").Append(sideBefore).Append('"');
        sb.Append(",\"hp_delta\":").Append(hpAfter - hpBefore);
        sb.Append(",\"energy_before\":").Append(energyBefore);
        sb.Append(",\"energy_after\":").Append(energyAfter);
        sb.Append(",\"energy_delta\":").Append(energyAfter - energyBefore);
        sb.Append(",\"still_in_combat\":").Append(stillInCombat ? "true" : "false");
        sb.Append(",\"is_play_phase\":").Append(CombatManager.Instance.IsPlayPhase ? "true" : "false");
        sb.Append('}');
        return (200, sb.ToString());
    }

    // -------------------------- end_turn --------------------------

    private static (int status, string body) EndTurn()
    {
        if (!CombatManager.Instance.IsInProgress)
            return (409, "{\"ok\":false,\"error\":\"not in combat\"}");
        if (!CombatManager.Instance.IsPlayPhase)
            return (409, "{\"ok\":false,\"error\":\"not in play phase\"}");

        var combat = CombatManager.Instance.DebugOnlyGetState();
        var player = combat?.Players.FirstOrDefault();
        if (player == null) return (500, "{\"ok\":false,\"error\":\"no player\"}");

        var roundBefore = combat!.RoundNumber;
        Log.Info($"{Tag} end_turn (round {roundBefore})");

        // EndTurn is fire-and-forget — it triggers the enemy turn asynchronously.
        // The next /observe will reflect the state once TurnStarted/TurnEnded fire.
        PlayerCmd.EndTurn(player, canBackOut: false);

        return (200, "{\"ok\":true,\"action\":\"end_turn\",\"round_before\":" + roundBefore + "}");
    }

    // -------------------------- helpers --------------------------

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
