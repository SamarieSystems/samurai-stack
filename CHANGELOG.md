# Changelog

## 0.1.0-alpha.1 - 2026-10-24

First public alpha of the staged homelab deployment toolkit.

- Reviewed Proxmox guest provisioning and a separate Debian/Ubuntu package bootstrap.
- Additive local/NFS storage preparation with ownership markers, UID/GID probes, hardlinks and startup mount guards.
- Connected Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd, Jellyfin and Seerr.
- Authenticated qBittorrent behind Gluetun, direct magnet entry and optional Proton forwarded-port synchronization.
- Dockge, Uptime Kuma, optional Homarr, Immich, Navidrome, slskd and optional Music Assistant stages.
- Private credentials, immutable image references, explicit plan acceptance, retry receipts and fail-closed verification.
- A complete operator quickstart, recovery and security notes, regression checks and disposable commissioning tools.

The fresh-VM run exercised real account enrollment, application connections,
media playback, torrent transfer and tunnel failure, storage-loss startup
guards, and a separate photo restore. See the dated [validation report](docs/VALIDATION.md)
for exact evidence and remaining hardware/provider/manual requirements.

This alpha targets new dedicated installations. It does not automate physical
disk installation, storage migration, GPU driver binding or ongoing backups.
