# Samurai Stack

[![Checks](https://github.com/SamarieSystems/samurai-stack/actions/workflows/checks.yml/badge.svg)](https://github.com/SamarieSystems/samurai-stack/actions/workflows/checks.yml)
[GPL-3.0-only](LICENSE) | [Releases](https://github.com/SamarieSystems/samurai-stack/releases) | [Changelog](CHANGELOG.md)

Build a private media server with a guided installer. Pick a step, review what it will change and enter the requested application credentials. Media accounts, private API keys and application connections are set up in the terminal. You choose the indexers, subtitle providers and quality preferences. Stop and return to the same step without starting over.

**0.2.7.** A staged private media-server installer for the documented Linux guest path, including CPU-only machines. Recorded checks cover real accounts, imports, restores, playback and controlled VPN failure/recovery, with their exact scope in [validation](docs/VALIDATION.md). Commission each new installation's storage, provider accounts, playback and any selected notification delivery before handing it over. See [supported scope](docs/ROADMAP.md) for the remaining boundaries.

## Start here

**Using a server someone already installed?** Start with the
[plain-language user handbook](docs/guide/README.md): watching, requests,
downloads, files, subtitles, maintenance and troubleshooting. The
[handover sheet](docs/guide/HANDOVER-CHECKLIST.md) records your own links and
acceptance checks. You do not need to run the installer to use Jellyfin.

Inside a **new Debian 13 KVM/QEMU VM**, download `samurai-setup.sh` and
`SHA256SUMS` from the [release assets](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.2.7).
Copy both files into the same directory in the VM, open a terminal there, then run:

```sh
sha256sum --check --ignore-missing SHA256SUMS
bash ./samurai-setup.sh
```

Require `samurai-setup.sh: OK` before running it. The Bash entry point guides
missing Python prerequisites and guest preparation. Git, `pip` and a Python
virtual environment are not needed. From a normal account it requests sudo
when available; otherwise use a root console (`su -`). [Quickstart](docs/QUICKSTART.md)
covers each screen, media accounts and the remaining provider choices.

**Already wiped the old applications and want to start over?** Run
`./setup --fresh`, or `bash /path/to/samurai-setup.sh --fresh` with the
verified portable asset. This opens a blank phases 1-6 wizard with no old site,
credentials or cached progress. It safely archives eligible stale installer
state and services; existing appdata or containers block takeover. Existing
media and mounts stay in place. Use normal setup to resume the new session;
`--fresh` again means another blank session. See [fresh setup](docs/QUICKSTART.md#start-fresh-after-removing-the-old-applications).

Already have the release source? Run `./setup`. After storage verification,
configuration is saved outside the source folder at
`/var/lib/samurai-stack/installations/DEPLOYMENT-UUID/site.json`; progress also
lives under `/var/lib/samurai-stack/setup`. Replacing a ZIP or moving the
checkout does not create another installation identity. Existing private
source-tree configurations remain supported with `--config`.

Already running an older source ZIP? Obtain `samurai-setup.sh` from the
[0.2.7 release assets](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.2.7),
check its supplied `SHA256SUMS` and follow the upgrade path below as root.

**Upgrading an existing 0.2.0/0.2.1 local-disk installation:** first use
`bash /path/to/samurai-setup.sh --upgrade-storage-identity`. This reviewed
maintenance step confirms the original filesystem UUIDs and updates the saved
storage/receipt bindings without starting over or touching media. Use the
[existing-installation storage upgrade](docs/STORAGE.md#upgrade-an-existing-local-installation)
instructions before resuming the numbered setup steps, especially after a
reboot changed `/dev/sdX` letters. New installations already capture persistent
UUIDs; do not use `--fresh` as an upgrade.

After that review, or for installations already using the current identities:

```sh
bash /path/to/samurai-setup.sh 3
```

This resumes a uniquely identified saved installation. For an older release
whose private configuration has not been registered yet, use
`bash /path/to/samurai-setup.sh --config /path/to/existing/site.json 3`.
No Git checkout or source replacement is needed. Preserve appdata and secrets;
the saved configuration does not contain their databases or passwords.

If the machine still runs Windows or has important files on its disks, first choose a safe VM and storage arrangement. This does not install Proxmox or convert the machine in place. Use the [deployment intake](docs/DEPLOYMENT-NIGHT.md) and [Proxmox guide](docs/PROXMOX.md) before changing the physical host.

## The menu

| Step | What you get |
| --- | --- |
| **1. Prepare this guest** | Checks the Debian/Ubuntu guest, shows package/repository changes, then installs Docker, Compose, the guest agent and NFS support after confirmation. |
| **2. Connect storage** | Lists suitable mounted local filesystems or guides a dedicated NFS export. Reviews the backing mount and a new empty media directory, then verifies access and prepares the data tree. |
| **3. Set up media** | Jellyfin, Seerr, Sonarr, Radarr, Prowlarr and Bazarr, with optional SABnzbd. Enrolls administrator and separate everyday viewer access, saves private API keys, enables hardlinks, selects the owned Jellyfin libraries in Seerr and connects the apps. Usenet setup tests verified TLS and NNTP authentication in the terminal. |
| **4. Set up VPN torrents** | qBittorrent behind Gluetun, protocol-specific hidden credential prompts, normal magnet entry and application connections. |
| **5. Set up management** | Dockge/Kuma terminal owner enrollment or existing-owner authentication, with named private TCP monitors. Optional Homarr and notification delivery have separate checks. |
| **6. Add photos or music** | Immich has terminal owner enrollment, lean resource defaults and a reviewed quota. Optional music has separate account/playback commissioning. Add extras when you can finish their backup or playback checks. |

Run `./setup 3` to open media directly, or `./setup 4` to return to VPN torrents. These are **guided menu numbers**; the advanced engine has separate stage IDs. The installer handles that mapping.

For an existing installation, `./setup --hardlinks` checks and offers to enable only Sonarr/Radarr's hardlink settings. It does not deploy containers, scan libraries or move media. A standalone repair for older source copies is documented in [Install and repair](docs/INSTALL.md).

New media accounts can opt into reviewed 720p/1080p original-audio profiles
during setup. Use `./setup --quality-profiles` or menu **p** to review this
separately. Existing profiles, defaults and media keep their settings. Global
size-definition changes have a separate review. See [Media profiles](docs/MEDIA-PROFILES.md).

Step 3 also offers [future-import naming](docs/MEDIA-NAMING.md), an optional
[English subtitle policy](docs/SUBTITLES.md) and private
[indexer intake/readiness](docs/INDEXERS.md). Existing media is not renamed or
backfilled, and source/provider registration is separate from actual delivery.

Use `./setup --capacity` or menu **c** to review free-space safeguards. The
default suggestion is 50 GiB for data and 20 GiB for application storage,
with native Starr/SAB checks and an optional owned qBit pause watcher after
VPN setup. These are thresholds, not quotas; downloads never automatically
resume. See [Capacity safeguards](docs/CAPACITY.md).

Start with CPU playback. GPU passthrough, guest drivers and a real transcode test remain deliberate hardware work. One compact application VM is the shortest first rollout. Separate VMs need the same installation identity and shared storage configuration; follow [the advanced multi-VM instructions](docs/ADVANCED.md#several-vms-one-installation).

## What you still provide

- A supported Linux VM, a reserved LAN address and a deliberate storage choice. NAS exports, users and access rules are created in the NAS interface.
- One application administrator login and a separate everyday Jellyfin/Seerr viewer login, entered with hidden password confirmation. Seerr uses **Sign in with Jellyfin**. Existing accounts are preserved; older unconfigured apps require explicit account-absence proof before enrollment. Management and optional applications retain their separate onboarding checks.
- Your own VPN, Usenet, indexer, subtitle and optional music-provider credentials. Hidden prompts and private files keep secrets out of command arguments and plans.
- Quality/language choices, actual player tests, notification delivery and independent backups. A green container cannot prove those worked.

Use [Quickstart](docs/QUICKSTART.md) for the normal path. [Advanced CLI](docs/ADVANCED.md) preserves individual plan/apply/configure/verify commands. [Services](docs/SERVICES.md), [Recovery](docs/RECOVERY.md) and [Acceptance](docs/ACCEPTANCE.md) explain application contracts and final checks.

## Data and security boundaries

No disk formatting, repartitioning, RAID creation, collection migration or recursive permission repair. Unowned nonempty data roots, mismatched mounts and another installation's marker are refused. A new guided NFS mount requires explicit review and a backup of the previous guest fstab; NAS configuration is never guessed remotely.

Downloads and libraries share a consistent `/data` tree. Databases and app configuration stay on local Linux storage. Jellyfin, Navidrome and Immich external libraries receive read-only media mounts. Immutable image indexes are pinned and preserved on retries; upgrades are not automatic.

Keep the installation on a trusted LAN or private VPN. No public DNS, router forwarding or WAN reverse proxy is created. Dockge can administer its local Docker host, so protect its login accordingly. Read [Security](docs/SECURITY.md) before exposing anything; application login alone is not a complete internet boundary.

Generated systemd units own guarded startup. Use them for whole-stack maintenance; Dockge remains useful for logs and container terminals. [Recovery](docs/RECOVERY.md) explains the controller interaction and resuming without deleting state.

Use `./setup --recover` for a read-only container, mount and saved-path report.
Conflicting projects and missing or replaced appdata paths stop setup before
recreation. A leftover generated systemd unit can be privately archived only
when its project and its old installation UUID have no containers, including
stopped ones. This does not restore deleted application databases. A wrong
storage choice in an unused, unregistered setup has a separate
[`--choose-storage` correction](docs/QUICKSTART.md#correct-an-unused-storage-choice).

The simple torrent stack has no ClamAV or approval queue. Files remain untrusted after downloading. This project does not change security controls on another existing installation.

MediaBot is a [separate optional project](https://github.com/SamarieSystems/mediabot), with an [installer audit](docs/MEDIABOT-AUDIT.md). Home Assistant and local LLMs are not prerequisites.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 scripts/check-release.py .
python3 scripts/validate-compose.py
```

Compose validation never starts containers. Runtime, VPN interruption/recovery, real playback and photo restore need the [acceptance checks](docs/ACCEPTANCE.md). GPL-3.0-only; vendor images retain their own licenses.
