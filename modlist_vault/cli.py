import argparse
import os
import sys
from pathlib import Path

from .backup import DEFAULT_MAX_BUNDLE_SIZE_BYTES, DEFAULT_VANILLA_PATTERNS, create_backup
from .mo2_instance import InvalidInstanceError, open_instance
from .restore import finalize_restore, restore as run_restore
from .util import configure_logging, logger


def _print_progress(current: int, total: int, message: str) -> None:
    print(f"[{current}/{total}] {message}")


def cmd_backup(args: argparse.Namespace) -> int:
    instance = open_instance(Path(args.instance))
    vanilla_patterns = [p.strip() for p in args.vanilla_pattern.split(",") if p.strip()] if args.vanilla_pattern else DEFAULT_VANILLA_PATTERNS
    manifest = create_backup(
        instance, Path(args.output), profile_name=args.profile,
        max_bundle_size_bytes=args.max_bundle_mb * 1024 * 1024 if args.max_bundle_mb else None,
        vanilla_patterns=vanilla_patterns,
        mo2_stock_archive=Path(args.mo2_stock_archive) if args.mo2_stock_archive else None,
        real_game_path=Path(args.real_game_path) if args.real_game_path else None,
    )

    from .backup import MODE_BUNDLED_ARCHIVE, MODE_BUNDLED_FOLDER, MODE_DOWNLOAD, MODE_GAME_CONTENT, MODE_UNKNOWN
    print(f"\nBacked up profile '{manifest.profile_name}' to {args.output}")
    print(f"  {len(manifest.mods_with_mode(MODE_DOWNLOAD))} mod(s) will be re-downloaded on restore")

    bundled_archives = manifest.mods_with_mode(MODE_BUNDLED_ARCHIVE)
    bundled_folders = manifest.mods_with_mode(MODE_BUNDLED_FOLDER)
    print(f"  {len(bundled_archives)} mod(s) had no known source - bundled their archive")
    print(f"  {len(bundled_folders)} mod(s) had no archive either - bundled their installed files")

    game_content = manifest.mods_with_mode(MODE_GAME_CONTENT)
    if game_content:
        total_gb = sum(m.size_bytes for m in game_content) / (1024 ** 3)
        with_recipe = [m for m in game_content if m.recipe]
        without_recipe = [m for m in game_content if not m.recipe]
        print(f"\n  {len(game_content)} mod(s) matched vanilla-content patterns ({total_gb:.1f} GB) and were "
              f"NOT bundled raw:")
        if with_recipe:
            print(f"    - reconstructable via recipe from your real game install (pass --game-path to "
                  f"'restore'): {', '.join(m.name for m in with_recipe)}")
        if without_recipe:
            print(f"    - no recipe captured (pass --real-game-path to 'backup' next time), or Steam "
                  f"provides these directly: {', '.join(m.name for m in without_recipe)}")

    oversized = [m for m in bundled_archives + bundled_folders if not m.bundled]
    if oversized:
        print(f"\n  {len(oversized)} mod(s) exceeded the {args.max_bundle_mb} MB cap and were NOT bundled "
              f"(you'll need to source these yourself):")
        for m in sorted(oversized, key=lambda m: -m.size_bytes):
            print(f"    - {m.name} ({m.size_bytes / (1024 * 1024):.0f} MB)")

    unknown = manifest.mods_with_mode(MODE_UNKNOWN)
    if unknown:
        print(f"\n  {len(unknown)} mod(s) were listed but had no folder on disk and were SKIPPED: "
              f"{', '.join(m.name for m in unknown)}")

    if manifest.has_mo2_ini:
        print(f"\n  Captured ModOrganizer.ini for later path rewriting via 'setup-mo2'.")
    if manifest.extra_files:
        print(f"  Captured {len(manifest.extra_files)} file(s) new or modified vs. the stock MO2 release "
              f"(plugins, tools/, splash.png, patched exe, etc).")
    elif args.mo2_stock_archive:
        print("  No extra/modified files found beyond the stock MO2 release.")
    if manifest.stock_game_extra_files:
        print(f"  Captured {len(manifest.stock_game_extra_files)} file(s) dropped into the local Stock Game "
              f"copy on top of the real game install: {', '.join(manifest.stock_game_extra_files)}")
    elif args.real_game_path:
        print("  No extra/modified files found in the local Stock Game copy beyond the real game install.")
    if manifest.profile_extra_files:
        print(f"  Captured {len(manifest.profile_extra_files)} profile file(s) beyond modlist.txt/plugins.txt "
              f"(settings.ini, skyrim*.ini, load order/grouping, BethINI cache, etc; saves/ excluded).")
    return 0


def cmd_setup_mo2(args: argparse.Namespace) -> int:
    from . import mo2_setup

    api_key = args.nexus_api_key or os.environ.get("NEXUS_API_KEY")
    result = mo2_setup.setup_mo2(
        archive_path=Path(args.archive) if args.archive else None,
        target_root=Path(args.target),
        backup_zip=Path(args.backup),
        new_game_path=args.game_path,
        nexus_api_key=api_key,
        game_domain=args.game_domain,
        create_local_game_copy=args.create_stock_game_copy,
        existing_downloads_dir=Path(args.downloads_source) if args.downloads_source else None,
    )
    print(f"\nExtracted MO2 into {args.target}")
    print(f"ModOrganizer.ini: {'written (paths rewritten for new location)' if result.ini_written else 'not found in backup - set up manually'}")
    print(f"Extra/modified files restored: {result.extra_files_restored}")
    if result.config_paths_rewritten:
        print(f"Tool/config files with absolute paths rewritten for this location: {result.config_paths_rewritten}")
    if result.stock_game_copy_created:
        print(f"Local game copy built at {result.stock_game_copy_created} (Creation Club content excluded) - "
              f"gamePath points here, {args.game_path} is untouched.")
    if result.stock_game_files_restored:
        target_desc = result.stock_game_copy_created or args.game_path
        print(f"Stock-game overlay files applied onto {target_desc}: {result.stock_game_files_restored}")
    if result.tools_reconstructed:
        print(f"Tools reconstructed from their own download source: {', '.join(result.tools_reconstructed)}")
    if result.tools_failed:
        print(f"Tools that FAILED to reconstruct (need a Nexus API key/game domain, or manual placement): {', '.join(result.tools_failed)}")
    print("\nNext: run 'restore' to rebuild your mods.")
    return 1 if result.tools_failed else 0


def cmd_restore(args: argparse.Namespace) -> int:
    instance = open_instance(Path(args.instance))
    api_key = args.nexus_api_key or os.environ.get("NEXUS_API_KEY")
    result = run_restore(
        instance,
        Path(args.backup),
        game_domain=args.game_domain,
        nexus_api_key=api_key,
        profile_name=args.profile,
        real_game_path=Path(args.game_path) if args.game_path else None,
        existing_downloads_dir=Path(args.downloads_source) if args.downloads_source else None,
        progress_callback=_print_progress,
    )

    print(f"\nFully reconstructed (no MO2 needed): {len(result.placed)}")
    print(f"Separators recreated: {len(result.separators_recreated)}")
    if result.game_content_skipped:
        print(f"Game-content mods with an empty placeholder slot (pass --game-path to reconstruct "
              f"these from your real game install): {', '.join(result.game_content_skipped)}")
    if result.placeholder_slots:
        print(f"Empty placeholder slots kept (load order preserved, no content): {len(result.placeholder_slots)}")
    if result.failed:
        print(f"\nFAILED ({len(result.failed)}):")
        for f in result.failed:
            print(f"  - {f.name}: {f.reason}")

    print(
        "\nMod order/enabled state is already written. Next: open MO2 once (just "
        "launch it - nothing to click) so it discovers plugins from your mods, "
        "then run 'finalize-restore' to match plugin order to the backup."
    )
    return 1 if result.failed else 0


def cmd_finalize_restore(args: argparse.Namespace) -> int:
    instance = open_instance(Path(args.instance))
    result = finalize_restore(instance, Path(args.backup), profile_name=args.profile)

    print(f"\nReordered {result.reordered_plugins} plugin(s).")
    if result.still_missing_plugins:
        print(f"\nMO2 hasn't discovered these plugins yet ({len(result.still_missing_plugins)}) - "
              f"open MO2 once and re-run finalize-restore:")
        for name in result.still_missing_plugins:
            print(f"  - {name}")
        return 1
    print("Plugin order matches the backup.")
    return 0


def cmd_vault_snapshot(args: argparse.Namespace) -> int:
    from . import vault

    instance = open_instance(Path(args.instance))
    vanilla_patterns = [p.strip() for p in args.vanilla_pattern.split(",") if p.strip()] if args.vanilla_pattern else DEFAULT_VANILLA_PATTERNS
    info = vault.create_vault_snapshot(
        instance, Path(args.vault), profile_name=args.profile,
        max_bundle_size_bytes=args.max_bundle_mb * 1024 * 1024 if args.max_bundle_mb else None,
        vanilla_patterns=vanilla_patterns,
        mo2_stock_archive=Path(args.mo2_stock_archive) if args.mo2_stock_archive else None,
        real_game_path=Path(args.real_game_path) if args.real_game_path else None,
    )
    print(f"\nSnapshot '{info.snapshot_id}' created in {args.vault}")
    print(f"  {info.changelog.summary() if info.changelog else 'No changes'}")
    print(f"  New content stored: {info.changelog.new_blob_count if info.changelog else 0} file(s), "
          f"{(info.changelog.new_blob_bytes if info.changelog else 0) / (1024 * 1024):.1f} MB")
    return 0


def cmd_vault_list(args: argparse.Namespace) -> int:
    from . import vault

    infos = vault.list_snapshots(Path(args.vault))
    if not infos:
        print(f"No snapshots found in {args.vault}")
        return 0
    print(f"\n{len(infos)} snapshot(s) in {args.vault}:\n")
    for info in infos:
        summary = info.changelog.summary() if info.changelog else "Initial snapshot"
        print(f"  {info.snapshot_id}  -  {summary}")
    return 0


def cmd_vault_materialize(args: argparse.Namespace) -> int:
    from . import vault

    vault.materialize_snapshot(Path(args.vault), args.snapshot, Path(args.output))
    print(f"\nMaterialized snapshot '{args.snapshot}' to {args.output}")
    print("Next: run 'setup-mo2' and 'restore' against it exactly like any other backup .zip.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modlist-vault", description=(
        "Back up and restore an MO2 modlist without a 400GB zip file. Works "
        "alongside Download Manager+ (which does the actual installing) and "
        "Remember Installation Choices (which pre-fills FOMOD dialogs) - this "
        "tool never touches MO2 while it's running."
    ))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="Snapshot an MO2 instance's modlist into a portable backup file.")
    p_backup.add_argument("--instance", required=True, help="Path to the MO2 instance folder (contains profiles/, mods/, downloads/).")
    p_backup.add_argument("--profile", help="Profile name (default: auto-detect if only one exists).")
    p_backup.add_argument("--output", required=True, help="Where to write the backup .zip.")
    p_backup.add_argument(
        "--max-bundle-mb", type=int, default=None,
        help="Skip bundling any single mod's files/archive larger than this (MB) when it has no "
             "redownload source - keeps its modlist.txt slot, but you handle it manually. "
             "Default: no cap - always bundle raw regardless of size.",
    )
    p_backup.add_argument(
        "--vanilla-pattern", default=",".join(DEFAULT_VANILLA_PATTERNS),
        help="Comma-separated, case-insensitive substrings - any mod whose name contains one is treated as "
             "vanilla/Steam-delivered content and excluded entirely (not bundled, not flagged as missing). "
             f"Default: '{','.join(DEFAULT_VANILLA_PATTERNS)}'.",
    )
    p_backup.add_argument(
        "--mo2-stock-archive",
        help="Path to the official portable MO2 .7z you downloaded - if given, captures ModOrganizer.ini "
             "(for later path rewriting) and any plugin files not part of that stock release (your "
             "third-party plugins, e.g. Download Manager+, Remember Installation Choices). Requires py7zr.",
    )
    p_backup.add_argument(
        "--real-game-path",
        help="Path to your actual Steam/GOG game install - only needed if ModOrganizer.ini's gamePath points "
             "at a local copy inside the instance (a 'Stock Game' folder) rather than the real install. If "
             "given, captures any files dropped into that local copy on top of what Steam/GOG installed there "
             "(e.g. a Creation Kit patcher like CK Platform Extended), so setup-mo2 can reapply them.",
    )
    p_backup.set_defaults(func=cmd_backup)

    p_setup = sub.add_parser("setup-mo2", help="Extract MO2 itself into a fresh target instance, using a backup's captured ini/extra files/tools.")
    p_setup.add_argument("--archive", help="Path to the official portable MO2 .7z release. Optional if the backup recorded a redownload URL for it (from MO2's own self-updater .meta) - then it's fetched automatically.")
    p_setup.add_argument("--target", required=True, help="Where to extract MO2 to (the new instance folder).")
    p_setup.add_argument("--backup", required=True, help="Path to a backup .zip created with --mo2-stock-archive.")
    p_setup.add_argument("--game-path", required=True, help="Path to your actual game install (e.g. the Steam Skyrim SE folder). By default gamePath points straight here; with --create-stock-game-copy, this is instead the source copied FROM.")
    p_setup.add_argument("--game-domain", help="Nexus game domain slug (e.g. skyrimspecialedition) - needed to reconstruct any tools/ entries with a Nexus source.")
    p_setup.add_argument("--nexus-api-key", help="Nexus Premium API key (or set NEXUS_API_KEY env var) - needed to reconstruct any tools/ entries with a Nexus source.")
    p_setup.add_argument(
        "--create-stock-game-copy", action="store_true",
        help="Build a local game copy inside the instance (e.g. 'Stock Game') from --game-path instead of "
             "pointing gamePath straight at it - keeps MO2/mods from ever touching your real Steam/GOG "
             "install, and insulates the modlist from a later Steam update changing files mid-project. "
             "Creation Club content is excluded (it's tracked as its own mod); any captured stock-game "
             "overlay files (e.g. a Creation Kit patcher) are applied into the copy. Takes extra disk space.",
    )
    p_setup.add_argument(
        "--downloads-source",
        help="Path to an existing folder of already-downloaded archives (e.g. a prior MO2/Wabbajack install's "
             "downloads/ folder, or a shared archive cache) - checked by exact filename before downloading any "
             "tool component from Nexus, copying it in instead of re-fetching when found.",
    )
    p_setup.set_defaults(func=cmd_setup_mo2)

    p_restore = sub.add_parser("restore", help="Rebuild mods into a (usually fresh) MO2 instance - downloads/bundles archives and reconstructs each mod's files directly, no MO2 install step needed.")
    p_restore.add_argument("--instance", required=True, help="Path to the TARGET MO2 instance folder.")
    p_restore.add_argument("--backup", required=True, help="Path to the backup .zip.")
    p_restore.add_argument("--profile", help="Target profile name (default: same name as in the backup).")
    p_restore.add_argument("--game-domain", help="Nexus game domain slug (e.g. skyrimspecialedition). Usually auto-detected from the backup; only needed if that fails.")
    p_restore.add_argument("--nexus-api-key", help="Nexus Premium API key (or set NEXUS_API_KEY env var).")
    p_restore.add_argument(
        "--game-path",
        help="Path to your actual game install (same as setup-mo2's --game-path). Optional - only needed to "
             "fully reconstruct game-content mods (e.g. 'Creation Club Files') that were captured with a "
             "recipe at backup time; without it, they still get an empty placeholder slot (load order kept, "
             "no content).",
    )
    p_restore.add_argument(
        "--downloads-source",
        help="Path to an existing folder of already-downloaded archives (e.g. a prior MO2/Wabbajack install's "
             "downloads/ folder, or a shared archive cache) - checked by exact filename before downloading any "
             "mod from Nexus, copying it in instead of re-fetching when found.",
    )
    p_restore.set_defaults(func=cmd_restore)

    p_finalize = sub.add_parser("finalize-restore", help="Run after opening MO2 once post-restore, to match plugin (esp/esm/esl) order to the backup.")
    p_finalize.add_argument("--instance", required=True, help="Path to the TARGET MO2 instance folder.")
    p_finalize.add_argument("--backup", required=True, help="Path to the same backup .zip used in restore.")
    p_finalize.add_argument("--profile", help="Target profile name (default: same name as in the backup).")
    p_finalize.set_defaults(func=cmd_finalize_restore)

    p_vault_snapshot = sub.add_parser(
        "vault-snapshot",
        help="Take a snapshot into a vault - like 'backup', but content identical to an earlier "
             "snapshot in the same vault is stored once, not re-bundled every time.",
    )
    p_vault_snapshot.add_argument("--instance", required=True, help="Path to the MO2 instance folder.")
    p_vault_snapshot.add_argument("--profile", help="Profile name (default: auto-detect if only one exists).")
    p_vault_snapshot.add_argument("--vault", required=True, help="Path to the vault folder (created if it doesn't exist).")
    p_vault_snapshot.add_argument(
        "--max-bundle-mb", type=int, default=None,
        help="Same as 'backup' --max-bundle-mb. Default: no cap.",
    )
    p_vault_snapshot.add_argument(
        "--vanilla-pattern", default=",".join(DEFAULT_VANILLA_PATTERNS),
        help=f"Same as 'backup' --vanilla-pattern. Default: '{','.join(DEFAULT_VANILLA_PATTERNS)}'.",
    )
    p_vault_snapshot.add_argument("--mo2-stock-archive", help="Same as 'backup' --mo2-stock-archive.")
    p_vault_snapshot.add_argument("--real-game-path", help="Same as 'backup' --real-game-path.")
    p_vault_snapshot.set_defaults(func=cmd_vault_snapshot)

    p_vault_list = sub.add_parser("vault-list", help="List a vault's snapshots with a one-line changelog summary each.")
    p_vault_list.add_argument("--vault", required=True, help="Path to the vault folder.")
    p_vault_list.set_defaults(func=cmd_vault_list)

    p_vault_materialize = sub.add_parser(
        "vault-materialize",
        help="Extract one vault snapshot into a normal standalone backup .zip - "
             "then 'setup-mo2'/'restore'/'finalize-restore' work on it unchanged.",
    )
    p_vault_materialize.add_argument("--vault", required=True, help="Path to the vault folder.")
    p_vault_materialize.add_argument("--snapshot", required=True, help="Snapshot ID (see 'vault-list').")
    p_vault_materialize.add_argument("--output", required=True, help="Where to write the extracted backup .zip.")
    p_vault_materialize.set_defaults(func=cmd_vault_materialize)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose)

    try:
        return args.func(args)
    except InvalidInstanceError as e:
        logger.error(str(e))
        return 2
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        if args.verbose:
            raise
        return 2


if __name__ == "__main__":
    sys.exit(main())
