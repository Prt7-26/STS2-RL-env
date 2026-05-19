using System.Collections.Generic;
using System.Linq;
using System.Text;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;

namespace Sts2Gym;

/// <summary>
/// Day-4 P0: hand-built JSON for mid-combat state (dev plan §2.1 path b).
///
/// Why hand-built instead of System.Text.Json: CombatState / Creature /
/// PlayerCombatState do NOT have ToSerializable() methods (the game saves only
/// between rooms — task E confirmed). System.Text.Json reflection on these
/// types would emit a lot of internal scaffolding (Godot Node refs, scene
/// references, event delegates) we don't want exposed. StringBuilder is the
/// minimum-surprise path.
///
/// Output mirrors dev plan §2.1 path-b dataclass design:
///   SerializableCombatState     -> top object
///   SerializableCreature[]      -> creatures
///   SerializablePlayerCombatState[] -> players (mid-combat data)
///   SerializablePower[]         -> per-creature
///
/// PartialObs mode (partial=true) masks per dev plan §2.8 list. Day-4 masks
/// implemented:
///   - draw_pile content (replaced with count-only)
///   - exhaust_pile / discard_pile / play_pile content kept (visible to player)
///   - hidden powers (IsHidden flag — TODO Day-5 once we confirm the API)
///   - first-encounter enemy max_hp (TODO Day-5 — need DiscoveredEnemies join)
/// RNG / RelicGrabBag masking happens at the wrapper layer (HttpBridge),
/// not here.
/// </summary>
internal static class CombatSnapshot
{
    /// <summary>
    /// Build the combat-extension JSON, or null when not in combat.
    /// Caller stitches this into the /observe response as "combat": {...}.
    /// </summary>
    public static string? Build(bool partial)
    {
        var combat = CombatManager.Instance.DebugOnlyGetState();
        if (combat == null) return null;

        var sb = new StringBuilder(2048);
        sb.Append('{');

        // ----- top-level scalars -----
        sb.Append("\"round\":").Append(combat.RoundNumber);
        sb.Append(",\"current_side\":\"").Append(combat.CurrentSide).Append('"');
        sb.Append(",\"play_phase\":").Append(CombatManager.Instance.IsPlayPhase ? "true" : "false");
        sb.Append(",\"encounter\":").Append(JsonStr(combat.Encounter?.Id.Entry));
        sb.Append(",\"modifier_ids\":[");
        var firstMod = true;
        foreach (var m in combat.Modifiers)
        {
            if (!firstMod) sb.Append(',');
            sb.Append(JsonStr(m.Id.Entry));
            firstMod = false;
        }
        sb.Append(']');

        // ----- creatures (allies + enemies in one list, distinguishable via is_player/side) -----
        sb.Append(",\"creatures\":[");
        var firstC = true;
        foreach (var c in combat.Creatures)
        {
            if (!firstC) sb.Append(',');
            AppendCreature(sb, c, partial);
            firstC = false;
        }
        sb.Append(']');

        sb.Append(",\"enemy_count\":").Append(combat.Enemies.Count);
        sb.Append(",\"creature_count\":").Append(combat.Creatures.Count);
        sb.Append(",\"hittable_enemy_count\":").Append(combat.HittableEnemies.Count);

        // ----- players (mid-combat fields not in SerializableRun) -----
        sb.Append(",\"players\":[");
        var firstP = true;
        foreach (var p in combat.Players)
        {
            if (!firstP) sb.Append(',');
            AppendPlayerCombatState(sb, p, partial);
            firstP = false;
        }
        sb.Append(']');

        // ----- escaped (for monsters that fled) -----
        sb.Append(",\"escaped_count\":").Append(combat.EscapedCreatures.Count);

        sb.Append('}');
        return sb.ToString();
    }

    // -------------------------- creature --------------------------

    private static void AppendCreature(StringBuilder sb, Creature c, bool partial)
    {
        sb.Append('{');
        sb.Append("\"combat_id\":");
        if (c.CombatId.HasValue) sb.Append(c.CombatId.Value); else sb.Append("null");
        sb.Append(",\"side\":\"").Append(c.Side).Append('"');
        sb.Append(",\"is_player\":").Append(c.IsPlayer ? "true" : "false");
        sb.Append(",\"is_alive\":").Append(c.IsAlive ? "true" : "false");
        sb.Append(",\"is_hittable\":").Append(c.IsHittable ? "true" : "false");
        sb.Append(",\"current_hp\":").Append(c.CurrentHp);
        sb.Append(",\"max_hp\":").Append(c.MaxHp);
        sb.Append(",\"block\":").Append(c.Block);
        sb.Append(",\"slot_name\":").Append(JsonStr(c.SlotName));

        if (c.IsPlayer && c.Player != null)
        {
            sb.Append(",\"character_id\":").Append(JsonStr(c.Player.Character.Id.Entry));
            sb.Append(",\"net_id\":").Append(c.Player.NetId);
        }
        else if (c.Monster != null)
        {
            sb.Append(",\"monster_id\":").Append(JsonStr(c.Monster.Id.Entry));
        }

        // powers (buffs/debuffs)
        sb.Append(",\"powers\":[");
        var firstPow = true;
        foreach (var pow in c.Powers)
        {
            if (!firstPow) sb.Append(',');
            AppendPower(sb, pow);
            firstPow = false;
        }
        sb.Append(']');

        // monster intent
        if (!c.IsPlayer && c.Monster != null)
        {
            AppendIntent(sb, c, c.Monster);
        }

        sb.Append('}');
    }

    private static void AppendPower(StringBuilder sb, PowerModel pow)
    {
        sb.Append("{\"id\":").Append(JsonStr(pow.Id.Entry));
        sb.Append(",\"amount\":").Append(pow.Amount);
        if (pow.DisplayAmount != pow.Amount)
        {
            sb.Append(",\"display_amount\":").Append(pow.DisplayAmount);
        }
        sb.Append('}');
    }

    private static void AppendIntent(StringBuilder sb, Creature monsterCreature, MonsterModel monster)
    {
        sb.Append(",\"next_move\":{");
        sb.Append("\"id\":").Append(JsonStr(monster.NextMove.Id));
        sb.Append(",\"intents\":[");

        var firstI = true;
        foreach (var intent in monster.NextMove.Intents)
        {
            if (!firstI) sb.Append(',');
            sb.Append('{');
            sb.Append("\"type\":\"").Append(intent.IntentType).Append('"');

            // For attack intents, also expose the projected damage.
            if (intent is AttackIntent attack)
            {
                int totalDamage = -1;
                try
                {
                    // GetTotalDamage takes (targets, owner). We use empty target list
                    // since attack damage doesn't depend on target identity in most cases.
                    // If the calc blows up, fall back to -1 (unknown).
                    totalDamage = attack.GetTotalDamage(System.Array.Empty<Creature>(), monsterCreature);
                }
                catch
                {
                    // swallow — projected-damage best-effort, not load-bearing for Day-4
                }
                sb.Append(",\"total_damage\":").Append(totalDamage);
                sb.Append(",\"repeats\":").Append(attack.Repeats);
            }

            sb.Append('}');
            firstI = false;
        }
        sb.Append("]}");
    }

    // -------------------------- player combat state --------------------------

    private static void AppendPlayerCombatState(StringBuilder sb, Player p, bool partial)
    {
        var pcs = p.PlayerCombatState;
        sb.Append('{');
        sb.Append("\"net_id\":").Append(p.NetId);

        if (pcs == null)
        {
            // Combat is being torn down or player has no combat state — minimal payload.
            sb.Append(",\"in_combat_state\":false}");
            return;
        }

        sb.Append(",\"in_combat_state\":true");
        sb.Append(",\"energy\":").Append(pcs.Energy);
        sb.Append(",\"max_energy\":").Append(pcs.MaxEnergy);
        sb.Append(",\"stars\":").Append(pcs.Stars);

        // Hand and visible piles: always emit the card lists (player can see).
        AppendPile(sb, ",\"hand\":", pcs.Hand);
        AppendPile(sb, ",\"discard_pile\":", pcs.DiscardPile);
        AppendPile(sb, ",\"exhaust_pile\":", pcs.ExhaustPile);
        AppendPile(sb, ",\"play_pile\":", pcs.PlayPile);

        // Draw pile: PartialObs hides card identities (player can only see count + sometimes top-of-deck).
        if (partial)
        {
            sb.Append(",\"draw_pile_partial\":true");
            sb.Append(",\"draw_count\":").Append(pcs.DrawPile.Cards.Count);
        }
        else
        {
            AppendPile(sb, ",\"draw_pile\":", pcs.DrawPile);
        }

        // Counts always present (cheap, useful for both modes).
        sb.Append(",\"hand_count\":").Append(pcs.Hand.Cards.Count);
        sb.Append(",\"draw_count\":").Append(pcs.DrawPile.Cards.Count);
        sb.Append(",\"discard_count\":").Append(pcs.DiscardPile.Cards.Count);
        sb.Append(",\"exhaust_count\":").Append(pcs.ExhaustPile.Cards.Count);
        sb.Append(",\"play_count\":").Append(pcs.PlayPile.Cards.Count);

        // Pets (Necrobinder Osty etc.) — emit as creatures.
        sb.Append(",\"pets\":[");
        var firstPet = true;
        foreach (var pet in pcs.Pets)
        {
            if (!firstPet) sb.Append(',');
            AppendCreature(sb, pet, partial);
            firstPet = false;
        }
        sb.Append(']');

        sb.Append('}');
    }

    private static void AppendPile(StringBuilder sb, string fieldPrefix, CardPile pile)
    {
        sb.Append(fieldPrefix).Append('[');
        var first = true;
        foreach (var card in pile.Cards)
        {
            if (!first) sb.Append(',');
            AppendCard(sb, card);
            first = false;
        }
        sb.Append(']');
    }

    private static void AppendCard(StringBuilder sb, CardModel card)
    {
        sb.Append('{');
        sb.Append("\"id\":").Append(JsonStr(card.Id.Entry));
        sb.Append(",\"cost\":").Append(card.EnergyCost.GetResolved());
        sb.Append(",\"canonical_cost\":").Append(card.EnergyCost.Canonical);
        sb.Append(",\"costs_x\":").Append(card.EnergyCost.CostsX ? "true" : "false");
        sb.Append(",\"upgrade_level\":").Append(card.CurrentUpgradeLevel);
        sb.Append(",\"is_upgraded\":").Append(card.IsUpgraded ? "true" : "false");
        sb.Append(",\"is_upgradable\":").Append(card.IsUpgradable ? "true" : "false");
        // TargetType drives action-codec target enumeration (dev plan §3.4).
        // Python side uses this to know whether to require a target_combat_id.
        sb.Append(",\"target_type\":\"").Append(card.TargetType).Append('"');
        // CanPlay is the action-mask oracle (dev plan §2.3) — expose so clients
        // know the legal hand subset without re-deriving it.
        if (card.Pile?.IsCombatPile == true)
        {
            bool canPlay = false;
            try { canPlay = card.CanPlay(out _, out _); } catch { /* defensive */ }
            sb.Append(",\"can_play\":").Append(canPlay ? "true" : "false");
        }
        sb.Append('}');
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
