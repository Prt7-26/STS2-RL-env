# STS2 Reverse Engineering Workspace

LOCAL ONLY — do not push to any public repo.
Decompiled artifacts from a copy of Slay the Spire 2 I legally own,
used for personal mod development and research.

## Layout

- decompiled_dll/   ILSpy output of sts2.dll
- raw_pck/          GDRE Tools output of Slay the Spire 2.pck
- sts2.dll          Original DLL (for mod assembly references)
- 0Harmony.dll      Harmony library shipped with the game
- sts2.deps.json    Original deps manifest
- docs/             Notes, version stamp, RNG audit

## Key entry points

- decompiled_dll/MegaCrit.Sts2.Core.Combat/  combat loop
- decompiled_dll/MegaCrit.Sts2.Core.Run/     run-level state
- decompiled_dll/MegaCrit.Sts2.Core.Cards/   card definitions
- decompiled_dll/MegaCrit.Sts2.Core.Modding/ mod loader hooks
