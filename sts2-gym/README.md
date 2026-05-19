# STS2-Gym

RL / LLM environment bridge for Slay the Spire 2. Pre-alpha, in active development.

> **本工作区当前只在本地开发。** 反编译产物（`../sts2-reverse/`、`*.dll`、`*.pck`）已被 `.gitignore` 屏蔽，仓库本身可推送到公开 GitHub 但**编译需要本地有合法 STS2 副本**。

---

## Layout

```
sts2-gym/
├── mod/                    # C# mod (loaded into game process via official mod system)
│   ├── Sts2Gym.csproj
│   ├── sts2gym.json        # mod manifest
│   └── Sts2GymMod.cs       # entry + event handlers
└── py/                     # Python env (gymnasium.Env wrapping mod's HTTP endpoints)
    └── (empty for now)
```

Recon documentation lives one level up: [`../sts2-reverse/docs/recon/`](../sts2-reverse/docs/recon/).

Architectural North Star: [`../STS2_GYM_DEV_PLAN.md`](../STS2_GYM_DEV_PLAN.md).

---

## Prerequisites (developer machine)

- **.NET SDK ≥ 9.0** (`dotnet --version`). Tested with 10.0.x targeting net9.0.
- **macOS Apple Silicon** for current iteration (Linux / Windows / macOS x86_64 P1 milestone).
- **STS2 installed via Steam**. Default install path is detected by the build:
  ```
  ~/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/
  ```
- Decompiled `sts2.dll` + `0Harmony.dll` at `../sts2-reverse/` (these match the installed game DLL by md5).

---

## Building the mod

```bash
cd sts2-gym/mod
dotnet build -c Release
```

Produces `sts2-gym/mod/bin/Release/sts2gym.dll`.

The `<Reference>` items in `Sts2Gym.csproj` point at `../sts2-reverse/sts2.dll` and `../sts2-reverse/0Harmony.dll` with `<Private>false</Private>` — the build does **not** copy these into the output, because the game ships its own copies at runtime.

---

## Deploying the mod

### Recommended: one-shot smoke test script

```bash
./sts2-gym/scripts/smoke_test.sh
```

This script builds, deploys, waits for you to launch the game, then tails the log filtered to `[sts2gym]` and mod-loader lines. Override the STS2 install path via env var:

```bash
STS2_INSTALL=/custom/path ./sts2-gym/scripts/smoke_test.sh
```

### Manual fallback (macOS arm64)

```bash
STS2="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2"
# IMPORTANT: on macOS the mod dir must be INSIDE the .app bundle, next to the
# game binary. Godot's OS.GetExecutablePath() returns Contents/MacOS/<binary>,
# and ModManager.Initialize scans <dirname-of-executable>/mods/.
MOD_DIR="$STS2/SlayTheSpire2.app/Contents/MacOS/mods/sts2gym"
mkdir -p "$MOD_DIR"
cp sts2-gym/mod/sts2gym.json "$MOD_DIR/"
cp sts2-gym/mod/bin/Release/sts2gym.dll "$MOD_DIR/"
```

The manifest file name is flexible (any `*.json` in the mod dir is parsed), but the DLL filename **must** be `<manifest.id>.dll` — i.e. `sts2gym.dll`.

### macOS path caveats

- **The mod must live inside the signed .app bundle** (`Contents/MacOS/mods/`), not at the install root. Putting it at `<install>/mods/` silently fails — `ModManager` doesn't even log "no mods found", it just leaves `_mods.Count == 0` and returns early.
- **Steam may re-validate / re-download the .app bundle on update**, which would wipe `Contents/MacOS/mods/`. If you find your mod missing after a game update, just re-run `smoke_test.sh`.
- **macOS Gatekeeper / code signing**: putting files inside a signed bundle does not always invalidate the signature for ad-hoc launches via Steam, but if you ever see "app is damaged and can't be opened" after deploying, the workaround is `xattr -dr com.apple.quarantine "$STS2/SlayTheSpire2.app"`.

### STS2 log location (macOS)

```
~/Library/Application Support/SlayTheSpire2/logs/godot.log         # current
~/Library/Application Support/SlayTheSpire2/logs/godot<UTC-stamp>.log  # historical
```

The current run writes to `godot.log` (overwritten each launch) and also rotates a timestamped copy. `smoke_test.sh` tails whichever was most recently modified.

### First-launch checklist

1. Launch STS2.
2. Open the Mods UI. **Agree to mod loading** (this sets `SettingsSave.ModSettings.PlayerAgreedToModLoading = true` —
   without this, all mods are forcibly disabled). This is a one-time UX gate.
3. Restart STS2.
4. Check the log file. Look for:
   - `Loaded 1 mods (1 total)` — official mod loader confirmation
   - `[sts2gym] hello — ModInitializer.Init invoked` — our entry point fired
   - `[sts2gym] subscriptions: ...` — event subscriptions registered
5. Start a new run. Look for `[sts2gym] RunStarted #1: ascension=... players=... seed=...`.
6. Enter combat. Look for `[sts2gym] CombatSetUp #1: encounter=...` and `[sts2gym] TurnStarted #1: ...`.

### Log file location (macOS)

Godot writes logs under user data. Typical macOS path:

```
~/Library/Application Support/Godot/app_userdata/Slay the Spire 2/logs/
```

Tail the latest log:
```bash
tail -F "$HOME/Library/Application Support/Godot/app_userdata/Slay the Spire 2/logs/godot.log"
```

(Exact filename may vary; `grep` for the `[sts2gym]` tag.)

---

## What this hello-world verifies

The minimal P0 smoke test verifies:

| Verification | Mechanism |
|---|---|
| Manifest parsing | `Loaded 1 mods` line appears |
| DLL loading | `[sts2gym] hello — ModInitializer.Init invoked` line appears |
| `[ModInitializer(...)]` attribute discovery | same as above |
| Event subscription timing | `[sts2gym] RunStarted` appears when starting a new run |
| `RunManager.Instance.ToSave()` reuse (dev plan §2.1 path a) | `[sts2gym] SerializableRun snapshot OK: ...` line includes `schema=...`, `rng_streams=...` etc. |
| `CombatManager.Instance` events | `[sts2gym] CombatSetUp / TurnStarted / TurnEnded` lines appear in combat |
| `FastModeType.Instant` opt-in (dev plan §2.4) | `[sts2gym] FastMode: Normal -> Instant` line appears at run start, animations visibly fast |

If any of these miss, the mod-loading pipeline has a problem and we cannot proceed to ScenarioInjector / ActionDispatcher.

---

## Next steps (per dev plan)

Day 2:
- Deploy to game, verify all checklist items above
- Compare `CombatHistory.Entries` event sequence across `FastMode = Normal / Fast / Instant` for bit-exactness
- If Instant breaks bit-exactness, fall back to Fast and document the corner case

Day 3 and beyond: HTTP `/observe` endpoint, phase resolver, mid-combat `SerializableCombatState`. See [`../sts2-reverse/docs/recon/SUMMARY.md`](../sts2-reverse/docs/recon/SUMMARY.md) §4 for the 7-day plan.
