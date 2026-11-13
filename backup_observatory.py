#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""
TheSkyX Observatory Backup Script

If you need to run this script as root to ensure full access to all logs, hardware
reports, and files (e.g. `lshw` works best as root, and TSX might be owned by another user),
you can run it using `sudo`.

Since `root` might not have `uv` installed or in its PATH, the best way to execute this
script with `sudo` and `uv` is to run:
    sudo -E $(which uv) run backup_observatory.py

Or explicitly:
    sudo /home/youruser/.cargo/bin/uv run backup_observatory.py

To preview paths without backing up, run:
    uv run backup_observatory.py --detect-paths

Transfer engine
---------------
By default the transfer is done by rsync-commander
(https://github.com/2bitoperations/rsync-commander), fetched and cached
automatically with `uv tool run` (uvx). It sends only changed blocks, verifies
every block with SHA-1, compresses FITS on the wire (zstd), resumes interrupted
files, and uses parallel ssh streams.

SSH prompts: you are asked for your password at most ONCE per backup (zero
times if an ssh key works). rsync-commander probes key auth first. Without a
key it opens a single ssh master connection (the one prompt) and every stream
shares it. A key (ssh-copy-id) gives parallel streams and no prompt.

If rsync-commander can't be fetched (no uv, no network to GitHub), the script
falls back to the old single `rsync` run, which also prompts at most once.
`--engine rsync` forces it. `RC_COMMANDER_DIR=/path/to/checkout` uses a local
checkout instead of GitHub.
"""

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("TSXBackup")

# Directories that are NEVER backed up, from any source (imager, guider, logs),
# by either engine. Patterns match a directory's own name at any depth
# (rsync-style; [Xx] classes make them case-insensitive). Chosen from what had
# actually landed on the NAS over time:
#   "Automated Pointing Run NNN"  TPoint pointing runs
#   "Closed Loop Slews"           slew-verification frames (431 MiB had leaked)
#   "bad"                         frames you've binned by hand
#   "Train PEC NNN" / "TrainPEC Runs"  PEC-training captures
#   ".thumbs"                     TheSkyX thumbnail cache
ALWAYS_EXCLUDE_DIRS = [
    "*[Pp]ointing [Rr]un*",
    "*[Cc]losed [Ll]oop [Ss]lew*",
    "[Bb][Aa][Dd]",
    "*[Tt]rain*[Pp][Ee][Cc]*",
    ".thumbs",
]
# Files (any source) never backed up. TheSkyX also saves closed-loop slew frames
# straight into the date folder as "<date>-TSX-CLPA Slew-Light-...".
ALWAYS_EXCLUDE_FILES = [
    "*[Cc][Ll][Pp][Aa] [Ss]lew*",
]

def check_system_tools(check_reports=False, need_rsync=True):
    """Ensure required tools are installed before proceeding."""
    # Ensure system binaries are in PATH, as normal users on Debian
    # often don't have /usr/sbin in their PATH, leading to missing hwinfo.
    for p in ["/usr/sbin", "/sbin"]:
        if p not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] += f"{os.pathsep}{p}"

    tools = ['rsync'] if need_rsync else []
    if check_reports:
        tools.extend(['lshw', 'hwinfo', 'dpkg-query'])
        
    missing = []
    for tool in tools:
        if shutil.which(tool) is None:
            missing.append(tool)
    if missing:
        logger.error(f"Missing required system tools: {', '.join(missing)}")
        logger.error("Please install them before running this script.")
        hint = "apt install lshw hwinfo rsync" if check_reports else "apt install rsync"
        logger.error(f"Hint: {hint}")
        sys.exit(1)
    logger.info("All required system tools found.")

def find_tsx_directories():
    """Heuristic search for TheSkyX data directories."""
    home = Path.home()
    possible_roots = list(home.glob("Software Bisque/TheSkyX*")) + \
                     list(home.glob("Documents/Software Bisque/TheSkyX*")) + \
                     list(home.glob("Library/Application Support/Software Bisque/TheSkyX*"))
    
    if not possible_roots:
        logger.error("Could not find TheSkyX user data directories.")
        logger.error("Please ensure TheSkyX is installed and has been run at least once.")
        sys.exit(1)
    
    # Assume the first matching root that looks like it has data
    tsx_root = possible_roots[0]
    logger.info(f"Detected TheSkyX data root: {tsx_root}")
    
    paths = {
        'root': tsx_root,
        'imager': None,
        'guider': None,
        'logs': []
    }
    
    # Fuzzy heuristic for imager
    for p in list(tsx_root.rglob("*Imager Autosave*")) + list(tsx_root.rglob("Camera AutoSave/Imager")):
        if p.is_dir():
            paths['imager'] = p
            break
            
    # Guider
    # ("Autoguider" is what TheSkyX on Linux names it; it may hold only guiding logs)
    for p in (list(tsx_root.rglob("*Guider Autosave*")) + list(tsx_root.rglob("Camera AutoSave/Guider"))
              + list(tsx_root.rglob("Camera AutoSave/Autoguider"))):
        if p.is_dir():
            paths['guider'] = p
            break
            
    # Logs
    for log_name in ["Logs", "TPoint", "Guider"]:
        p = tsx_root / log_name
        if p.is_dir():
            paths['logs'].append(p)
            
    return paths

def generate_system_reports(temp_dir):
    """Generate hardware and package lists."""
    logger.info("Generating system reports...")
    try:
        with open(temp_dir / "installed_packages.txt", "w") as f:
            subprocess.run(["dpkg-query", "-l"], stdout=f, check=True)
            
        with open(temp_dir / "lshw_report.txt", "w") as f:
            subprocess.run(["lshw"], stdout=f, stderr=subprocess.DEVNULL)
            
        with open(temp_dir / "hwinfo_report.txt", "w") as f:
            subprocess.run(["hwinfo"], stdout=f, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.error(f"Failed to generate system reports: {e}")
        sys.exit(1)

def create_settings_archive(tsx_paths, temp_dir):
    """Bundle all critical configs into a tar.xz."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"tsx_settings_backup_{timestamp}.tar.xz"
    archive_path = temp_dir / archive_name
    
    logger.info(f"Creating highly compressed settings archive: {archive_path}")
    logger.info("This might take a moment due to xz compression...")
    
    exclude_paths = []
    if tsx_paths['imager']: exclude_paths.append(tsx_paths['imager'].resolve())
    if tsx_paths['guider']: exclude_paths.append(tsx_paths['guider'].resolve())
    
    try:
        with tarfile.open(archive_path, "w:xz") as tar:
            # Add TSX config
            if tsx_paths['root'].exists():
                logger.info(f"Adding TSX configs from {tsx_paths['root']}")
                
                def filter_excludes(tarinfo):
                    if tarinfo.name.lower().endswith(('.fit', '.fits')):
                        return None
                    for ex in exclude_paths:
                        try:
                            rel_ex = ex.relative_to(tsx_paths['root'])
                            expected_tar_name = f"TheSkyX_Config/{rel_ex}"
                            # Exclude the directory itself or anything inside it
                            if tarinfo.name == expected_tar_name or tarinfo.name.startswith(expected_tar_name + "/"):
                                return None
                        except ValueError:
                            pass
                    return tarinfo
                
                tar.add(tsx_paths['root'], arcname="TheSkyX_Config", filter=filter_excludes)
            
            # Add udev rules
            udev_path = Path("/etc/udev/rules.d")
            if udev_path.exists():
                logger.info(f"Adding {udev_path}")
                tar.add(udev_path, arcname="udev_rules.d")
                
            # Add system reports
            for report in ["installed_packages.txt", "lshw_report.txt", "hwinfo_report.txt"]:
                r_path = temp_dir / report
                if r_path.exists():
                    tar.add(r_path, arcname=report)
                    
        return archive_path
    except Exception as e:
        logger.error(f"Failed to create archive: {e}")
        sys.exit(1)

def stage_for_rsync(tsx_paths, archive_path, temp_dir):
    """Create a symlinked structure in /tmp for a single rsync run."""
    staging_dir = temp_dir / "rsync_staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
        
    staging_dir.mkdir()
    
    images_dir = staging_dir / "images"
    images_dir.mkdir()
    
    if tsx_paths['imager']:
        (images_dir / "main_imager").symlink_to(tsx_paths['imager'])
    else:
        logger.warning("No main imager directory found to stage.")
    
    if tsx_paths['guider']:
        (images_dir / "guider").symlink_to(tsx_paths['guider'])
        
    if tsx_paths['logs']:
        logs_dir = staging_dir / "logs"
        logs_dir.mkdir()
        for log_dir in tsx_paths['logs']:
            (logs_dir / log_dir.name).symlink_to(log_dir)
            
    snapshots_dir = staging_dir / "snapshots"
    snapshots_dir.mkdir()
    (snapshots_dir / archive_path.name).symlink_to(archive_path)
    
    return staging_dir

def run_rsync(staging_dir, remote, dest_path):
    """Execute the single rsync process."""
    hostname = socket.gethostname()
    remote_dest = f"{remote}:{dest_path}/{hostname}/"
    
    logger.info(f"Starting single rsync transfer to {remote_dest}")
    logger.info("You may be prompted for your SSH password if keys are not configured.")
    
    cmd = [
        "rsync",
        "-avhL",             # Archive, verbose, human-readable, copy-links
        "--checksum",        # Use checksums instead of mod-time & size
        "--partial",         # Keep partially transferred files
        "--info=progress2",  # Overall progress and throughput
        # Never-backed-up directories, anywhere (trailing / = directories only)
        *[arg for d in ALWAYS_EXCLUDE_DIRS for arg in ("--exclude", d + "/")],
        *[arg for f in ALWAYS_EXCLUDE_FILES for arg in ("--exclude", f)],
        # Include guider darks
        "--include", "images/guider/**/*Dark*.fit",
        "--include", "images/guider/**/*Dark*.fits",
        "--include", "images/guider/**/*dark*.fit",
        "--include", "images/guider/**/*dark*.fits",
        # Exclude all other guider fits
        "--exclude", "images/guider/**/*.fit",
        "--exclude", "images/guider/**/*.fits",
        
        # Exclusions for the main imager (using ** to match within date subdirectories)
        "--exclude", "images/main_imager/**/*@Focus*",
        "--exclude", "images/main_imager/**/*Focus*",
        "--exclude", "images/main_imager/**/*Closed Loop Slews*",
        "--exclude", "images/main_imager/**/*Automated Pointing Run*",
        "--exclude", "images/main_imager/**/*Guide*",
        str(staging_dir) + "/",
        remote_dest
    ]
    
    logger.info(f"Executing: {' '.join(cmd)}")
    
    try:
        subprocess.run(cmd, check=True)
        logger.info("✅ Rsync transfer completed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Rsync transfer failed with exit code {e.returncode}")
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        logger.error("\n❌ Transfer interrupted by user.")
        sys.exit(1)

RC_REPO = "git+https://github.com/2bitoperations/rsync-commander"

# Same selection as the rsync filter rules in run_rsync(), in rsync-commander's
# profile syntax (patterns without '/' match names at any depth).
ALWAYS_FILTER = ([f"- {d}/" for d in ALWAYS_EXCLUDE_DIRS]
                 + [f"- {f}" for f in ALWAYS_EXCLUDE_FILES])
MAIN_IMAGER_EXCLUDES = ["*@Focus*", "*Focus*", "*Closed Loop Slews*",
                        "*Automated Pointing Run*", "*Guide*"]
GUIDER_FILTER = ["+ *Dark*.fit", "+ *Dark*.fits", "+ *dark*.fit", "+ *dark*.fits",
                 "- *.fit", "- *.fits"]


def find_uv():
    for cand in (shutil.which("uv"), Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv",
                 "/usr/local/bin/uv", "/opt/homebrew/bin/uv"):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def rsync_commander_cmd(ref="main", update=False):
    """Command prefix that runs rsync-commander, fetching it if needed.
    Returns None if it can't be made available."""
    uv = find_uv()
    if not uv:
        logger.warning("uv not found: cannot fetch rsync-commander.")
        return None
    local = os.environ.get("RC_COMMANDER_DIR")
    if local:
        cmd = [uv, "run", "--project", local, "--no-group", "gui", "--no-group", "dev",
               "rsync-commander"]
        check = cmd
    else:
        cmd = [uv, "tool", "run", "--from", f"{RC_REPO}@{ref}", "rsync-commander"]
        # --update-commander: refresh once here; the real run then uses the new cache
        check = cmd[:3] + (["--refresh-package", "rsync-commander"] if update else []) + cmd[3:]
    logger.info("Fetching/checking rsync-commander (cached after the first run)...")
    try:
        subprocess.run(check + ["batch", "--help"], check=True, stdout=subprocess.DEVNULL,
                       timeout=900)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"Could not fetch/run rsync-commander: {e}")
        return None
    return cmd


def _toml_str(s):
    # JSON string escapes are valid TOML basic-string escapes
    return json.dumps(str(s), ensure_ascii=False)


def _toml_list(items):
    return "[" + ", ".join(_toml_str(i) for i in items) + "]"


def write_profile(tsx_paths, archive_path, remote, dest_path, temp_dir, args):
    """An rsync-commander batch profile mirroring the rsync staging tree."""
    hostname = socket.gethostname()  # same destination dir name as the rsync engine
    lines = [
        "# generated by backup_observatory.py",
        f"dest = {_toml_str(f'{remote}:{dest_path}/{hostname}')}",
        'ssh_mux = "auto"',
        "",
        "[options]",
        f"workers = {int(args.workers)}",
        "checksum = true",
        f"compression = {_toml_str(args.compression)}",
        "",
    ]

    def source(path, to, extra=()):
        lines.extend(["[[source]]", f"path = {_toml_str(path)}", f"to = {_toml_str(to)}",
                      "follow_symlinks = true", *extra, ""])

    if tsx_paths['imager']:
        source(tsx_paths['imager'], "images/main_imager",
               [f"filter = {_toml_list(ALWAYS_FILTER)}",
                f"exclude = {_toml_list(MAIN_IMAGER_EXCLUDES)}"])
    else:
        logger.warning("No main imager directory found to back up.")
    if tsx_paths['guider']:
        source(tsx_paths['guider'], "images/guider",
               [f"filter = {_toml_list(ALWAYS_FILTER + GUIDER_FILTER)}"])
    for log_dir in tsx_paths['logs']:
        source(log_dir, f"logs/{log_dir.name}", [f"filter = {_toml_list(ALWAYS_FILTER)}"])
    if archive_path:
        source(archive_path, f"snapshots/{archive_path.name}")
    profile = temp_dir / "observatory-profile.toml"
    profile.write_text("\n".join(lines))
    return profile


def run_rsync_commander(cmd, profile, dry_run=False):
    run = cmd + ["batch", str(profile), "--interval", "15"]
    if dry_run:
        run.append("--dry-run")
    logger.info(f"Executing: {' '.join(run)}")
    try:
        r = subprocess.run(run)
    except KeyboardInterrupt:
        logger.error("\n❌ Transfer interrupted by user (partial files are kept for resume).")
        sys.exit(1)
    if r.returncode == 0:
        logger.info("✅ rsync-commander transfer completed successfully.")
    else:
        logger.error(f"❌ rsync-commander failed with exit code {r.returncode}")
        sys.exit(r.returncode)


def preview_archive(tsx_paths):
    """Preview the files and sizes that will be included in the settings archive."""
    logger.info("--- Archive Preview (TSX Configs) ---")
    
    exclude_paths = []
    if tsx_paths['imager']: exclude_paths.append(tsx_paths['imager'].resolve())
    if tsx_paths['guider']: exclude_paths.append(tsx_paths['guider'].resolve())
    
    if not tsx_paths['root'].exists():
        logger.error(f"Root path does not exist: {tsx_paths['root']}")
        sys.exit(1)
        
    root_path = tsx_paths['root'].resolve()
    total_size = 0
    file_list = []
    
    for dirpath, dirnames, filenames in os.walk(root_path):
        current_dir = Path(dirpath).resolve()
        
        # Check if current_dir is excluded or inside an excluded dir
        is_excluded = False
        for ex in exclude_paths:
            if current_dir == ex or ex in current_dir.parents:
                is_excluded = True
                break
                
        if is_excluded:
            # Skip traversing this excluded directory
            dirnames[:] = []
            continue
            
        for f in filenames:
            if f.lower().endswith(('.fit', '.fits')):
                continue
            f_path = current_dir / f
            if f_path.is_file() and not f_path.is_symlink():
                size = f_path.stat().st_size
                total_size += size
                try:
                    rel_path = f_path.relative_to(root_path)
                except ValueError:
                    rel_path = f_path
                file_list.append((str(rel_path), size))
                
    # Sort by size descending
    file_list.sort(key=lambda x: x[1], reverse=True)
    
    logger.info(f"Total files to be archived from config: {len(file_list)}")
    logger.info(f"Total size: {total_size / (1024*1024):.2f} MB")
    
    if file_list:
        logger.info("Top 20 largest files:")
        for path, size in file_list[:20]:
            logger.info(f"  {size / (1024*1024):>7.2f} MB - {path}")
            
    sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="TheSkyX Observatory Backup Script")
    parser.add_argument("--remote", default="armalota@devnull", help="SSH remote user@host (default: armalota@devnull)")
    parser.add_argument("--dest-path", default="/volume1/temp/actually-temp", help="Destination base path on remote (default: /volume1/temp/actually-temp)")
    parser.add_argument("--detect-paths", action="store_true", help="Run path detection heuristics and exit")
    parser.add_argument("--preview-archive", action="store_true", help="Preview the files and sizes to be archived and exit")
    parser.add_argument("--system-reports", action="store_true", help="Generate hardware and package lists (requires lshw, hwinfo, dpkg-query)")
    parser.add_argument("--engine", choices=["auto", "commander", "rsync"], default="auto",
                        help="auto (default): rsync-commander, falling back to rsync if it can't be fetched; "
                             "commander: rsync-commander only; rsync: the classic single rsync run")
    parser.add_argument("--commander-ref", default="main",
                        help="git branch/tag/commit of rsync-commander to use (default: main)")
    parser.add_argument("--update-commander", action="store_true",
                        help="re-fetch rsync-commander instead of using the cached copy")
    parser.add_argument("--workers", type=int, default=4, help="parallel streams (rsync-commander)")
    parser.add_argument("--compression", choices=["zstd", "gzip", "none"], default="zstd",
                        help="wire compression (rsync-commander; default zstd)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the archive and list what would be sent, transfer nothing (rsync-commander)")
    
    args = parser.parse_args()
    
    logger.info("Initializing Observatory Backup")
    
    commander = None
    if not args.detect_paths and not args.preview_archive:
        if args.engine in ("auto", "commander"):
            commander = rsync_commander_cmd(args.commander_ref, args.update_commander)
            if commander is None:
                if args.engine == "commander":
                    logger.error("rsync-commander is not available (see above).")
                    sys.exit(1)
                logger.warning("Falling back to the classic rsync engine.")
        if commander is None and args.dry_run:
            logger.error("--dry-run needs rsync-commander.")
            sys.exit(1)
        check_system_tools(check_reports=args.system_reports, need_rsync=commander is None)
    
    tsx_paths = find_tsx_directories()
    
    if args.detect_paths:
        logger.info("--- Path Detection Results ---")
        logger.info(f"Root:   {tsx_paths['root']}")
        logger.info(f"Imager: {tsx_paths['imager']}")
        logger.info(f"Guider: {tsx_paths['guider']}")
        logger.info(f"Logs:   {', '.join(str(p) for p in tsx_paths['logs']) if tsx_paths['logs'] else 'None'}")
        
        if not tsx_paths['imager'] or not tsx_paths['guider']:
            logger.warning("Could not find some expected directories. You may need to take images first.")
        sys.exit(0)
        
    if args.preview_archive:
        preview_archive(tsx_paths)
        
    temp_dir = Path("/tmp/tsx_backup_run")
    temp_dir.mkdir(exist_ok=True)
    
    try:
        if args.system_reports:
            generate_system_reports(temp_dir)
        archive_path = create_settings_archive(tsx_paths, temp_dir)
        if commander:
            profile = write_profile(tsx_paths, archive_path, args.remote, args.dest_path,
                                    temp_dir, args)
            run_rsync_commander(commander, profile, dry_run=args.dry_run)
        else:
            staging_dir = stage_for_rsync(tsx_paths, archive_path, temp_dir)
            run_rsync(staging_dir, args.remote, args.dest_path)
    finally:
        logger.info("Cleaning up temporary staging files...")
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    main()
