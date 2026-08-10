# Quick Start

This walks through the GUI. Every step has an equivalent CLI subcommand if you'd rather
script it - see the [README](README.md#cli-subcommands-at-a-glance) for the list, or
`mo2-modlist-vault.exe <command> --help`.

## 0. First launch

Run `mo2-modlist-vault.exe` (or `python -m modlist_vault.gui` from source). The Nexus API
key/game domain bar at the top is shared across all tabs - paste your
[Nexus Premium API key](https://next.nexusmods.com/settings/api-keys) there before your first
restore (backup and vault snapshots don't need it).

## 1. Take your first backup

Open the **Backup** tab.

1. **MO2 Instance Folder** - the folder containing `profiles/`, `mods/`, `downloads/`, and
   `ModOrganizer.ini`.
2. **Profile** - fills in automatically once the instance folder is set.
3. **Output** - where to write the backup `.zip`.
4. **MO2 Portable .7z** - the official portable MO2 release archive you originally
   downloaded (usually already sitting in your `downloads/` folder). Needed to capture MO2
   itself and any third-party plugins for later restore.
5. **Real Game Path** - your actual Steam/GOG install folder. Only matters if your instance's
   `gamePath` points at a local copy inside the instance (a "Stock Game" folder) rather than
   the real install - leave it if you're not sure, you can always add it later.
6. Click **Backup**. Watch the log panel for progress; when it's done you have a single
   `.zip` with everything needed to rebuild this exact modlist elsewhere.

## 2. Restore it (e.g. on a fresh machine, or a fresh instance folder)

Open the **Restore** tab.

1. **Target Instance Folder** - an empty (or fresh) folder to rebuild into.
2. **Backup File (.zip)** - the file from step 1.
3. **Game Path** - your real Steam/GOG install.
4. Optionally check **Create local Stock Game copy** if you want MO2/mods kept off your real
   install entirely (a local copy is built and used instead).
5. Optionally set **Existing Downloads Folder** to a prior downloads cache - archives found
   there by exact filename are copied in instead of redownloaded.
6. Click **Setup + Restore**. This extracts MO2 itself, then rebuilds every mod directly from
   its recipe - no installer, no FOMOD dialogs. Watch the log for `[n/total] Restoring ...`
   progress.
7. Once it finishes, **open MO2 once** (just launch it - nothing to click) so it discovers
   the newly-placed plugins.
8. Back in the Restore tab, click **Finalize** (or run it from the Finalize tab) to match
   plugin load order to the backup.

If MO2 (or a helper process) is still holding a file open and a restore fails with a
permission error, use **Close MO2 Processes** in the top bar and try again.

## 3. Start a vault (repeated, deduplicated snapshots)

Open the **Vault** tab. Same instance/profile/MO2-archive/real-game-path fields as Backup,
plus:

1. **Vault Folder** - pick anywhere; it's created on first use. Reuse the same folder every
   time to build up a history.
2. Click **Take Snapshot**. The first snapshot stores everything (like a normal backup); each
   snapshot after that only stores content that's actually new or changed - the History list
   shows a one-line changelog per snapshot (e.g. "+2 mods, load order changed").
3. Select any snapshot in the History list to see its full changelog in the detail pane.
4. **Restore to This Point** materializes that snapshot back into a standalone backup `.zip`
   and runs the same Setup + Restore flow from step 2 against it - into whatever Target
   Instance Folder / Game Path the Restore tab (or this Vault tab) currently has set.

Take a snapshot before any risky change (a big new mod, a load-order overhaul) so you always
have a point to roll back to.

## 4. One-click restore points from inside MO2 (optional)

If you've installed the companion **MO2 Vault Integration** plugin (see its own README):

1. In MO2, find the padlock toolbar icon (or "Vault Restore Point" under the Tools menu).
2. Open its **Settings...** and set:
   - **Tool Folder** - the folder containing `mo2-modlist-vault.exe`.
   - **Vault Folder** - the same vault folder from step 3.
3. Click **Create Restore Point** any time, without closing MO2 - it reads your instance,
   active profile, and real game path live from MO2 itself, so there's nothing else to keep
   in sync. A result popup shows the changelog once it's done.
4. **Open Vault** launches the full desktop app (e.g. to browse history or restore) - restoring
   still requires MO2 closed first, which the desktop app's own "Close MO2 Processes" button
   handles.

## Tips

- Vault snapshots currently re-hash your whole modlist every time (no shortcut for unchanged
  files yet), so a snapshot can take a while on a large list - that's expected, not stuck.
  Check Task Manager for a still-running `mo2-modlist-vault.exe` process if you're unsure.
- Settings are remembered per-tab in `%LOCALAPPDATA%\mo2-modlist-vault\settings.json`. Use
  **Clear All Settings...** in the top bar to reset everything (including the API key) - safe
  to do before handing a copy of the tool to someone else.
