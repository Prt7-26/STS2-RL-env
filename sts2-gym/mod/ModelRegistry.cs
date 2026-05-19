using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Models;

namespace Sts2Gym;

/// <summary>
/// Day-9.3: stable card_id → int and monster_id → int registry.
///
/// Why: agent obs needs to identify cards/enemies, not just count+cost. Without
/// stable ids the policy can't learn "Bash applies Vulnerable better than Strike".
///
/// Why versioned: STS2 is in EA, content adds + renames between patches. We
/// encode the mod's effective game version + a content_hash; the py side caches
/// the registry and re-fetches on hash mismatch. Unknown ids at agent runtime
/// fall through to slot 0 (UNKNOWN) so old policies don't crash on a new card.
///
/// Index assignment is alphabetical by entry id (deterministic across runs of
/// the same game version). Slot 0 is reserved for UNKNOWN.
/// </summary>
internal static class ModelRegistry
{
    private const string Tag = "[sts2gym/registry]";
    public const int UnknownIdx = 0;

    private static string? _cached;

    public static string GetCached()
    {
        // Lazy build on first access; ModelDb may not be fully ready at mod init.
        return _cached ??= BuildJson();
    }

    /// <summary>Force a rebuild (for testing or after a hypothetical reload).</summary>
    public static void Invalidate() => _cached = null;

    private static string BuildJson()
    {
        try
        {
            var cards = ModelDb.AllCards.Select(c => c.Id.Entry).Distinct().OrderBy(s => s, StringComparer.Ordinal).ToList();
            var monsters = ModelDb.Monsters.Select(m => m.Id.Entry).Distinct().OrderBy(s => s, StringComparer.Ordinal).ToList();
            var relics = ModelDb.AllRelics.Select(r => r.Id.Entry).Distinct().OrderBy(s => s, StringComparer.Ordinal).ToList();
            var encounters = ModelDb.AllEncounters.Select(e => e.Id.Entry).Distinct().OrderBy(s => s, StringComparer.Ordinal).ToList();

            var gameVersion = ResolveGameVersion();
            var contentHash = ComputeContentHash(cards, monsters, relics);

            var sb = new StringBuilder(64 * 1024);
            sb.Append("{");
            sb.Append($"\"schema_version\":1");
            sb.Append($",\"game_version\":{JsonStr(gameVersion)}");
            sb.Append($",\"content_hash\":{JsonStr(contentHash)}");
            sb.Append($",\"unknown_idx\":{UnknownIdx}");
            // Cards
            sb.Append(",\"cards\":{");
            for (int i = 0; i < cards.Count; i++)
            {
                if (i > 0) sb.Append(',');
                // Slot 0 reserved → start at 1
                sb.Append(JsonStr(cards[i])).Append(':').Append(i + 1);
            }
            sb.Append("}");
            // Monsters
            sb.Append(",\"monsters\":{");
            for (int i = 0; i < monsters.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(JsonStr(monsters[i])).Append(':').Append(i + 1);
            }
            sb.Append("}");
            // Relics — included for future encoding (not used in current obs).
            sb.Append(",\"relics\":{");
            for (int i = 0; i < relics.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(JsonStr(relics[i])).Append(':').Append(i + 1);
            }
            sb.Append("}");
            // Encounters — listed for callers who want to enumerate /reset targets
            sb.Append(",\"encounters\":[");
            for (int i = 0; i < encounters.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(JsonStr(encounters[i]));
            }
            sb.Append("]");
            sb.Append($",\"counts\":{{\"cards\":{cards.Count},\"monsters\":{monsters.Count},\"relics\":{relics.Count},\"encounters\":{encounters.Count}}}");
            sb.Append("}");
            Log.Info($"{Tag} built registry: {cards.Count} cards, {monsters.Count} monsters, {relics.Count} relics, " +
                     $"{encounters.Count} encounters, game_version={gameVersion}, hash={contentHash[..12]}");
            return sb.ToString();
        }
        catch (Exception ex)
        {
            Log.Error($"{Tag} BuildJson failed: {ex}");
            return $"{{\"error\":\"registry build failed\",\"message\":{JsonStr(ex.Message)}}}";
        }
    }

    private static string ResolveGameVersion()
    {
        // sts2.dll's assembly version. Stable enough as a "did anything change"
        // signal — content_hash is the authoritative fingerprint, but version
        // is what humans recognize ("v0.103.2 (2026.04.16)" shows up in-game).
        try
        {
            var asm = typeof(ModelDb).Assembly;
            return asm.GetName().Version?.ToString() ?? "unknown";
        }
        catch
        {
            return "unknown";
        }
    }

    private static string ComputeContentHash(List<string> cards, List<string> monsters, List<string> relics)
    {
        var sb = new StringBuilder();
        sb.Append("CARDS:");
        foreach (var c in cards) sb.Append(c).Append(',');
        sb.Append("MONSTERS:");
        foreach (var m in monsters) sb.Append(m).Append(',');
        sb.Append("RELICS:");
        foreach (var r in relics) sb.Append(r).Append(',');
        using var sha = SHA256.Create();
        var hash = sha.ComputeHash(Encoding.UTF8.GetBytes(sb.ToString()));
        return Convert.ToHexString(hash).ToLowerInvariant();
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
