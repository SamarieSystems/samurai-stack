# Samurai Stack

[![Checks](https://github.com/SamarieSystems/samurai-stack/actions/workflows/checks.yml/badge.svg)](https://github.com/SamarieSystems/samurai-stack/actions/workflows/checks.yml)
[GPL-3.0-only](LICENSE) | [Releases](https://github.com/SamarieSystems/samurai-stack/releases) | [Changelog](CHANGELOG.md)

Build a private media server with a guided installer. Pick a step, review what it will change and enter the requested application credentials. Media accounts, private API keys and application connections are set up in the terminal. You choose the indexers, subtitle providers and quality preferences. Stop and return to the same step without starting over.

**0.1.0 alpha 5.** Start with a new supported Linux VM and a dedicated test directory. This is an alpha with recorded disposable-VM checks, not a promise that an arbitrary existing machine is ready to modify. See [validation](docs/VALIDATION.md) for tested behavior and remaining limits, and [the 0.2 release gates](docs/ROADMAP.md) for the next milestone.

## Start here

Inside a **new Debian 13 KVM/QEMU VM**, open a root console (`su -` if needed):

```sh
apt-get update && apt-get --no-remove --no-upgrade install -y git python3 ca-certificates
git clone --branch v0.1.0-alpha.5 https://github.com/SamarieSystems/samurai-stack.git && cd samurai-stack
./setup
```

From a normal user account, the installer requests sudo when available; otherwise use the root console. It does not need `pip`, a Python virtual environment or copied plan hashes. [Quickstart](docs/QUICKSTART.md) covers each screen, media accounts and the remaining provider choices.

Already have the release source? Run `./setup` from that directory. An existing `site.json` and stored credentials are reused. Keep them when updating the source.

Already running an older source ZIP? Obtain `samurai-setup.pyz` from the
[alpha 5 release assets](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.1.0-alpha.5),
check its supplied `SHA256SUMS`, then run this from the existing source directory
as root:

```sh
python3 /path/to/samurai-setup.pyz --config site.json 3
```

This resumes media setup using the original configuration, deployment ID,
appdata and private credentials. No Git checkout or source replacement is needed.
Use an absolute `--config` path when running from another directory.

If the machine still runs Windows or has important files on its disks, first choose a safe VM and storage arrangement. This does not install Proxmox or convert the machine in place. Use the [deployment intake](docs/DEPLOYMENT-NIGHT.md) and [Proxmox guide](docs/PROXMOX.md) before changing the physical host.

## The menu

| Step | What you get |
| --- | --- |
| **1. Prepare this guest** | Checks the Debian/Ubuntu guest, shows package/repository changes, then installs Docker, Compose, the guest agent and NFS support after confirmation. |
| **2. Connect storage** | Guides local storage or a dedicated NFS export, reviews a new persistent guest mount when needed, then verifies the media identity and prepares the data tree. |
| **3. Set up media** | Jellyfin, Seerr, Sonarr, Radarr, Prowlarr and Bazarr, with optional SABnzbd. Enrolls administrator and separate everyday viewer access, saves private API keys, enables hardlinks, selects the owned Jellyfin libraries in Seerr and connects the apps. Usenet setup tests verified TLS and NNTP authentication in the terminal. |
| **4. Set up VPN torrents** | qBittorrent behind Gluetun, protocol-specific hidden credential prompts, normal magnet entry and application connections. |
| **5. Set up management** | Dockge and Uptime Kuma, with optional Homarr. Account creation and a delivered notification still need your input. |
| **6. Add photos or music** | Choose Immich or Navidrome/slskd, with optional Music Assistant. Add these when you can finish their account, backup or playback checks. |

Run `./setup 3` to open media directly, or `./setup 4` to return to VPN torrents. These are **guided menu numbers**; the advanced engine has separate stage IDs. The installer handles that mapping.

For an existing installation, `./setup --hardlinks` checks and offers to enable only Sonarr/Radarr's hardlink settings. It does not deploy containers, scan libraries or move media. A standalone repair for older source copies is documented in [Install and repair](docs/INSTALL.md).

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

The simple torrent stack has no ClamAV or approval queue. Files remain untrusted after downloading. This project does not change security controls on another existing installation.

MediaBot is a [separate optional project](https://github.com/SamarieSystems/mediabot), with an [installer audit](docs/MEDIABOT-AUDIT.md). Home Assistant and local LLMs are not prerequisites.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 scripts/check-release.py .
python3 scripts/validate-compose.py
```

Compose validation never starts containers. Runtime, VPN interruption/recovery, real playback and photo restore need the [acceptance checks](docs/ACCEPTANCE.md). GPL-3.0-only; vendor images retain their own licenses.
