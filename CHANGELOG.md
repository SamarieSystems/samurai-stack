# Changelog

## 0.1.0-alpha.6 - 2026-10-25

- Save verified installation configuration and guided progress outside the
  source checkout. Reuse trusted registered/runtime configuration before
  opening a new wizard; preserve the original identity when updating a ZIP.
  Keep explicit private `--config` paths compatible with older releases.
- Add `./setup --recover` for read-only container, systemd, mount and saved-path
  evidence. Explain project/UUID conflicts before application or image writes.
  Refuse recreation when existing containers' appdata paths are missing,
  replaced or unsafe. This is protection against further loss, not database
  recovery after deletion.
- Archive an obsolete generated unit only after proving both its Compose
  project and old installation UUID have no containers, including stopped
  ones. Stop and disable only that exact unit, repeat the absence checks and
  preserve its original bytes in a private archive. Refuse custom commands,
  drop-ins and stale loaded configuration. Clear restart limits only for the
  intended generated unit.
- Show suitable already-mounted local filesystems during storage selection.
  Add `--choose-storage` for correcting an empty, unregistered local choice on
  the VM root filesystem, with a private original-config backup. Do not repoint
  existing applications, adopt another tree, move media or format a disk.
  Require an explicit saved site for this correction and reject matching
  services that are active, restarting or queued, even when Docker is empty.
- Point missing-storage-marker failures to guided step 2 instead of exposing
  the engine's different stage numbering without an actionable command.

## 0.1.0-alpha.5 - 2026-10-25

- Complete guided media account enrollment for Jellyfin, Sonarr, Radarr,
  Prowlarr, Bazarr, Seerr and optional SABnzbd. Hidden administrator/viewer
  prompts replace browser first-user wizards and API-key copying on the
  supported path. Seerr uses the Jellyfin administrator and restricted viewer.
- Connect the media applications automatically and enable only the two
  deployment-named, path/ID-verified Jellyfin libraries in Seerr. Preserve
  unrelated library selections without starting scans or requesting media.
- Configure an NNTP provider through terminal prompts, verify its TLS
  certificate and actual authentication, and complete SABnzbd's wizard. Test
  existing configured providers while preserving their settings and credentials.
- Resume older unconfigured alphas using app-specific proof that no configured
  account exists, including read-only active-database checks where needed.
  Preserve existing owner passwords, account policies and installation identity.
- Package `samurai-setup.pyz` for existing source ZIP installations. It uses
  the saved site configuration and application state without requiring Git or
  overwriting the old source tree. Keep the focused hardlink repair available.

## 0.1.0-alpha.4 - 2026-10-25

- Enable Sonarr/Radarr `copyUsingHardlinks` during application configuration and
  verify the exact API field by readback. Add focused `hardlinks --apply` and
  `./setup --hardlinks` maintenance without deployment, restarts or media access.
- Package a standalone hardlink repair for existing ZIP installations. It uses
  the original private site configuration and verifies local application ownership.
- Reserve fresh application directories before their first start. Offer hidden
  administrator/viewer credential prompts and enroll Jellyfin, Sonarr, Radarr
  and Prowlarr through their APIs. Test actual logins and deny anonymous access;
  give Jellyfin's everyday viewer an explicitly restricted policy. Preserve
  existing accounts and exact saved credentials across retries. Other first-run
  screens and provider choices remain explicit manual work.

- Identify active installation-DVD APT sources before host preparation and
  offer a separate reviewed, default-no repair that disables only those entries
  while keeping online OS mirrors and private originals. Replan before package
  approval. Unknown repository failures never trigger automatic edits.
- Retain bounded private command output, including APT failures and dependency
  simulations. Guided errors explain known refusals and point directly to the
  readable command log instead of hiding the cause behind a generic failure.
- Give a newly created Docker keyring directory the traversal permissions APT
  needs even under the bootstrap's private umask; preserve existing directories.
- Document path conventions, the common container `/data` tree, focused repair,
  and concrete acceptance gates for the 0.2 milestone.

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
