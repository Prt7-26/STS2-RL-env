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
    // Day-14 speed-tune: pre-built "observation with inline action_mask" so
    // /observe?with_mask=1 needs zero string-splicing on the hot path. Saves
    // one HTTP round-trip per agent step.
    private static volatile string _cachedFullObsWithMask = EmptyObservation;
    private static volatile string _cachedPartialObsWithMask = EmptyObservation;
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
            // Pre-splice "obs with action_mask inline" — single string concat
            // at refresh time so /observe?with_mask=1 is the same hot path
            // cost as plain /observe.
            _cachedFullObsWithMask = InlineMask(_cachedFullObs, _cachedActionMask);
            _cachedPartialObsWithMask = InlineMask(_cachedPartialObs, _cachedActionMask);
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
            _cachedFullObsWithMask = err;
            _cachedPartialObsWithMask = err;
        }
    }

    /// <summary>Splice ``,"action_mask":<mask>`` in just before the closing brace of obs.</summary>
    private static string InlineMask(string obs, string mask)
    {
        if (string.IsNullOrEmpty(obs) || obs[obs.Length - 1] != '}') return obs;
        var inner = obs.Substring(0, obs.Length - 1);
        return inner + ",\"action_mask\":" + mask + "}";
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

        // Day-9.4: partial-obs masking on the SerializableRun JSON.
        //   - run.rng.counters → hidden (replaced with masked sentinel). Knowing
        //     the next RNG counter would let an agent predict future draws / map
        //     point generation / monster intent dice rolls.
        //   - run.shared_relic_grab_bag → keep size, hide pool contents. Lets
        //     the agent know "how many relics remain to be drawn" without seeing
        //     which specific ones.
        //   - max_hp on enemies: NOT masked. HP bars are visible to humans in
        //     real time (user pushback on Day-9 plan: "怪的血量上限不是人能看到的吗").
        if (partial)
        {
            runJson = MaskRunForPartial(runJson);
        }

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
        // Day-10.A: surface non-combat phase context so agents know what's actionable.
        AppendNonCombatJson(sb, phase);
        sb.Append(",\"run\":").Append(runJson).Append('}');
        return sb.ToString();
    }

    /// <summary>
    /// Day-10.A: append "map"/"event"/"reward"/"game_over" fields when applicable.
    /// Each phase-specific block is best-effort and skipped on any read error so
    /// the rest of /observe stays serviceable.
    /// </summary>
    private static void AppendNonCombatJson(StringBuilder sb, string phase)
    {
        try
        {
            if (phase == "map") AppendMapJson(sb);
            else if (phase == "event") AppendEventJson(sb);
            else if (phase == "reward") AppendRewardJson(sb);
            else if (phase == "game_over") AppendGameOverJson(sb);
            else if (phase == "shop") AppendShopJson(sb);
            else if (phase == "rest") AppendRestJson(sb);
            else if (phase == "relic_select") AppendRelicSelectJson(sb);
            else if (phase == "card_reward_select") AppendCardRewardSelectJson(sb);
            else if (phase == "bundle_select") AppendBundleSelectJson(sb);
            else if (phase == "treasure") AppendTreasureJson(sb);
        }
        catch (Exception ex)
        {
            Log.Warn($"{Tag} AppendNonCombatJson failed: {ex.Message}");
        }
    }

    private static void AppendTreasureJson(StringBuilder sb)
    {
        // Day-14 hotfix: NTreasureRoom flow per AutoSlay's TreasureRoomHandler.cs:
        //   1) Click the chest (NButton "%Chest" inside NTreasureRoom) — sets
        //      _hasChestBeenOpened=true and reveals NTreasureRoomRelicHolder[] .
        //   2) Click each enabled NTreasureRoomRelicHolder to claim relics.
        //   3) Click NProceedButton to leave.
        var info = NonCombatHandlers.PeekTreasure();
        sb.Append(",\"treasure\":{");
        sb.Append("\"chest_open\":").Append(info.ChestOpen ? "true" : "false");
        sb.Append(",\"can_proceed\":").Append(info.CanProceed ? "true" : "false");
        sb.Append(",\"relics\":[");
        for (int i = 0; i < info.Holders.Count; i++)
        {
            if (i > 0) sb.Append(',');
            var h = info.Holders[i];
            sb.Append("{\"idx\":").Append(i)
              .Append(",\"is_enabled\":").Append(h.IsEnabled ? "true" : "false")
              .Append(",\"id\":").Append(JsonEncodedString(h.RelicId ?? ""))
              .Append(",\"rarity\":").Append(JsonEncodedString(h.Rarity ?? ""))
              .Append('}');
        }
        sb.Append("]}");
    }

    private static void AppendBundleSelectJson(StringBuilder sb)
    {
        // Day-10.O: NChooseABundleSelectionScreen — pick 1 of N card bundles.
        // AutoSlay clicks bundle.Hitbox then NConfirmButton. We collapse this
        // into a single /step bundle_pick.
        var bundles = NonCombatHandlers.EnumerateBundles();
        sb.Append(",\"bundle_select\":{\"count\":").Append(bundles.Count);
        sb.Append(",\"bundles\":[");
        for (int i = 0; i < bundles.Count; i++)
        {
            if (i > 0) sb.Append(',');
            sb.Append("{\"idx\":").Append(i).Append(",\"cards\":[");
            var cards = bundles[i].Bundle;
            for (int c = 0; c < (cards?.Count ?? 0); c++)
            {
                if (c > 0) sb.Append(',');
                sb.Append(JsonEncodedString(cards![c].Id.Entry));
            }
            sb.Append("]}");
        }
        sb.Append("]}");
    }

    private static void AppendCardRewardSelectJson(StringBuilder sb)
    {
        // Day-10.G: NCardRewardSelectionScreen is pushed when the player clicks
        // a CardReward NRewardButton. AutoSlay's pattern is UiHelper.FindAll
        // <NCardHolder> then EmitSignal(Pressed) on one.
        var cards = NonCombatHandlers.EnumerateCardRewardHolders();
        sb.Append(",\"card_reward_select\":{\"count\":").Append(cards.Count);
        sb.Append(",\"cards\":[");
        for (int i = 0; i < cards.Count; i++)
        {
            if (i > 0) sb.Append(',');
            var model = cards[i].CardNode?.Model;
            sb.Append("{\"idx\":").Append(i)
              .Append(",\"card_id\":").Append(JsonEncodedString(model?.Id.Entry))
              .Append('}');
        }
        sb.Append("]}");
    }

    private static void AppendRelicSelectJson(StringBuilder sb)
    {
        // Day-10.E: NChooseARelicSelection — Neow PRECARIOUS_SHEARS opens this,
        // treasure rooms too. Buttons exposed by index; click via /step relic_pick.
        var buttons = NonCombatHandlers.EnumerateRelicButtons();
        sb.Append(",\"relic_select\":{\"count\":").Append(buttons.Count);
        sb.Append(",\"items\":[");
        for (int i = 0; i < buttons.Count; i++)
        {
            if (i > 0) sb.Append(',');
            sb.Append("{\"idx\":").Append(i)
              .Append(",\"is_enabled\":").Append(buttons[i].IsEnabled ? "true" : "false")
              .Append('}');
        }
        sb.Append("]}");
    }

    private static void AppendShopJson(StringBuilder sb)
    {
        var state = MegaCrit.Sts2.Core.Runs.RunManager.Instance.DebugOnlyGetState();
        var room = state?.CurrentRoom as MegaCrit.Sts2.Core.Rooms.MerchantRoom;
        if (room == null) return;
        var entries = NonCombatHandlers.FlattenMerchantEntries(room.Inventory);
        var gold = state!.Players.FirstOrDefault()?.Gold ?? 0;
        sb.Append(",\"shop\":{");
        sb.Append("\"player_gold\":").Append(gold);
        sb.Append(",\"items\":[");
        for (int i = 0; i < entries.Count; i++)
        {
            if (i > 0) sb.Append(',');
            var e = entries[i];
            string kind = e switch
            {
                MegaCrit.Sts2.Core.Entities.Merchant.MerchantCardEntry => "card",
                MegaCrit.Sts2.Core.Entities.Merchant.MerchantRelicEntry => "relic",
                MegaCrit.Sts2.Core.Entities.Merchant.MerchantPotionEntry => "potion",
                MegaCrit.Sts2.Core.Entities.Merchant.MerchantCardRemovalEntry => "purge",
                _ => "other",
            };
            string? id = e switch
            {
                MegaCrit.Sts2.Core.Entities.Merchant.MerchantCardEntry ce => ce.CreationResult?.Card?.Id.Entry,
                MegaCrit.Sts2.Core.Entities.Merchant.MerchantRelicEntry re => re.Model?.Id.Entry,
                MegaCrit.Sts2.Core.Entities.Merchant.MerchantPotionEntry pe => pe.Model?.Id.Entry,
                _ => null,
            };
            sb.Append("{\"entry_idx\":").Append(i);
            sb.Append(",\"kind\":\"").Append(kind).Append('"');
            sb.Append(",\"id\":").Append(JsonEncodedString(id));
            sb.Append(",\"cost\":").Append(e.Cost);
            sb.Append(",\"is_stocked\":").Append(e.IsStocked ? "true" : "false");
            sb.Append(",\"enough_gold\":").Append(e.EnoughGold ? "true" : "false");
            sb.Append('}');
        }
        sb.Append("]");
        sb.Append(",\"can_leave\":true");
        sb.Append('}');
    }

    private static void AppendRestJson(StringBuilder sb)
    {
        var state = MegaCrit.Sts2.Core.Runs.RunManager.Instance.DebugOnlyGetState();
        var room = state?.CurrentRoom as MegaCrit.Sts2.Core.Rooms.RestSiteRoom;
        if (room == null) return;
        var options = room.Options;
        sb.Append(",\"rest\":{\"options\":[");
        for (int i = 0; i < options.Count; i++)
        {
            if (i > 0) sb.Append(',');
            var o = options[i];
            sb.Append("{\"option_idx\":").Append(i)
              .Append(",\"option_id\":").Append(JsonEncodedString(o.OptionId))
              .Append(",\"is_enabled\":").Append(o.IsEnabled ? "true" : "false")
              .Append('}');
        }
        sb.Append("]}");
    }

    private static void AppendMapJson(StringBuilder sb)
    {
        var state = MegaCrit.Sts2.Core.Runs.RunManager.Instance.DebugOnlyGetState();
        if (state?.Map == null) return;
        sb.Append(",\"map\":{");
        var cur = state.CurrentMapCoord;
        sb.Append("\"current\":");
        if (cur.HasValue) sb.Append($"[{cur.Value.col},{cur.Value.row}]"); else sb.Append("null");
        sb.Append(",\"reachable\":[");
        HashSet<MegaCrit.Sts2.Core.Map.MapPoint> legal;
        if (cur.HasValue)
        {
            var p = state.Map.GetPoint(cur.Value);
            legal = p?.Children ?? new HashSet<MegaCrit.Sts2.Core.Map.MapPoint>();
        }
        else legal = state.Map.startMapPoints;
        var first = true;
        foreach (var p in legal)
        {
            if (!first) sb.Append(',');
            sb.Append("{\"col\":").Append(p.coord.col).Append(",\"row\":").Append(p.coord.row)
              .Append(",\"point_type\":").Append(JsonEncodedString(p.PointType.ToString())).Append('}');
            first = false;
        }
        sb.Append(']');
        sb.Append(",\"act_index\":").Append(state.CurrentActIndex);
        sb.Append('}');
    }

    private static void AppendEventJson(StringBuilder sb)
    {
        var state = MegaCrit.Sts2.Core.Runs.RunManager.Instance.DebugOnlyGetState();
        var room = state?.CurrentRoom as MegaCrit.Sts2.Core.Rooms.EventRoom;
        if (room == null) return;
        var evt = MegaCrit.Sts2.Core.Runs.RunManager.Instance.EventSynchronizer.GetLocalEvent();
        if (evt == null) return;
        sb.Append(",\"event\":{");
        sb.Append("\"id\":").Append(JsonEncodedString(evt.Id.Entry));
        sb.Append(",\"is_finished\":").Append(evt.IsFinished ? "true" : "false");
        sb.Append(",\"options\":[");
        var options = evt.CurrentOptions;
        for (int i = 0; i < (options?.Count ?? 0); i++)
        {
            if (i > 0) sb.Append(',');
            var opt = options![i];
            sb.Append("{\"option_idx\":").Append(i)
              .Append(",\"text_key\":").Append(JsonEncodedString(opt.TextKey))
              .Append(",\"was_chosen\":").Append(opt.WasChosen ? "true" : "false")
              .Append(",\"is_locked\":").Append(opt.IsLocked ? "true" : "false")
              .Append(",\"is_proceed\":").Append(opt.IsProceed ? "true" : "false")
              .Append('}');
        }
        sb.Append("]}");
    }

    private static void AppendRewardJson(StringBuilder sb)
    {
        // Day-10.C: enumerate NRewardButton[] so agents can pick gold/potion/relic
        // /step take_reward_item. Card-reward picks route through ICardSelector
        // (already exposed in the top-level "selector" field) when their button
        // is clicked — agent does take_reward_item → selector_active=true →
        // select_pick → returns to reward screen.
        var overlay = MegaCrit.Sts2.Core.Nodes.Screens.Overlays.NOverlayStack.Instance?.Peek();
        sb.Append(",\"reward\":{\"screen\":").Append(JsonEncodedString(overlay?.GetType().Name ?? "unknown"));
        var buttons = NonCombatHandlers.EnumerateRewardButtons();
        sb.Append(",\"items\":[");
        for (int i = 0; i < buttons.Count; i++)
        {
            if (i > 0) sb.Append(',');
            var b = buttons[i];
            sb.Append("{\"idx\":").Append(i);
            sb.Append(",\"reward_type\":").Append(JsonEncodedString(b.Reward?.GetType().Name));
            sb.Append(",\"is_enabled\":").Append(b.IsEnabled ? "true" : "false");
            sb.Append('}');
        }
        sb.Append("]");
        sb.Append(",\"can_leave\":true}");
    }

    private static void AppendGameOverJson(StringBuilder sb)
    {
        sb.Append(",\"game_over\":{\"can_proceed\":true}");
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
    /// Day-9.4: mutate the serialized SerializableRun JSON to hide info that a
    /// human player can't see. Cheap-ish: parse via JsonNode (~10× slower than
    /// raw serialize but only runs once per game event, and only for the partial
    /// view).
    /// </summary>
    private static string MaskRunForPartial(string runJson)
    {
        try
        {
            var node = System.Text.Json.Nodes.JsonNode.Parse(runJson);
            if (node is System.Text.Json.Nodes.JsonObject root)
            {
                // Hide RNG counters but keep the seed (knowing the seed alone is
                // information available to a human at runs-tart — the dial-an-RNG
                // outcome is determined by accumulated step count which IS hidden).
                if (root["rng"] is System.Text.Json.Nodes.JsonObject rng)
                {
                    rng.Remove("counters");
                    rng["counters_masked"] = true;
                }
                // RelicGrabBag — keep the size only.
                if (root["shared_relic_grab_bag"] is System.Text.Json.Nodes.JsonObject bag)
                {
                    int size = 0;
                    if (bag["relics"] is System.Text.Json.Nodes.JsonArray relics)
                    {
                        size = relics.Count;
                    }
                    bag["size"] = size;
                    bag.Remove("relics");
                    bag["relics_masked"] = true;
                }
                return root.ToJsonString();
            }
        }
        catch (Exception ex)
        {
            Log.Warn($"{Tag} partial mask failed (returning unmasked): {ex.Message}");
        }
        return runJson;
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
            // Day-10.G: distinguish the card-reward sub-screen so the agent
            // knows to dispatch card_reward_pick (NCardHolder click) instead
            // of take_reward_item (NRewardButton click on the parent).
            if (t == typeof(NCardRewardSelectionScreen)) return "card_reward_select";
            if (t == typeof(NDeckUpgradeSelectScreen)) return "upgrade";
            if (t == typeof(NDeckTransformSelectScreen)) return "transform";
            if (t == typeof(NDeckEnchantSelectScreen)) return "enchant";
            if (t == typeof(NDeckCardSelectScreen)) return "card_select";
            if (t == typeof(NSimpleCardSelectScreen)) return "card_select";
            if (t == typeof(NChooseACardSelectionScreen)) return "card_select";
            // Day-10.O: NChooseABundleSelectionScreen doesn't route through our
            // ICardSelector — needs its own pick + confirm. Distinct phase.
            if (t == typeof(NChooseABundleSelectionScreen)) return "bundle_select";
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
                // ?partial=1 -> PartialObs view (dev plan §2.8: hides draw_pile contents).
                // ?with_mask=1 -> include action_mask inline as the "action_mask" key
                //                 (Day-14 speed-tune: spares the client a separate /action_mask
                //                 round-trip; cuts agent loop from 3 to 2 HTTP calls / step).
                // No query -> FullInfo without mask, original shape preserved for backcompat.
                var partialFlag = ctx.Request.QueryString["partial"];
                bool partial = partialFlag == "1" || partialFlag == "true";
                var maskFlag = ctx.Request.QueryString["with_mask"];
                bool withMask = maskFlag == "1" || maskFlag == "true";
                string cached;
                if (withMask)
                    cached = partial ? _cachedPartialObsWithMask : _cachedFullObsWithMask;
                else
                    cached = partial ? _cachedPartialObs : _cachedFullObs;
                body = WithFreshAge(cached);
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

            case "/selector/enable":
                if (method != "POST") { status = 405; body = "{\"ok\":false,\"error\":\"POST only\"}"; break; }
                Sts2GymMod.EnableSelector();
                RefreshObservation();
                status = 200;
                body = $"{{\"ok\":true,\"selector_enabled\":{(Sts2GymMod.SelectorEnabled ? "true" : "false")}}}";
                break;

            case "/selector/disable":
                if (method != "POST") { status = 405; body = "{\"ok\":false,\"error\":\"POST only\"}"; break; }
                Sts2GymMod.DisableSelector();
                RefreshObservation();
                status = 200;
                body = $"{{\"ok\":true,\"selector_enabled\":{(Sts2GymMod.SelectorEnabled ? "true" : "false")}}}";
                break;

            case "/start_run":
                if (method != "POST") { status = 405; body = "{\"ok\":false,\"error\":\"POST only\"}"; break; }
                (status, body) = RunStarter.HandleStartRunAsync(ctx).GetAwaiter().GetResult();
                break;

            case "/abandon_run":
                // POST. Tear down the current run via RunManager.CleanUp so a
                // subsequent /start_run can succeed. Used by ascension_test.py
                // which spawns multiple runs back-to-back. No-op if no run is
                // active. Must run on the Godot main thread because CleanUp
                // touches the scene tree (NOverlayStack.Clear, NMapScreen, etc).
                if (method != "POST") { status = 405; body = "{\"ok\":false,\"error\":\"POST only\"}"; break; }
                (status, body) = GameThread.RunOnMainAsync(() =>
                {
                    if (!RunManager.Instance.IsInProgress)
                        return Task.FromResult((200, "{\"ok\":true,\"was_active\":false}"));
                    try
                    {
                        RunManager.Instance.CleanUp(graceful: false);
                        RefreshObservation();
                        return Task.FromResult((200, "{\"ok\":true,\"was_active\":true}"));
                    }
                    catch (Exception ex)
                    {
                        Log.Error($"{Tag} /abandon_run CleanUp threw: {ex}");
                        return Task.FromResult((500, $"{{\"ok\":false,\"error\":\"CleanUp threw\",\"message\":{JsonEncodedString(ex.Message)}}}"));
                    }
                }).GetAwaiter().GetResult();
                break;

            case "/save_run":
                // GET only. Returns the current run as a JSON-serialized
                // SerializableRun, recoverable via POST /restore_run.
                if (method != "GET") { status = 405; body = "{\"ok\":false,\"error\":\"GET only\"}"; break; }
                (status, body) = SaveRestore.HandleSave();
                break;

            case "/restore_run":
                // POST { "save": <SerializableRun JSON> }. CleanUp the current
                // run (if any) and load the supplied save, like the Continue Run
                // button in the main menu.
                if (method != "POST") { status = 405; body = "{\"ok\":false,\"error\":\"POST only\"}"; break; }
                (status, body) = SaveRestore.HandleRestoreAsync(ctx).GetAwaiter().GetResult();
                break;

            case "/registry":
                // Day-9.3: dump card/monster id → int mappings for stable obs encoding.
                // Includes content_hash + game_version so the py side can detect
                // version skew on game updates.
                body = ModelRegistry.GetCached();
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
