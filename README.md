# Samurai Stack

[![Checks](https://github.com/SamarieSystems/samurai-stack/actions/workflows/checks.yml/badge.svg)](https://github.com/SamarieSystems/samurai-stack/actions/workflows/checks.yml)
[GPL-3.0-only](LICENSE) | [Releases](https://github.com/SamarieSystems/samurai-stack/releases) | [Changelog](CHANGELOG.md)

A repeatable path from a new Linux guest to a private media server. Review a plan, apply one stage, connect its applications, then verify before adding more.

**0.1.0 alpha 2.** Commissioned on a fresh, disposable Debian 13 VM with CPU-only applications and synthetic data. The [validation report](docs/VALIDATION.md) separates live results from the hardware, commercial-provider and manual enrollment checks still required on your machine. Start with test storage and follow the [acceptance checklist](docs/ACCEPTANCE.md) before trusting a new installation with personal files.

**New installation:** gather the [deployment-night intake](docs/DEPLOYMENT-NIGHT.md), then follow the [Quickstart](docs/QUICKSTART.md). They cover hardware, credentials, manual work, configuration, storage, media, VPN torrents and first-use checks. Read [Security and data boundaries](docs/SECURITY.md) before using existing storage or exposing administrator interfaces.

Python 3.11+ and Docker Compose v2.24+; application stages target a Debian 12/13 or Ubuntu 24.04/26.04 systemd VM. CPU-only is supported. No Python packages are required. Proxmox and a separate NAS are optional once a supported Linux guest already exists.

## The stages

| Stage | What it does | Finish line |
| --- | --- | --- |
| 0, storage | Verify the exact existing local filesystem or NFS export; create a dedicated directory tree; test media UID/GID access and download-to-library hardlinks | Expected mount, marker, directories and identity permissions verified |
| 1, foundation | Inspect an already installed Proxmox host; plan and provision explicitly selected new VMs from a checksum-pinned cloud image | VM definitions/disks verified; guest readiness is a separate check |
| 2, media | Sonarr, Radarr, Prowlarr, Bazarr, optional SABnzbd, Jellyfin and Seerr | Account enrollment, API connection tests and persisted library/client settings verified |
| 3, torrents | qBittorrent sharing Gluetun's VPN namespace; authenticated Web UI; ordinary magnet entry; optional Proton forwarded-port synchronization | Bound interface, namespace, limits, categories, forwarding and Starr connections verified |
| 4, management | Dockge and Uptime Kuma; optional Homarr with a restricted socket proxy | Working UI accounts, monitors and a delivered test alert, recorded as manual checks |
| 5, photos | Immich with matching server/ML versions and local PostgreSQL/Valkey storage | API readiness, first administrator, plus the photo/backup acceptance checks |
| 6, music | Navidrome and slskd; optional Music Assistant | Actual playback, accounts and provider/player setup, recorded as manual checks |

Stage 4 works without the NAS and can run on any selected VM role. Kuma can monitor the other hosts centrally; Dockge controls the local Docker daemon on the host where it runs. Apply stage 4 on another guest if you want a local Dockge there too. A compact deployment can put the applications together; the installer never assumes a GPU exists. A physical GPU passed through normally belongs to one VM, so plan which workloads share that guest.

MediaBot stays a [separate optional project](https://github.com/SamarieSystems/mediabot). Its installer is audited in [MEDIABOT-AUDIT.md](docs/MEDIABOT-AUDIT.md). This toolkit does not install the local LLM or change an existing bot deployment.

## Start here

1. If using Proxmox, install it yourself onto the disk you deliberately selected. That installer can erase that disk. This project never launches it. See [PROXMOX.md](docs/PROXMOX.md) for inspected, planned guest provisioning and GPU assessment.
2. Inside the new guest, use [HOST-BOOTSTRAP.md](docs/HOST-BOOTSTRAP.md) to install Docker/Compose and the guest/NFS prerequisites from Docker's official repository. It has its own read-only plan and explicit apply.
3. Set up and mount storage using [STORAGE.md](docs/STORAGE.md). Create NAS users and exports in that vendor's interface. The toolkit will not guess which existing NAS account or export to change.
4. In this checkout, run `python3 stack init`. It asks for addresses, paths, UID/GID, service roles, features and VPN type. It creates a private `site.json`; it does not touch storage or start services. Use `python3 stack --config site.json validate` after edits.

Run all application commands **inside the intended guest**, with the checkout available locally. Windows can prepare configuration and run tests, but cannot apply Linux service stages. Use a unique deployment name if other Compose projects already exist.

For stage 1, create the site configuration on the installed Proxmox host, then run `sudo python3 stack --config site.json proxmox init`. The foundation wizard inventories CPU/RAM, lists existing bridges/storage and asks for guest IDs, resources, a pinned cloud image and an SSH public key. It records configuration only. `proxmox plan` then validates capacity, ownership and the exact image before any guest is created. GPU assessment is included in inventory; host driver binding and passthrough remain explicit manual steps in this alpha.

## One stage at a time

For storage:

```bash
sudo python3 stack --config site.json plan 0 --out plan-0.json
# Read plan-0.json. Copy its review_hash into the next command.
sudo python3 stack --config site.json apply 0 --plan plan-0.json --accept-plan REVIEW_HASH
sudo python3 stack --config site.json verify 0
```

Then media:

```bash
sudo python3 stack --config site.json plan 2 --out plan-2.json
sudo python3 stack --config site.json apply 2 --plan plan-2.json --accept-plan REVIEW_HASH
sudo python3 stack --config site.json access 2
```

Open the printed private LAN addresses. Create the Jellyfin and Seerr administrators, connect Seerr to Jellyfin, and complete each application's required first-run authentication. Enter your Usenet/indexer/subtitle-provider accounts in their own UIs; no paid credentials or content providers are supplied here.

Store the requested API keys without putting values in command arguments:

```bash
sudo python3 stack --config site.json secret jellyfin_api_key
sudo python3 stack --config site.json secret seerr_api_key
sudo python3 stack --config site.json secret bazarr_api_key
sudo python3 stack --config site.json configure 2
sudo python3 stack --config site.json verify 2
```

Sonarr/Radarr/Prowlarr keys are read from their own local configuration after first boot. SABnzbd's key is read from its local INI. Missing keys produce specific enrollment tasks, never a fabricated successful setup. On a separate torrent VM, import the media apps' keys privately so it can register the download clients. See [SERVICES.md](docs/SERVICES.md) for exact enrollment, VPN credentials, paths and priority settings.

Repeat `plan`, `apply`, `configure`, `verify` for the next selected stage. An interrupted or failed apply preserves files, volumes and partial provisioning. Generate a fresh plan with a new output filename before retrying. Existing plan files are never overwritten silently. `plan --offline` is useful for reviewing generated Compose on another machine, but cannot be applied.

For management and music, APIs do not prove all the first-run GUI/provider steps. After actually doing them, run `sudo python3 stack --config site.json attest 4` (or `6`), review the checklist and confirm it. Verification records **verified_with_manual_checks**, separately from automated API verification.

## Boundaries that matter

- No disk formatting, partitioning, RAID creation, data migration, automatic media moves, recursive permission repair, public DNS or router/public-exposure configuration. Docker creates its ordinary container bridge and firewall rules during guest bootstrap.
- A nonempty, unowned data root is refused. An explicitly reviewed `allow_existing_contents` setting permits additive directories only; it never moves or renames existing files.
- Database/configuration storage stays on the guest's local filesystem. Downloads and libraries share one `/data` tree. Jellyfin, Navidrome and external Immich library mounts are read-only.
- Every apply is tied to an explicit plan hash, host identity and configuration. Another host, configuration change or previous Compose change requires another plan.
- The release catalog pins immutable image indexes, so a new installation gets the reviewed builds too. Local image locks preserve those references on retries. See [image provenance](docs/IMAGES.md). Updating is deliberately not automated in this release.
- Credentials live in private files, not example configuration or plan output. Dockge's Docker socket access is effectively host administration; keep it private and authenticated.
- Generated stacks use systemd with exact mount validation before startup. Docker's independent container auto-restart is disabled for these stacks. Use the named systemd unit after a host reboot or recovery. Dockge remains useful for logs, terminals and individual service actions; see the interaction notes in [RECOVERY.md](docs/RECOVERY.md).
- Antivirus scanning is not part of the default torrent stage. Files are untrusted regardless. This toolkit does not disable scanning or approval safeguards on another existing installation.

Exit codes: `0` completed operation, `1` safety/prerequisite failure, `2` pending enrollment or application configuration failure, `130` interrupted. A stage marked `started` is not `verified`.

## Development and release checks

```bash
python3 -m unittest discover -s tests -v
python3 scripts/check-release.py .
python3 scripts/validate-compose.py
```

The last command validates rendered scenarios with the real Compose parser. It never starts containers. First-use download, VPN interruption/recovery, playback/transcoding, photo restore and cold-boot tests remain commissioning checks. See [ACCEPTANCE.md](docs/ACCEPTANCE.md), [RECOVERY.md](docs/RECOVERY.md) and [SOURCES.md](docs/SOURCES.md).

There is also an explicit optional first-boot qBit smoke test:

```bash
python3 scripts/smoke-qbit.py --image YOUR_EXISTING_LOCAL_QBIT_IMAGE --acknowledge-temporary-container
```

It uses a new temporary configuration, a random test credential, `--network none`, no host ports, bounded CPU/RAM and no personal-data mounts. It never pulls an image. It confirms authentication and initial interface settings, then removes only its labeled temporary container. It does not prove VPN behavior or real torrent transfer.

`scripts/smoke-storage.py --media-uid ID --media-gid ID --acknowledge-temporary-directories` performs real repeated stage-0 preparation entirely beneath a new temporary directory, verifies sentinel preservation and refuses a simulated missing NFS mount. It can run as that media identity; testing a different UID/GID needs sudo. Application/systemd stages always require sudo.

GPL-3.0-only. Vendor images retain their own licenses. This source checkout is the supported distribution format; do not `pip install` an incomplete directory without the companion scripts.
