"""Interactive CLI to restore a Minecraft server backup chain (full + incrementals).

This is a thin wrapper over :mod:`utils.restore_core` (shared with the bot's
``/restore`` command): it adds an interactive curses selector and safety prompts,
then delegates the actual scan/merge/restore to ``restore_core``.

The Minecraft server MUST be stopped before restoring in-place — the bot's
``/restore`` command stops and restarts it for you, but this standalone tool does
not, so it exists mainly for offline/disaster recovery when the bot isn't running.

Usage:
    python restore.py                        # pick a server from diamondsign.json
    python restore.py --server <name>        # skip the picker (name/key)
    python restore.py --dry-run
    python restore.py --backup-dir P --target-dir Q   # files-only, no chain reset
"""

import argparse
import curses
import sys
from pathlib import Path

from utils import restore_core
from utils.config import load_config, backup_exclude_names, ConfigError


def _build_display(chains: list) -> tuple:
    """Build (display_lines, points) for the curses selector, latest-first.

    display_lines: list of (is_selectable, text, point_idx). points: flat list of
    (chain_idx, point_idx). Mirrors restore_core.list_restore_points ordering.
    """
    points = []
    display_lines = []
    num = 1
    for rev_idx, ci in enumerate(reversed(range(len(chains)))):
        chain = chains[ci]
        full = chain["full"]
        chain_label = f"Chain {chain['chain_id']}" if chain["chain_id"] else "Standalone"
        if rev_idx > 0:
            display_lines.append((False, "", None))
        display_lines.append(
            (False, f"  {chain_label} (from {full['path'].name})", None))
        for ii in reversed(range(len(chain["incrementals"]))):
            incr = chain["incrementals"][ii]
            label = (f"  {num:3d}.  [INCR] {restore_core.parse_timestamp(incr['timestamp'])}"
                     f"  ({restore_core.format_size(incr['size'])})")
            display_lines.append((True, label, len(points)))
            points.append((ci, ii))
            num += 1
        label = (f"  {num:3d}.  [FULL] {restore_core.parse_timestamp(full['timestamp'])}"
                 f"  ({restore_core.format_size(full['size'])})")
        display_lines.append((True, label, len(points)))
        points.append((ci, -1))
        num += 1
    return display_lines, points


def _curses_select(stdscr, header_text: str, display_lines: list,
                   selectable_indices: list) -> int | None:
    """Interactive paginated selector with highlight. Returns the chosen
    point_idx (index into the points list), or None if cancelled."""
    curses.curs_set(0)
    curses.use_default_colors()
    header_lines = header_text.splitlines() if header_text else []

    def recalc():
        my, mx = stdscr.getmaxyx()
        ps = max(3, my - len(header_lines) - 2)
        tp = max(1, (len(display_lines) + ps - 1) // ps)
        return my, mx, ps, tp

    max_y, max_x, page_size, total_pages = recalc()
    hi = 0 if selectable_indices else -1
    page = (selectable_indices[0] // page_size) if selectable_indices else 0
    number_buf = ""

    def first_sel_on_page(p):
        lo, hi_bound = p * page_size, (p + 1) * page_size
        for i, di in enumerate(selectable_indices):
            if lo <= di < hi_bound:
                return i
        return None

    while True:
        stdscr.erase()
        row = 0
        for hl in header_lines:
            if row >= max_y:
                break
            try:
                stdscr.addnstr(row, 0, hl, max_x - 1)
            except curses.error:
                pass
            row += 1
        start = page * page_size
        end = min(start + page_size, len(display_lines))
        for di in range(start, end):
            if row >= max_y - 1:
                break
            _sel, text, _pidx = display_lines[di]
            try:
                if _sel and hi >= 0 and di == selectable_indices[hi]:
                    stdscr.addnstr(row, 0, text.ljust(max_x - 1),
                                   max_x - 1, curses.A_REVERSE)
                else:
                    stdscr.addnstr(row, 0, text, max_x - 1)
            except curses.error:
                pass
            row += 1
        is_last = end >= len(display_lines)
        nav = []
        if selectable_indices:
            nav.append("Up/Dn highlight")
        if page > 0:
            nav.append("Left/p prev")
        if not is_last:
            nav.append("Right/n next")
        nav += ["Enter select", "# jump", "q quit"]
        page_str = f"Page {page + 1}/{total_pages}"
        nav_str = "  ".join(nav)
        status = (f"  {page_str}  |  Select: {number_buf}_  ({nav_str})"
                  if number_buf else f"  {page_str}  |  {nav_str}")
        try:
            stdscr.addnstr(max_y - 1, 0, status, max_x - 1, curses.A_BOLD)
        except curses.error:
            pass
        stdscr.refresh()
        key = stdscr.getch()

        if key == ord('q'):
            return None
        elif key == curses.KEY_UP:
            number_buf = ""
            if selectable_indices and hi > 0:
                hi -= 1
                page = selectable_indices[hi] // page_size
        elif key == curses.KEY_DOWN:
            number_buf = ""
            if selectable_indices and hi < len(selectable_indices) - 1:
                hi += 1
                page = selectable_indices[hi] // page_size
        elif key in (curses.KEY_RIGHT, ord('n'), ord('N')):
            number_buf = ""
            if page < total_pages - 1:
                page += 1
                fp = first_sel_on_page(page) if selectable_indices else None
                if fp is not None:
                    hi = fp
        elif key in (curses.KEY_LEFT, ord('p'), ord('P'), ord('b'), ord('B')):
            number_buf = ""
            if page > 0:
                page -= 1
                fp = first_sel_on_page(page) if selectable_indices else None
                if fp is not None:
                    hi = fp
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
            if number_buf:
                try:
                    n = int(number_buf) - 1
                    if 0 <= n < len(selectable_indices):
                        return display_lines[selectable_indices[n]][2]
                except ValueError:
                    pass
                number_buf = ""
            elif selectable_indices and hi >= 0:
                return display_lines[selectable_indices[hi]][2]
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            number_buf = number_buf[:-1]
        elif key == 27:
            number_buf = ""
        elif 48 <= key <= 57:
            number_buf += chr(key)
        elif key == curses.KEY_RESIZE:
            max_y, max_x, page_size, total_pages = recalc()
            page = min(page, total_pages - 1)


def display_restore_points(chains: list, header: str | None = None) -> tuple:
    """Show restore points with curses; return (points, selected_idx|None)."""
    display_lines, points = _build_display(chains)
    selectable = [i for i, (is_sel, _, _) in enumerate(display_lines) if is_sel]

    def _run(stdscr):
        return _curses_select(stdscr, header or "", display_lines, selectable)

    try:
        selected = curses.wrapper(_run)
    except KeyboardInterrupt:
        selected = None
    return points, selected


def _select_server(app, server_arg):
    """Return the chosen ServerConfig from diamondsign.json.

    With ``--server <name/key>`` it's resolved directly; otherwise the single
    server is used, or the servers are listed numbered and the user picks one."""
    servers = app.all_servers()
    if not servers:
        print("No servers configured in diamondsign.json.", file=sys.stderr)
        sys.exit(1)
    if server_arg:
        server = next((s for s in servers
                       if server_arg in (s.name, s.key)), None)
        if server is None:
            names = ", ".join(s.name for s in servers)
            print(f"Unknown server '{server_arg}'. Configured: {names}",
                  file=sys.stderr)
            sys.exit(1)
        return server
    if len(servers) == 1:
        return servers[0]
    print("Servers in diamondsign.json:")
    for i, s in enumerate(servers, 1):
        print(f"  {i}. {s.name}   [{s.edition}]   ({s.minecraft_dir})")
    try:
        raw = input(f"Choose a server [1-{len(servers)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(1)
    try:
        idx = int(raw)
        if not (1 <= idx <= len(servers)):
            raise ValueError
    except ValueError:
        print(f"Invalid choice: '{raw}'", file=sys.stderr)
        sys.exit(1)
    return servers[idx - 1]


def _resolve_paths(args):
    """Return (backup_dir, target_dir, manifest_path, exclude_names, copy_cmd,
    preserve_names, establish_chain, server_name).

    Files-only legacy mode (``--backup-dir`` + ``--target-dir`` with no
    ``--server``) restores an arbitrary backup dir with no config and no chain
    reset. Otherwise a server is resolved from diamondsign.json — picked
    interactively from a numbered list when ``--server`` isn't given — and its
    backup dir / server dir / manifest / excludes are used (each dir still
    overridable by the matching flag). ``server_name`` pins chain discovery to
    that server's own zips (None in files-only mode: no server context)."""
    if args.backup_dir and args.target_dir and not args.server:
        # Legacy files-only restore: no server context, so we can't safely
        # rebuild a per-server manifest/marker — just extract the chain's files.
        return (Path(args.backup_dir), Path(args.target_dir), None, set(), "",
                set(), False, None)
    try:
        app = load_config()
    except ConfigError as e:
        print(f"\n{e}\n", file=sys.stderr)
        sys.exit(1)
    server = _select_server(app, args.server)
    backup_dir = Path(args.backup_dir) if args.backup_dir else server.backup_dir
    target_dir = Path(args.target_dir) if args.target_dir else server.minecraft_dir
    exclude = backup_exclude_names(server)
    establish = target_dir.resolve() == server.minecraft_dir.resolve()
    return (backup_dir, target_dir, server.data_dir / "backup_manifest.json",
            exclude, server.backup_copy_cmd, exclude, establish,
            server.minecraft_dir.name)


def main():
    parser = argparse.ArgumentParser(
        description="Restore a Minecraft server from a full + incremental chain")
    parser.add_argument("--server", help="server name/key in diamondsign.json "
                        "(skip the interactive picker; uses its backup dir, "
                        "server dir, manifest, excludes)")
    parser.add_argument("--backup-dir", help="override the backup zip directory")
    parser.add_argument("--target-dir", help="override the restore target dir")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview without writing files")
    args = parser.parse_args()

    (backup_dir, target_dir, manifest_path, exclude, copy_cmd,
     preserve, establish, server_name) = _resolve_paths(args)

    if not backup_dir.exists():
        print(f"Error: backup directory not found: {backup_dir}", file=sys.stderr)
        sys.exit(1)

    chains = restore_core.discover_chains(backup_dir, server_name)
    if not chains:
        print("No restorable backup chains found (need at least one full backup).")
        sys.exit(1)

    header = "\n".join([
        "=" * 60, "  Minecraft Server Backup Restore Tool", "=" * 60,
        f"  Backup directory: {backup_dir}",
        f"  Target directory: {target_dir}",
        f"  Chain reset:      {'yes (in-place)' if establish else 'no (files only)'}",
        "", "  Available restore points:"])
    points, selected_idx = display_restore_points(chains, header=header)
    if selected_idx is None:
        print("Cancelled.")
        return
    chain_idx, point_idx = points[selected_idx]

    if not args.dry_run:
        print("\n" + "!" * 60)
        print("  WARNING: stop the Minecraft server before restoring in-place —")
        print("  restoring a running server corrupts the world.")
        print("!" * 60)
        if input("\n  Is the server stopped? (yes/no): ").strip().lower() != "yes":
            print("Stop the server first, then re-run.")
            return
        if target_dir.exists() and any(target_dir.iterdir()):
            if input(f"  Overwrite '{target_dir}'? (yes/no): ").strip().lower() != "yes":
                print("Restore cancelled.")
                return

    summary = restore_core.restore_chain(
        chains[chain_idx], point_idx, target_dir, backup_dir=backup_dir,
        exclude_names=exclude, copy_cmd=copy_cmd, manifest_path=manifest_path,
        establish_chain=establish, preserve_names=preserve,
        log_fn=print, dry_run=args.dry_run)
    if args.dry_run:
        print(f"\n[dry run] would restore full {summary['full']} + "
              f"{len(summary['incrementals'])} incremental(s).")
    else:
        print(f"\nRestore complete → {target_dir}")
        if summary.get("chain_id"):
            print(f"  New chain: {summary['chain_id']}")


if __name__ == "__main__":
    main()
