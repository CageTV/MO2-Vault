# Project State / Continuity Rundown

Paste this whole file into a new conversation if chat history is lost and you need to
add/update the app. It's written to stand alone - no prior context assumed.

## What this is

Two related projects, built together, both personal/for-hire tools around Mod Organizer 2:

1. **`mo2-modlist-vault`** (`H:\Claud Work Space\mo2-modlist-vault`) - the core tool. Backs up
   an MO2 modlist as a small recipe (not a full copy), restores it onto a fresh machine by
   redownloading from Nexus and reconstructing mods via hash-matched recipes, with zero
   FOMOD/installer interaction. Also has a "vault" mode: repeated, deduplicated, Time
   Machine-style snapshots instead of one-off backups. Ships as a dual-mode exe (GUI when
   double-clicked, CLI when given arguments).
2. **`MO2 Vault Integration`** (`H:\Claud Work Space\MO2 Vault Integration`) - a companion MO2
   plugin (`mobase.IPluginTool`). Adds a toolbar button inside MO2 for one-click vault
   snapshots ("Create Restore Point") without leaving MO2, plus an "Open Vault" button that
   launches the standalone app. It shells out to `mo2-modlist-vault.exe`'s CLI rather than
   embedding any of the tool's own logic/dependencies inside MO2's Python environment.

The user's real production modlist is **3000+ mods, ~500GB**; the ~443-mod `Tes_list`
Skyrim SE instance and a small ~6GB `FO4Horizon` Fallout 4 instance (both at
`H:\Claud Work Space\`) are the dev/test lists actually used to validate this tool. Design
choices (no size caps, dedup strategy, etc.) are made with the 3000-mod target in mind even
though testing happens on the smaller lists.

## Why it exists (design intent)

The user explicitly does not want a Wabbajack-style "install and pick every FOMOD choice
yourself" experience - the whole point is that restoring never shows an installer or a FOMOD
dialog. This shaped the core architecture: **recipe-based reconstruction**. At backup time,
every mod's installed files are hash-matched (SHA1) against the archive that installed them
(from `downloads/`), producing a small "recipe" (which archive-internal paths map to which
installed paths). At restore time, the recipe is replayed directly - extract archive, copy
matched files - with no installer ever running.

## Architecture map (`mo2-modlist-vault/modlist_vault/`)

- **`mo2_instance.py`** - `Mo2Instance` model: instance root, profile resolution (with
  `create_if_missing` for fresh restore targets), `downloads_dir` (reads
  `[Settings] download_directory` from `ModOrganizer.ini`, handles `%BASE_DIR%` and BOM).
- **`install_recipe.py`** - the hash-matching core. `compute_install_recipe()` (archive vs.
  installed mod folder) and `compute_folder_recipe()` (real game Data folder vs. installed
  mod folder, for Creation-Club-style content) share matching logic via `_match_against_index()`.
  `apply_install_recipe()`/`apply_folder_recipe()` replay a recipe at restore time.
- **`backup.py`** - `build_manifest()` walks `modlist.txt`, classifies each mod
  (`MODE_ARCHIVE_RECIPE` / `MODE_BUNDLED_ARCHIVE` / `MODE_BUNDLED_FOLDER` / `MODE_GAME_CONTENT`),
  computes recipes. `_prepare_backup()` (shared with `vault.py`) builds the manifest +
  extra-files list without writing anything; `create_backup()` adds zip-writing on top. No
  size cap on bundled (no-redownload-source) content by design - real custom mods/tool
  outputs can be 1-10GB+.
- **`mo2_setup.py`** - extracts the stock MO2 release, overlays customizations
  (`find_extra_or_modified_files()` diffs the instance against the stock release hash-by-hash),
  rewrites `ModOrganizer.ini` paths (`rewrite_ini_paths()` - literal substring substitution,
  not a full ini parse, since most of the file is opaque Qt binary blobs), builds a local
  "Stock Game" copy if requested (`create_stock_game_copy()`), reconstructs `tools/<name>`
  entries from their recipes.
- **`restore.py`** - `restore()` downloads/reuses archives and calls `_reconstruct_mod()`
  per mod; oversized/unreconstructable mods get an empty placeholder slot (load order
  preserved) rather than being silently dropped. `finalize_restore()` matches plugin order
  after MO2's first launch.
- **`vault.py`** - content-addressed snapshot store: `blobs/<sha1[:2]>/<sha1>` shared across
  all snapshots, each snapshot a full (not delta) manifest + `content_index.json` +
  `changelog.json`. `create_vault_snapshot()` reuses `_prepare_backup()`, hashes every content
  item, writes new blobs only if the hash doesn't already exist, computes a changelog against
  the vault's latest snapshot. `materialize_snapshot()` turns any snapshot back into a normal
  backup zip, fully consumable by unmodified `setup_mo2()`/`restore()`.
- **`nexus_api.py`** - `find_current_file_id()` follows Nexus's update chain, falls back to
  exact-filename match (preferring `category_name == "MAIN"`).
- **`tool_sources.py`** - matches `tools/<name>` folders to `downloads/*.meta` files, including
  multi-archive tools (e.g. DynDOLOD's TexGen + main components).
- **`util.py`** - `safe_copy2`/`safe_copytree` wrap `shutil` calls so a `PermissionError`
  names the exact locked file and suggests closing MO2/helper processes/antivirus.
- **`process_utils.py`** - `kill_mo2_processes()`, a manual (button-triggered, never automatic)
  `taskkill` of MO2 and known helper processes (`usvfs_proxy_*.exe`, `nxmhandler.exe`, etc.).
- **`cli.py`** - argparse entry point, one `cmd_*` function per subcommand:
  `backup`, `setup-mo2`, `restore`, `finalize-restore`, `vault-snapshot`, `vault-list`,
  `vault-materialize`.
- **`gui/`** - PySide6 desktop app.
  - `settings.py` - **plain JSON file** at `%LOCALAPPDATA%\mo2-modlist-vault\settings.json`,
    not `QSettings`/registry - deliberately, so a distributed copy of the tool starts clean on
    someone else's machine (no leaked paths/API key) and can be reset via `clear_all()` /
    "Clear All Settings..." in the GUI.
  - `workers.py` - `_BaseWorker(QThread)` pattern per long-running action; `VaultRestoreToPointWorker`
    chains materialize -> setup_mo2 -> restore in one call for the Vault tab's one-click restore.
  - `main_window.py` - four tabs (Backup/Restore/Finalize/Vault) + shared Nexus bar + persistent
    log dock. Each tab's fields are independent `Settings` keys (`backup/*`, `restore/*`,
    `vault/*`) - see "Known gotchas" below for why this caused real bugs.
- **`launcher.py`** / **`run.py`** - dual-mode entry point (`launcher.py`: no args -> GUI, args
  -> CLI; `run.py`: top-level PyInstaller entry script that imports `launcher.py` so its
  relative imports keep working under a frozen build).
- **`build_exe.spec`** - PyInstaller **onedir** build. `console=True` deliberately (not
  `console=False`) - this exe is dual-mode, and a windowed-subsystem build leaves
  `sys.stdout`/`stderr` broken for the CLI path (the MO2 plugin's `subprocess` capture and any
  terminal CLI use both need a real console; `CREATE_NO_WINDOW` on the *caller* side hides the
  console flash for automated invocations instead). Explicit `hiddenimports` for py7zr's
  dynamically-dispatched codec modules - note the import name often isn't the PyPI package
  name (`pybcj` -> `bcj`, `pycryptodomex` -> `Cryptodome`; verified by grepping py7zr's own
  source, not guessed).

## `MO2 Vault Integration/` (the plugin project)

- `__init__.py` - `createPlugins()` factory (MO2's plugin loading convention).
- `vault_restore_point.py` - `VaultRestorePointPlugin(mobase.IPluginTool)`. Modeled directly
  on a real, working reference plugin the user provided (`ESLifier MO2 Integration`, seen at
  `C:\Modlists\VTS\plugins\ESLifier MO2 Integration\`): same toolbar-button-injection pattern,
  same "shell out to an external exe rather than embed logic" philosophy, same single-folder
  `PluginSetting` pattern.
  - Two settings only: **Tool Folder** (where `mo2-modlist-vault.exe` lives) and **Vault
    Folder**. Both stored via `mobase.PluginSetting`, which MO2 persists *inside that
    instance's own* `ModOrganizer.ini` under `[Plugins]` - confirmed by inspecting a real
    instance's ini. This is deliberately per-instance with zero extra plumbing.
  - Everything else (instance root, active profile, real game path) is asked **live** from
    `organizer` on every click (`organizer.basePath()`, `organizer.profileName()`,
    `organizer.managedGame().gameDirectory()`), never cached - so it can't go stale the way
    the standalone GUI's cross-tab fields did (see gotcha below).
  - `SnapshotWorker(QObject)` runs `subprocess.run([exe, "vault-snapshot", ...],
    capture_output=True, creationflags=CREATE_NO_WINDOW)` on a `QThread`, so MO2's UI thread
    never blocks. Safe to run with MO2 open - snapshotting only reads/hashes files, it never
    writes into the live instance (restoring is what needs MO2 closed, and this plugin
    intentionally never restores).
  - Toolbar icon cycles through 8 generated spinner-ring PNG frames
    (`icons/vault_icon_spin_0..7.png`, made with Pillow, amber ring with a fading "moving
    head" - not the padlock icon itself rotated, which would just look upside-down/broken for
    a run that can take minutes) while a snapshot is running.
  - "Open Vault" button: `os.startfile(exe_path)` with no args, opening the full GUI - mirrors
    ESLifier's own "Start ESLifier" button.
- Installed for live testing at `H:\Claud Work Space\Tes_list\plugins\MO2 Vault Integration\`
  (copy the two `.py` files + `icons/` there again after any edit here - MO2 only reloads
  plugins on its own restart, and a plain file copy while MO2 is running is safe, it just
  won't take effect until MO2 is relaunched).

## Known gotchas / bugs already found and fixed this session

Useful context if something breaks again in a similar shape:

- **`.git` folders inside `tools/`** (e.g. zEdit's `unifiedPatchingFramework` submodule) used
  to get captured as "extra files" and could fail to restore with a Windows permission error
  on the submodule's `.git` gitlink file. Fixed by excluding `.git`/`.svn`/`.hg` at both
  capture time (`mo2_setup.py`'s `IGNORE_PATTERNS`) and apply time (defensive skip, for
  backups already taken under the old code).
- **`gamePath` not restored correctly from a vault snapshot** - the Vault tab's "Restore to
  This Point" was silently depending on the *Restore tab's* "Create local Stock Game copy"
  checkbox, which you'd have no reason to have set while working from the Vault tab. Fixed by
  reading `game_copy_folder_name` off the snapshot's own manifest and auto-forcing the flag
  when the source instance actually had a local Stock Game copy - never depend on a checkbox
  living on a different tab for something the manifest already knows.
- **Stale cross-tab fields causing a wrong MO2 archive to be used** - "Restore to This Point"
  originally fell back to the *Restore tab's* fields when the Vault tab's were empty; but
  after running an unrelated restore for a *different* modlist, those fields held that other
  list's values (not empty, just wrong), and got silently reused. Fixed by flipping priority:
  the Vault tab's own Instance/MO2-archive/Real-Game-Path fields win when set, Restore tab is
  only the fallback for genuinely empty fields. **General lesson: any field that's "shared" or
  "reused" across tabs/workflows for convenience is a latent staleness bug - prefer deriving
  values live from an authoritative source (a manifest, MO2's own `organizer`) over caching a
  copy in a different tab/settings key.**
- **PyInstaller onedir build initially missing `bcj`/`Cryptodome` hidden imports** - PyInstaller
  couldn't resolve `pybcj`/`pycryptodomex` as hidden import *names* because those are PyPI
  package names, not the importable module names (`bcj`, `Cryptodome` respectively) - found by
  grepping py7zr's actual source for its import statements rather than guessing.
- **`console=False` would have broken the CLI path** - caught before it shipped: a
  windowed-subsystem PyInstaller build has no usable `sys.stdout`/`stderr` unless a console is
  attached, which breaks both terminal CLI use and the MO2 plugin's `subprocess` output
  capture. Went with `console=True` + `CREATE_NO_WINDOW` on the caller side instead.
- **A "stuck" restore point was not actually stuck** - diagnosed via `Get-CimInstance
  Win32_Process`/UI Automation rather than guessing: the headless `vault-snapshot` subprocess
  was alive and actively burning CPU, just slow (see Known follow-ups below). Worth checking
  Task Manager / process list before assuming a hang.

## Testing status (as of this writing)

- Full backup/restore/finalize cycle validated end-to-end on `Tes_list` (Skyrim SE, ~443
  mods): exact modlist.txt reproduction, all tools reconstructed, Stock Game copy + CK
  Platform Extended overlay working, mod/separator colors preserved.
- Full backup/restore cycle validated on `FO4Horizon` (Fallout 4, ~6GB, small list) onto a
  fresh drive - confirms the tool is genuinely game-agnostic, not just Skyrim-tuned.
  Vault snapshot/changelog/materialize validated against real vault data (deduplication
  confirmed: 0 new blobs on an unchanged re-snapshot).
- `mo2-modlist-vault.exe` (packaged build) smoke-tested: CLI mode (`vault-list` against a real
  vault) and GUI mode both confirmed working, including settings.json loading correctly
  (verified via Windows UI Automation reading actual field values, not just visual
  inspection).
- `MO2 Vault Integration` plugin: syntax-checked only at write time (`mobase`/PyQt aren't
  importable outside MO2's own embedded Python, so no local import test is possible). Live
  end-to-end testing in real MO2 is **in progress with the user** as of this writing - confirm
  current status before assuming it fully works. First live click did successfully launch the
  correct headless subprocess with correct live-derived arguments (confirmed via process
  inspection), but the completion dialog / full round-trip hadn't been confirmed yet as of the
  last exchange.

## Known follow-ups (not yet implemented)

- **Snapshot performance**: `create_vault_snapshot()` re-hashes every content item on every
  run, even unchanged ones - dedup happens *after* hashing, not before. Fine at ~443 mods,
  will not scale comfortably to the real 3000+ mod/~500GB target. The standard fix (used by
  git/rsync/Time Machine) is a stat-based shortcut: skip re-hashing a file if its
  (size, mtime) match what's already recorded for it in the vault's latest snapshot, and just
  reuse the previously-recorded blob hash. Proposed to the user, not yet implemented as of
  this writing - check whether it's since been done before re-proposing it.
- **Vault blob pruning**: nothing garbage-collects blobs no longer referenced by any snapshot.
  Explicitly deferred as a "phase 2" feature when it becomes relevant.
- **No automated/scheduled snapshotting** - manual "Take Snapshot" / "Create Restore Point"
  only, by design so far.

## User preferences to respect

- Explicitly does **not** want an install-wizard/FOMOD-choice-driven restore flow - this is
  the core reason the recipe-based architecture exists at all.
- Chose **PySide6** over Tkinter for the GUI, explicitly.
- Chose to **plan properly first** (not "rough it in") for the vault feature - use plan mode /
  get sign-off before large architectural additions, as was done for the vault system and for
  the exe-launcher + MO2-plugin work.
- Cares about **distributability**: settings must never leak personal paths/API keys to
  someone else who receives a copy of the tool (drove the JSON-not-registry settings design),
  and wants a "reset" path that doesn't require digging through the registry.
- Prefers **manual, explicit, button-triggered** destructive/impactful actions (closing MO2
  processes, clearing settings) over anything automatic/silent.
- Real target scale is 3000+ mods / ~500GB - keep this in mind when evaluating whether an
  approach that "works fine" on the ~443-mod dev list will actually hold up.

## If you're picking this up cold

1. Read this file, then skim `README.md` and `QUICKSTART.md` for the user-facing shape.
2. Check `git status`/recent file mtimes in both project folders for anything in flight.
3. Ask the user what's currently being tested/blocked before assuming a clean slate - the
   MO2 plugin's live testing was likely still in progress when this file was written.
