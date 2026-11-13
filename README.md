# Observatory Backup

`backup_observatory.py` backs up a TheSkyX observatory machine (Debian) to a NAS.

## What it backs up

Everything lands under `<dest-path>/<hostname>/` on the remote:

| Path | Contents |
|---|---|
| `images/main_imager/` | Imager autosave frames. Focus runs, closed-loop slews, automated pointing runs and guide frames are excluded. |
| `images/guider/` | Guider **darks** only. |
| `logs/{Logs,TPoint,Guider}/` | TheSkyX logs. |
| `snapshots/tsx_settings_backup_<timestamp>.tar.xz` | TheSkyX settings (no FITS, no autosave dirs), udev rules, and with `--system-reports` the `dpkg`/`lshw`/`hwinfo` reports. |

TheSkyX's data root and the imager, guider and log dirs are found by heuristics. Check them with `--detect-paths`.

## Transfer engine

By default the transfer runs through
[rsync-commander](https://github.com/2bitoperations/rsync-commander). It is
fetched and cached automatically by `uv tool run` on first use, so there is
nothing to install by hand besides `uv`. Compared with plain rsync it:

- compares existing files block by block (SHA-1) and sends only changed blocks;
- verifies every block on both ends;
- compresses FITS on the wire with zstd (subs typically shrink to 35–50%);
- resumes interrupted files;
- uses several parallel ssh streams, which matters on a slow, high-latency link.

**Password prompts: at most one per backup.** If an ssh key works for the
remote, there are no prompts and the backup uses parallel connections. If not,
rsync-commander opens a single ssh connection (you type the password once), and
every stream shares it. Run `ssh-copy-id user@nas` once to get parallel streams
and no prompt.

If rsync-commander can't be fetched (no `uv`, or no network to GitHub), the script
falls back to the classic single `rsync -avhL --checksum` run, which also prompts
at most once. The fallback only happens before any transfer starts, so you are never
asked twice.

## Usage

```bash
# Check which directories were detected
uv run backup_observatory.py --detect-paths

# Preview what goes into the settings archive
uv run backup_observatory.py --preview-archive

# List what would be sent, transfer nothing
uv run backup_observatory.py --dry-run

# Back up to the default destination
uv run backup_observatory.py

# Explicit destination, system reports, fewer streams
uv run backup_observatory.py --remote backupuser@server --dest-path /volume1/backups --system-reports --workers 2

# Engine control
uv run backup_observatory.py --engine rsync            # classic rsync only
uv run backup_observatory.py --engine commander        # rsync-commander only (no fallback)
uv run backup_observatory.py --update-commander        # re-fetch the latest rsync-commander
uv run backup_observatory.py --commander-ref <tag|sha> # pin a version
RC_COMMANDER_DIR=~/src/rsync-commander uv run backup_observatory.py   # use a local checkout
```

Other options: `--compression zstd|gzip|none`.

## Running with sudo

`lshw` and `hwinfo` give fuller reports as root. `root` may not have `uv` on its
PATH, so run:

```bash
sudo -E $(which uv) run backup_observatory.py --system-reports
```

Under sudo, ssh uses **root's** keys and known_hosts.

## Notes

- With the classic rsync engine, the destination base directory (`--dest-path`)
  must already exist on the remote. rsync-commander creates it.
- Files that are already up to date are verified by checksum, not re-sent. An
  incremental run sends only new frames and the new settings snapshot.
