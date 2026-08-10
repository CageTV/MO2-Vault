# MO2 Vault Integration

A companion MO2 plugin for [`mo2-modlist-vault`](../mo2-modlist-vault) - adds a one-click
**Create Restore Point** button inside MO2 itself, so you never have to leave MO2 to take a
vault snapshot.

It does not embed any of `mo2-modlist-vault`'s own logic (PySide6, py7zr, the backup/vault
code) inside MO2's Python environment - it shells out to a separately-built
`mo2-modlist-vault.exe`, the same way the reference plugin this was modeled on
(`ESLifier MO2 Integration`) shells out to `ESLifier.exe`. That keeps this plugin's only
dependencies `mobase` + Qt, both already provided by MO2 itself.

## Install

1. Build `mo2-modlist-vault.exe` first (see the main project's README).
2. Copy this whole folder into your MO2 instance's `plugins/` directory, e.g.:
   `<your instance>\plugins\MO2 Vault Integration\`
3. (Re)start MO2.

## Setup

1. Find the padlock toolbar icon (or "Vault Restore Point" under the Tools/wrench menu if the
   toolbar icon didn't land where expected).
2. Click **Settings...** and set:
   - **Tool Folder** - the folder containing `mo2-modlist-vault.exe`.
   - **Vault Folder** - where you want this instance's snapshots stored (created automatically
     on first use).

Both are stored per-instance, inside that instance's own `ModOrganizer.ini` (MO2's normal
plugin-setting mechanism) - no separate config file, and no risk of one MO2 instance's
settings bleeding into another's.

## Use

- **Create Restore Point** - takes a vault snapshot right now, without closing MO2. Runs in
  the background (the toolbar icon shows a spinner while it's working); a popup shows the
  result (snapshot ID + changelog summary) when it's done. Instance folder, active profile,
  and real game path are all read live from MO2 itself on every click, so there's nothing
  else to keep in sync or let go stale.
- **Open Vault** - launches the full `mo2-modlist-vault` desktop app (e.g. to browse snapshot
  history, or to restore). Restoring still requires MO2 to be closed first - the desktop
  app's own "Close MO2 Processes" button handles that.

## What this plugin deliberately does *not* do

Restore. Restoring writes directly into the live instance folder and needs MO2 (and its
helper processes) closed first - not something safe to trigger from inside MO2 itself. Use
the standalone `mo2-modlist-vault` GUI/CLI for restoring; this plugin's only job is one-click
snapshot creation while you're actively modding.
