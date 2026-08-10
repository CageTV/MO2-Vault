# MO2 Modlist Vault

A standalone backup/restore tool for Mod Organizer 2 modlists - a personal, Wabbajack-style
tool for backing up and restoring an entire modding setup with **minimal size**, and
restoring it onto a fresh machine with **zero manual FOMOD/install-wizard clicking**.

It is not an MO2 plugin for the backup/restore flow itself (a companion plugin exists for
triggering vault snapshots from inside MO2 - see [MO2 Vault Integration](#mo2-vault-integration-companion-plugin)
below) - it runs alongside MO2, reading and writing an instance folder directly.

## Why

Zipping up an entire modlist (mods + downloads + game files) produces an enormous archive,
most of which is redundant - it's content you can redownload from Nexus (or wherever it came
from) any time. MO2 Modlist Vault instead captures a **recipe**: for every mod, it works out
which files came from which downloaded archive (matched by content hash, not by name or
path), and stores just that recipe plus a small amount of "add-on" API metadata. Only content
that genuinely **can't** be redownloaded - a hand-placed custom mod, a tool's generated
output, a patched executable - gets bundled raw.

Restoring replays those recipes: redownload the archive from Nexus, extract it, copy the
matched files into place, done. No installer runs, no FOMOD dialog ever appears, and MO2
itself never has to be launched to reconstruct the modlist (only to pick up plugin load
order afterward, once).

## Features

- **Recipe-based mod reconstruction** - SHA1 hash matching between a downloaded archive and
  the installed mod folder, so restoring never needs the original install wizard or any
  manual choices.
- **Minimal backup size** - only content with no redownload source is bundled raw, with no
  size cap.
- **Full MO2 environment capture** - MO2 itself (portable release + any third-party plugins),
  the `tools/` folder (xEdit, DynDOLOD, Synthesis, BodySlide output, etc. - including
  multi-archive tools), profile settings (INIs, load order, BethINI cache), mod/separator
  highlight colors, and a "Stock Game" local copy overlay (e.g. a Creation Kit Platform
  Extended patcher) if your instance uses one.
- **Game-agnostic** - validated end-to-end on both a ~450-mod Skyrim Special Edition list and
  a Fallout 4 list; nothing in the tool is game-specific.
- **Vault: Time Machine-style snapshots** - take repeated, deduplicated snapshots into a
  vault over time instead of one full backup per run. Unchanged content is stored once no
  matter how many snapshots reference it (content-addressed blob storage, the same model
  git/restic/Borg use). Each snapshot shows a changelog against the previous one (mods
  added/removed/changed, load order changes, plugin changes) and can be restored
  independently.
- **GUI and CLI** - a PySide6 desktop app (Backup / Restore / Finalize / Vault tabs, a
  persistent log panel, remembered per-tab settings) and a full argparse CLI for scripting.
- **MO2 Vault Integration companion plugin** - a one-click "Create Restore Point" button
  inside MO2 itself.

## How it works

1. **Backup** (`backup` / the Backup tab) reads an MO2 instance's `modlist.txt`, matches
   every mod against its source archive under `downloads/`, and writes a small manifest +
   only the truly-unbundlable raw content into a `.zip`.
2. **Restore** (`setup-mo2` + `restore` / the Restore tab) extracts MO2 itself, overlays any
   captured customizations, then rebuilds every mod directly from its recipe - redownloading
   from Nexus (or pulling from an existing downloads folder you point it at) as needed.
3. **Finalize** (`finalize-restore` / the Finalize tab) - after opening MO2 once so it
   discovers plugins, matches plugin (esp/esm/esl) order to the backup.
4. **Vault** (`vault-snapshot` / the Vault tab) does the same capture as Backup, but into a
   shared, deduplicated snapshot store instead of a standalone zip - `vault-materialize`
   turns any snapshot back into a normal zip that `setup-mo2`/`restore`/`finalize-restore`
   consume unchanged.

## Installation

**Prebuilt exe**: build it yourself with PyInstaller (see [Building](#building-from-source))
- there's no separate release process yet. The build produces
`dist/mo2-modlist-vault/mo2-modlist-vault.exe`, a self-contained folder you can copy anywhere
(or hand to someone else - see [Portability](#portability)).

**From source**:
```bash
pip install -r requirements.txt
python -m modlist_vault.gui      # GUI
python -m modlist_vault.cli --help   # CLI
```
Requires Python 3.9+, [`py7zr`](https://pypi.org/project/py7zr/) (extracting/hashing `.7z`
archives) and [`PySide6`](https://pypi.org/project/PySide6/) (GUI only - the CLI works
without it).

## Usage

See [QUICKSTART.md](QUICKSTART.md) for a full walkthrough. Short version:

- **GUI**: launch it, fill in the Backup tab (instance folder, output path, your MO2
  portable `.7z`, real game path if you use a local "Stock Game" copy), click Backup. Restore
  is the same shape on a fresh instance folder. Vault is the same fields plus a Vault Folder
  - "Take Snapshot" instead of "Backup", with a timeline view and "Restore to This Point".
- **CLI**: `modlist-vault {backup,setup-mo2,restore,finalize-restore,vault-snapshot,
  vault-list,vault-materialize} --help` for full flag references - every GUI action has a
  matching CLI subcommand.

### CLI subcommands at a glance

| Command | Purpose |
|---|---|
| `backup` | Snapshot an MO2 instance into a standalone backup `.zip`. |
| `setup-mo2` | Extract MO2 itself into a fresh target instance from a backup. |
| `restore` | Rebuild every mod into a target instance from a backup's recipes. |
| `finalize-restore` | Match plugin order to the backup, after opening MO2 once. |
| `vault-snapshot` | Take a deduplicated snapshot into a vault. |
| `vault-list` | List a vault's snapshots with a one-line changelog summary each. |
| `vault-materialize` | Extract one vault snapshot into a standalone backup `.zip`. |

## Portability

Settings (last-used paths, the Nexus API key) live in a plain JSON file at
`%LOCALAPPDATA%\mo2-modlist-vault\settings.json` - not the Windows registry - specifically so
the app can be handed to someone else without leaking your paths or API key: a fresh copy on
a new machine starts with nothing configured. Use "Clear All Settings..." in the GUI (or
delete that file) to reset at any time.

## MO2 Vault Integration (companion plugin)

A separate MO2 plugin project, `MO2 Vault Integration`, adds a one-click "Create Restore
Point" toolbar button inside MO2 itself, so you don't need to leave MO2 to take a vault
snapshot. It shells out to this tool's exe rather than embedding any of its logic in MO2's
Python environment - see that project's own README for setup. It deliberately does **not**
support restoring (restoring needs MO2 closed - see [Known limitations](#known-limitations)),
only snapshotting.

## Building from source

```bash
pip install -r requirements-dev.txt
pyinstaller build_exe.spec
```
Produces a **onedir** build at `dist/mo2-modlist-vault/` (not onefile - faster startup for
the headless `vault-snapshot` calls the MO2 plugin makes, and matches how most MO2-plugin
companion tools distribute). The built exe is dual-mode: no arguments opens the GUI, any
arguments route to the CLI - see `modlist_vault/launcher.py`.

## Known limitations

- **Restoring needs MO2 (and its helper processes) closed.** Restoring writes directly into
  the live instance folder; if MO2 or a `usvfs_proxy_*.exe` helper still has a file open,
  writes fail with a permission error naming the exact locked file. The GUI has a "Close MO2
  Processes" button for this.
- **Snapshotting re-hashes every file every time.** `vault-snapshot` doesn't yet skip
  re-hashing content whose size/modified-time haven't changed since the last snapshot, so
  snapshot time scales with total modlist size, not with what actually changed. Fine for a
  few hundred mods; worth optimizing before relying on it for a very large (3000+ mod)
  modlist. See `PROJECT_STATE.md`.
- **Vault blob pruning isn't implemented.** Nothing removes blobs no longer referenced by any
  snapshot - a vault only grows. Not a problem for normal use, but worth knowing before
  filling a vault with many large, very different snapshots.


https://www.virustotal.com/gui/file/2f68901dc503abe426ca256bcfbe44c4faeb5314e093fb288d307f090b5ba798
