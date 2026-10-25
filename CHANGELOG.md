# Changelog

## 0.1.0-alpha.3 - 2026-10-25

- Add `./setup`: a guided six-step menu with direct `./setup NUMBER` resume.
  It keeps reviewed plans, hashes and private diagnostic reports internally;
  operators approve a readable description instead of copying CLI parameters.
- Discover the guest LAN address and timezone; save CPU-only, torrents-first
  choices once and reuse existing configuration, credentials and appdata.
- Connect a selected NFS export with one reviewed persistent mount and private
  fstab backup. Refuse conflicting/occupied mountpoints; retain NAS ACLs and data.
- Guide application enrollment with actual LAN URLs, hidden credential prompts,
  authenticated API-key checks before saving, and deliberate backed-up typo
  correction. Pending enrollment remains resumable without redeploying apps.
- Verify guarded systemd startup before accepting a healthy-container resume;
  keep interrupted progress paused and isolate progress by configuration file.
- Add read-only bootstrap runtime verification and keep long operations visible
  with periodic progress messages. Existing host preparation can resume without
  package installation or Docker restart.
- Rewrite the quickstart around the guided installer. The previous individual
  commands remain available in `docs/ADVANCED.md`.

NAS accounts/export permissions, provider accounts and application first-user
logins still belong to the operator. This interface does not format disks,
migrate existing collections, expose public services or waive live verification.

## 0.1.0-alpha.2 - 2026-10-24

- Save each stage's startup-guard configuration beside its own Compose file.
  Applying management with different storage settings no longer changes the
  guard configuration used by an existing media stage. Existing alpha 1 units
  retain their legacy config until deliberately reapplied.
- Add a deployment-night walkthrough covering existing pooled disks,
  torrents-only installations, hardware/access intake and manual migration work.
- Correct bootstrap retry examples to use sudo for private receipts, and require
  identical mount identity across guests sharing a storage marker.
- Add regressions for cross-stage isolation and disk-full atomic replacement.

For existing alpha 1 installations, copy this release into a separate source
directory, retain site configuration/appdata/secrets/image locks, and generate
new reviewed plans for application stages you want to reapply. Do not erase
working data or copy a plan from the previous version.

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
