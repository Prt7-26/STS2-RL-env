using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using MegaCrit.Sts2.Core.Entities.CardRewardAlternatives;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.TestSupport;

namespace Sts2Gym;

/// <summary>
/// Day-8.1: Sts2-Gym's ICardSelector implementation.
///
/// Game-internal call sites:
///   * CardSelectCmd.From*: pretty much every "choose N cards from a list" UI screen
///     — Survivor's in-combat discard, deck upgrade at rest, transform at shop,
///     post-combat card reward (via FromSimpleGridForRewards), event-driven
///     "choose a card" picks, etc. All async via GetSelectedCards.
///   * CardReward.cs:166: only when the UI screen is NOT shown (rare/headless
///     path). Sync via GetSelectedCardReward. We return null (= skip) — the
///     screen-driven async path is the one we actually want to intercept.
///
/// Lifecycle:
///   1. Sts2GymMod.Init pushes a single instance via CardSelectCmd.PushSelector
///      (additive — does not require the stack to be empty).
///   2. Engine calls GetSelectedCards from any phase that wants user input on a
///      card pick. We stash a pending request (options + min + max) and return
///      a Task backed by a TaskCompletionSource.
///   3. HTTP /observe and /action_mask expose the pending request. Agent reads
///      it and submits a sequence of /step select_pick / select_confirm / select_skip
///      actions. StepRunner forwards these to <see cref="Resolve"/>.
///   4. When the agent confirms (or min==max==1 + single pick auto-confirms), we
///      complete the TCS, the engine's await unblocks, and the game proceeds.
///
/// Thread-safety: all mutations are inside a single lock — engine call site is
/// usually the main thread, /step handler runs on the HTTP background thread.
/// The TCS is constructed with RunContinuationsAsynchronously so completing it
/// from a non-game-thread doesn't sync-resume the engine continuation on our
/// HTTP thread.
/// </summary>
public sealed class Sts2GymCardSelector : ICardSelector
{
    private const string Tag = "[sts2gym/selector]";

    private readonly object _lock = new();
    private TaskCompletionSource<IEnumerable<CardModel>>? _pendingTcs;
    private List<CardModel>? _pendingOptions;
    private int _pendingMin;
    private int _pendingMax;
    private readonly List<int> _accumulator = new();

    /// <summary>
    /// True iff the engine is currently waiting on us to deliver a selection.
    /// </summary>
    public bool IsActive
    {
        get { lock (_lock) return _pendingTcs != null; }
    }

    public Task<IEnumerable<CardModel>> GetSelectedCards(IEnumerable<CardModel> options, int minSelect, int maxSelect)
    {
        var optList = options.ToList();
        TaskCompletionSource<IEnumerable<CardModel>> tcs;

        lock (_lock)
        {
            if (_pendingTcs != null)
            {
                // Engine waits for previous selector to complete before issuing a new
                // one in the same call chain. This means a stale pending request is
                // a bug — log loudly and resolve it with min defaults so the engine
                // doesn't deadlock waiting on our orphaned TCS.
                Log.Warn($"{Tag} GetSelectedCards while another is pending — auto-resolving previous with min picks");
                var stale = _pendingOptions!.Take(_pendingMin).ToList();
                _pendingTcs.TrySetResult(stale);
            }

            _pendingTcs = tcs = new TaskCompletionSource<IEnumerable<CardModel>>(TaskCreationOptions.RunContinuationsAsynchronously);
            _pendingOptions = optList;
            _pendingMin = minSelect;
            _pendingMax = maxSelect;
            _accumulator.Clear();
        }

        Log.Info($"{Tag} GetSelectedCards: {optList.Count} options, min={minSelect}, max={maxSelect}");
        // Refresh observation so /observe picks up the new selector context immediately.
        HttpBridge.RefreshObservation();
        return tcs.Task;
    }

    public CardModel? GetSelectedCardReward(IReadOnlyList<CardCreationResult> options, IReadOnlyList<CardRewardAlternative> alternatives)
    {
        // Sync path — only reachable when no reward screen is shown (rare). Returning
        // null = "skip" (no card added). The default game flow keeps the screen up
        // and goes through GetSelectedCards via CardSelectCmd.FromSimpleGridForRewards,
        // which we already handle.
        Log.Info($"{Tag} GetSelectedCardReward (sync) called with {options.Count} options — returning null (skip)");
        return null;
    }

    /// <summary>
    /// Apply a single pick. Returns (ok, message). If min==max==1, auto-confirms
    /// after the pick to mirror the player's UX (one click resolves immediately).
    /// </summary>
    public (bool ok, string message) Pick(int index)
    {
        lock (_lock)
        {
            if (_pendingTcs == null || _pendingOptions == null)
                return (false, "no selector active");
            if (index < 0 || index >= _pendingOptions.Count)
                return (false, $"index {index} out of range [0, {_pendingOptions.Count})");
            if (_accumulator.Contains(index))
                return (false, $"index {index} already picked (use select_unpick to remove)");
            if (_accumulator.Count >= _pendingMax)
                return (false, $"selection full ({_accumulator.Count}/{_pendingMax})");

            _accumulator.Add(index);

            // Auto-confirm on single-pick selectors so the agent doesn't need to
            // emit an explicit confirm for the 80% case.
            if (_pendingMin == 1 && _pendingMax == 1)
            {
                FinalizeLocked();
                return (true, "picked + auto-confirmed");
            }
            return (true, $"picked ({_accumulator.Count}/{_pendingMin}..{_pendingMax})");
        }
    }

    /// <summary>Remove a previously-picked index from the accumulator.</summary>
    public (bool ok, string message) Unpick(int index)
    {
        lock (_lock)
        {
            if (_pendingTcs == null) return (false, "no selector active");
            if (!_accumulator.Remove(index))
                return (false, $"index {index} not in accumulator");
            return (true, $"unpicked ({_accumulator.Count}/{_pendingMin}..{_pendingMax})");
        }
    }

    /// <summary>Resolve the selector with the current accumulator (must have >= min picks).</summary>
    public (bool ok, string message) Confirm()
    {
        lock (_lock)
        {
            if (_pendingTcs == null) return (false, "no selector active");
            if (_accumulator.Count < _pendingMin)
                return (false, $"need at least {_pendingMin} picks, have {_accumulator.Count}");
            FinalizeLocked();
            return (true, "confirmed");
        }
    }

    /// <summary>Resolve with an empty selection (only legal when min == 0).</summary>
    public (bool ok, string message) Skip()
    {
        lock (_lock)
        {
            if (_pendingTcs == null) return (false, "no selector active");
            if (_pendingMin > 0)
                return (false, $"cannot skip — min={_pendingMin}");
            _accumulator.Clear();
            FinalizeLocked();
            return (true, "skipped");
        }
    }

    /// Caller must hold _lock.
    /// Note: does NOT call HttpBridge.RefreshObservation. The engine's awaiting
    /// continuation resumes after our TCS completes, which fires game events
    /// (PlayerActionsDisabledChanged, TurnStarted, etc.) — those refresh the
    /// cache naturally on the main thread. Calling Refresh from the HTTP
    /// background thread here would race on Godot scene-tree reads.
    private void FinalizeLocked()
    {
        var selected = _accumulator.Where(i => i >= 0 && i < _pendingOptions!.Count)
                                    .Select(i => _pendingOptions![i])
                                    .ToList();
        var tcs = _pendingTcs!;
        _pendingTcs = null;
        _pendingOptions = null;
        _pendingMin = 0;
        _pendingMax = 0;
        _accumulator.Clear();
        Log.Info($"{Tag} resolved with {selected.Count} card(s): " +
                 string.Join(",", selected.Select(c => c.Id.Entry)));
        tcs.TrySetResult(selected);
    }

    /// <summary>
    /// Snapshot of the pending request for /observe and /action_mask. Returns a
    /// defensive copy of the options/accumulator under the lock.
    /// </summary>
    public PendingSnapshot? Snapshot()
    {
        lock (_lock)
        {
            if (_pendingTcs == null || _pendingOptions == null) return null;
            return new PendingSnapshot
            {
                Options = new List<CardModel>(_pendingOptions),
                Accumulator = new List<int>(_accumulator),
                MinSelect = _pendingMin,
                MaxSelect = _pendingMax,
            };
        }
    }

    public sealed class PendingSnapshot
    {
        public List<CardModel> Options = new();
        public List<int> Accumulator = new();
        public int MinSelect;
        public int MaxSelect;
    }

    /// <summary>
    /// Force-resolve any pending selector with min defaults. Used when the env
    /// resets to a new encounter — we must not leak across episode boundaries.
    /// </summary>
    public void ForceResolveWithDefault()
    {
        lock (_lock)
        {
            if (_pendingTcs == null) return;
            var picks = Enumerable.Range(0, Math.Min(_pendingMin, _pendingOptions?.Count ?? 0)).ToList();
            _accumulator.Clear();
            _accumulator.AddRange(picks);
            Log.Warn($"{Tag} force-resolving pending selector with {picks.Count} default picks");
            FinalizeLocked();
        }
    }
}
