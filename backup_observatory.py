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
"""

import argparse
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

def check_system_tools():
    """Ensure required tools are installed before proceeding."""
    # Ensure system binaries are in PATH, as normal users on Debian
    # often don't have /usr/sbin in their PATH, leading to missing hwinfo.
    for p in ["/usr/sbin", "/sbin"]:
        if p not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] += f"{os.pathsep}{p}"

    tools = ['lshw', 'hwinfo', 'dpkg-query', 'rsync']
    missing = []
    for tool in tools:
        if shutil.which(tool) is None:
            missing.append(tool)
    if missing:
        logger.error(f"Missing required system tools: {', '.join(missing)}")
        logger.error("Please install them before running this script.")
        logger.error("Hint: apt install lshw hwinfo rsync")
        sys.exit(1)
    logger.info("All required system tools found.")

def find_tsx_directories():
    """Heuristic search for TheSkyX data directories."""
    home = Path.home()
    possible_roots = list(home.glob("Software Bisque/TheSkyX*")) + \
                     list(home.glob("Documents/Software Bisque/TheSkyX*"))
    
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
    for p in tsx_root.rglob("*Imager Autosave*"):
        if p.is_dir():
            paths['imager'] = p
            break
            
    # Guider
    for p in tsx_root.rglob("*Guider Autosave*"):
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
        # Exclusions for the main imager
        "--exclude", "images/main_imager/*@Focus*",
        "--exclude", "images/main_imager/*Focus*",
        "--exclude", "images/main_imager/*ClosedLoop*",
        "--exclude", "images/main_imager/*Guide*",
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

def main():
    parser = argparse.ArgumentParser(description="TheSkyX Observatory Backup Script")
    parser.add_argument("--remote", default="armalota@devnull", help="SSH remote user@host (default: armalota@devnull)")
    parser.add_argument("--dest-path", default="/volume1/temp/actually-temp", help="Destination base path on remote (default: /volume1/temp/actually-temp)")
    parser.add_argument("--detect-paths", action="store_true", help="Run path detection heuristics and exit")
    
    args = parser.parse_args()
    
    logger.info("Initializing Observatory Backup")
    
    if not args.detect_paths:
        check_system_tools()
    
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
        
    temp_dir = Path("/tmp/tsx_backup_run")
    temp_dir.mkdir(exist_ok=True)
    
    try:
        generate_system_reports(temp_dir)
        archive_path = create_settings_archive(tsx_paths, temp_dir)
        staging_dir = stage_for_rsync(tsx_paths, archive_path, temp_dir)
        run_rsync(staging_dir, args.remote, args.dest_path)
    finally:
        logger.info("Cleaning up temporary staging files...")
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    main()
