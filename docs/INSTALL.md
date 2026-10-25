# Install or repair Samurai Stack

For a new installation, follow [Guided setup](QUICKSTART.md) and the
[deployment-night checklist](DEPLOYMENT-NIGHT.md). Run setup inside the intended
Linux guest. Physical Proxmox installation, disk selection, NAS export permissions
and GPU passthrough remain separate decisions.

## Start over after an intentional application wipe

Use `./setup --fresh` from the current release, or run the verified portable
asset as root inside the VM:

```sh
bash /path/to/samurai-setup.sh --fresh
```

This opens a new blank **phases 1-6** wizard. It does not need an old `site.json`
and does not reuse old credentials or setup progress. Phase 1 verifies existing
OS prerequisites, phase 2 asks for new storage/configuration, and phase 3 asks
for new administrator/viewer credentials and wires the supported media apps.

Eligible old installer configuration and exact generated services are
archived privately only when the application roots are absent or empty and all
matching containers are gone, including stopped ones. Existing appdata blocks
takeover. Old progress is retained without reuse. Cleanup does not modify
media, storage markers or mounted filesystems.
Choose a new empty directory on the intended data mount during phase 2.

Normal setup resumes this new session, including before phase 2. Running
`--fresh` again deliberately creates another blank session. See
[fresh setup](QUICKSTART.md#start-fresh-after-removing-the-old-applications).

An existing Samurai installation keeps its installation identity, credentials
and data paths. Verified configuration is saved outside the source tree
at `/var/lib/samurai-stack/installations/DEPLOYMENT-UUID/site.json`, with
guided progress under `/var/lib/samurai-stack/setup`. A source-folder update
does not reset that state. Older private site files remain compatible through
`--config`. Use the focused action below to enable hardlinks in running apps.

## Media setup and an existing installation

Guided **step 3** enrolls the selected Jellyfin, Sonarr, Radarr, Prowlarr, Bazarr,
Seerr and SABnzbd accounts, saves private API keys and wires the supported
application connections. SAB is omitted when Usenet is disabled. New accounts
use one application administrator login and a separate everyday Jellyfin/Seerr
viewer, entered through hidden password prompts. **Seerr uses Sign in with
Jellyfin**, not a separate local password. Leave browser account wizards alone
while terminal enrollment runs.

When Usenet is enabled, the terminal also requests the NNTP server, TLS port,
connection allowance and provider service credentials. It performs an actual
authenticated connection test with certificate verification enabled and
completes SAB's wizard. Existing configured servers are tested and preserved.
No browser account wizard or manual API-key paste is required for the supported
media setup. Choosing indexer accounts, subtitle providers/languages and quality
profiles remains an operator decision. New Sonarr/Radarr accounts can opt into
the [reviewed HD/original-audio preset](MEDIA-PROFILES.md); existing settings
are preserved. These choices and a real
download/import/playback test still need attention after API wiring passes.

Existing configured accounts are checked without replacing their credentials
or permissions. An existing Jellyfin or SAB login may be requested to authorize
the supported connection/provider setup; supply its current credentials.
Unconfigured apps left by an older alpha are enrolled only after app-specific
proof that no configured account exists. An incomplete wizard flag is not
enough. Unknown state pauses for review, not an owner reset.

The shortest update for an existing source ZIP uses the portable
`samurai-setup.sh` asset from the
[0.2.0 release candidate](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.2.0-rc.3).
Verify it against the supplied `SHA256SUMS`, then run as root on the media VM:

```sh
bash /path/to/samurai-setup.sh 3
```

No Git installation or source-tree replacement is required. Default discovery
uses the trusted saved installation. For an older release not yet registered,
use `bash /path/to/samurai-setup.sh --config /actual/old/site.json 3`.
Its existing saved paths and credentials are reused. Media wiring also enables
only the two deployment-named Jellyfin movie/TV libraries in Seerr,
after path/ID verification, while retaining unrelated selections. It does not
start library scans or request media.

To resume with updated source, keep existing configuration, deployment ID,
appdata, storage marker and secrets. A source ZIP installation does not need
Git: extract the new source beside the old directory and run there:

```sh
chmod +x setup stack
./setup 3
```

If the private site has not been registered yet, explicitly pass
`--config /absolute/path/to/old-source/site.json`. The canonical copy is saved
only after live storage verification. Do not create a new deployment identity
or copy appdata into the source tree. Source updates do not rotate credentials
or move media, and a configuration copy cannot restore deleted databases.
See [Guided setup](QUICKSTART.md#update-an-existing-installation-including-a-source-zip)
for the ZIP and Git update paths and the complete step-by-step workflow.

## Leftover services and existing-installation diagnostics

After a complete intentional application wipe, use the fresh entry point above.
No old site is required. It checks leftover generated services and stale private
installer configuration before opening the new wizard.

For an installation you intend to preserve, `./setup --recover` inspects saved
paths and service/container ownership without changing them. Normal resume
keeps the saved identity and credentials. An eligible obsolete conflicting unit
can be archived during apply, but remaining containers, custom commands and
systemd overrides block takeover. Configuration and service files cannot
restore deleted application databases; those require their actual backups.

For a first-run local storage choice that accidentally used the VM root
filesystem, see the restricted
[`--choose-storage` correction](QUICKSTART.md#correct-an-unused-storage-choice).
It does not migrate a registered installation or reuse an occupied media tree.

## Enable hardlinks on an existing installation

Open a terminal on the existing **media VM**, enter the current release source
directory and run:

```sh
./setup --hardlinks
```

The guided action reads both settings first. If either is disabled, it asks
before enabling them, then independently checks the saved values. For an
existing configuration elsewhere, pass its actual path with `--config`.

The equivalent advanced commands are:

```sh
sudo ./stack --config site.json hardlinks
sudo ./stack --config site.json hardlinks --apply
sudo ./stack --config site.json hardlinks
```

The default command is read-only. `--apply` changes only Sonarr and Radarr's
`copyUsingHardlinks` boolean, the API setting behind **Use Hardlinks instead of
Copy**. It preserves their other media-management settings and reads both flags
back. The action checks that the configured local API ports belong to this
installation's running, owned containers before contacting them.

This repair does not redeploy or restart containers, recreate accounts, scan or
import media, move downloads, migrate directories, or change VPN settings. Other
applications do not need to be enrolled first. Keep the existing configuration;
creating another installation identity would prevent the ownership checks from
matching the running services.

The portable release asset also performs this focused repair on machines
using an older source ZIP. Obtain `samurai-setup.sh` and `SHA256SUMS` from the
[0.2.0 release candidate](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.2.0-rc.3)
and verify the checksum before running it inside the media VM:

```sh
sha256sum --check --ignore-missing SHA256SUMS
bash ./samurai-setup.sh --hardlinks
```

Require `samurai-setup.sh: OK` from the checksum check. This uses the trusted
saved installation. For an older unregistered site, append
`--config /absolute/path/to/existing/site.json`. No source replacement or
application redeployment is needed.

The older `samurai-hardlinks.pyz` asset remains an advanced compatibility
option requiring an already installed Python 3.11+ runtime. After verifying
its checksum, its interface is:

```sh
sudo python3 samurai-hardlinks.pyz --config /path/to/existing/site.json
sudo python3 samurai-hardlinks.pyz --config /path/to/existing/site.json --apply
```

Use the existing media VM and configuration for that artifact too. An older
source directory does not need replacing to run the standalone repair.

## What the paths mean

[TRaSH's Docker layout](https://trash-guides.info/File-and-Folder-Structure/How-to-set-up/Docker/)
distinguishes host storage paths from container paths. Its important design is a
shared tree that lets Starr see downloads and libraries together. The host
prefix can follow the actual disk or NAS mount. Separate container mounts for
downloads and libraries can prevent hardlinks even when their host paths look
related.

Samurai maps the chosen `storage.data_root` to `/data` in Starr and the download
clients. The library and download paths are children of that same tree:

```text
<existing data_root>/
  downloads/torrents/{movies,tv,manual,incomplete}
  downloads/usenet/{complete,incomplete}
  library/{movies,tv,music}
```

| Host path | Purpose |
| --- | --- |
| `/opt/samurai` | Conventional default for this toolkit's local application state, configuration and databases. A different supported, dedicated local directory can be selected during initial setup. This is a project convention, not a claim that the applications require that prefix. |
| `/srv/samurai-data` | Fallback example for the explicit local-directory/OS-filesystem option. It is not an automatic selection of a separate media disk. |
| `/mnt/media/samurai` | Example of a dedicated child on an existing reviewed disk or NAS mount at `/mnt/media`. An installation already using this path should retain it. |

Changing `/mnt/media/samurai` to `/srv/samurai-data` is not a hardlink repair.
Keep the actual saved mount and path. Hardlinks require the source and target
to share a filesystem that supports them, with suitable permissions; a shared
prefix alone does not prove that. No path or media migration is needed to enable
the application flags.

## Settings verification versus import proof

A successful repair proves both API flags are `true`. It does not prove a real
Starr import used hardlinks, and it does not replace existing duplicate copies
with hardlinks. Storage setup's temporary hardlink probe establishes a separate
filesystem capability check.

For end-to-end proof, use one controlled, authorized torrent that remains
seeding after its normal Starr import. Compare that file's download and library
paths on the media host with this read-only command, substituting the actual
two paths:

```sh
stat -c 'device=%d inode=%i links=%h %n' -- \
  '/path/to/download-file.mkv' '/path/to/imported-file.mkv'
```

Both paths must report the same device and inode, with link count at least two.
Equal sizes alone do not prove a hardlink. Choose a normal completed-download
import for this test; a manual import deliberately set to Move can consume the
source instead. The repair itself does not initiate either import mode.
