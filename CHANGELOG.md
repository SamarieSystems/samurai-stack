# Changelog

## 0.2.4 - 2026-10-26

- Use service-first display labels qualified by the chosen Compose project,
  such as `Jellyfin | home-picturehouse` and `qBittorrent | home-courier`.
  Retain the existing deployment UUID, project identities, storage paths,
  image locks and generated systemd unit names.
- Review existing Kuma monitor label changes against their original IDs,
  names and listener settings. Relabel through its supported authenticated
  edit operation, preserving history, tags, notification links and owner
  settings. Read-only verification accepts exact legacy aliases without
  renaming them; ambiguous or changed targets are refused.
- Relabel exact legacy Starr/Prowlarr/Seerr connections and native Jellyfin
  refresh entries in place during configuration. Keep IDs and unrelated fields,
  including enabled/default choices. Retain custom Jellyfin library names.
- Refresh Seerr's selected-profile display cache from its existing profile ID.
  Omit the read-only ID from Seerr update bodies while retaining the original
  URL ID and verifying the complete saved collection, including zero-ID servers.
- Give reviewed profile/custom-format labels the same project-qualified form,
  retaining IDs, scores, title assignments and media policy. Name new SAB NNTP
  providers by their hostname in the UI while keeping their private server key.
- Retain the complete offline user handbook and document how display names
  differ from operational identities. Existing media, accounts, provider
  credentials, download policy and VPN safeguards are preserved.

## User handbook supplement for 0.2.3 - 2026-10-25

- Add a plain-language, task-based handbook for everyday viewers and first-time
  owners, including a new-server walkthrough, file retrieval, imports, service
  operation, troubleshooting, backups and a fillable handover checklist.
- Build a searchable offline HTML/ZIP manual with inline technical references,
  printable user-only/full-manual scopes, verified internal links and checksums.
  The standard-library builder runs as a documentation gate in CI.
- Correct older management enrollment instructions and document optional
  persistent private Tailscale forwards. This supplement changes documentation
  and its packaging only; the v0.2.3 installer assets remain unchanged.

## 0.2.3 - 2026-10-25

- Include `--upgrade-storage-identity` and `--capacity` in the shared source and
  portable Bash help, making the existing supported maintenance actions visible
  before setup. Runtime deployment and recovery behavior is unchanged from the
  fully tested 0.2.2 implementation.
- Retain the immutable v0.2.2 tag after its final artifact check caught the help
  omission before release publication. Supersede it with this patch rather than
  repointing the tag. Preserve the measured runtime validation below and recheck
  the exact new packaged entry points.

## 0.2.2 - 2026-10-25

- Fix a reboot regression where ordinary Linux block-device renumbering made
  a valid media filesystem look like a replacement disk. New local block-backed
  storage records its filesystem UUID, mountpoint, filesystem type and Btrfs
  subvolume where applicable. Missing, read-only or different storage still
  fails closed before application startup.
- Add an explicit reviewed upgrade for existing local installations. Confirm
  the original media/application filesystem UUIDs, retain deployment markers
  and application inodes, and update current configuration, dependent receipts
  and recognized guards together with private original-content backups. Media,
  account databases, credentials and VPN settings are retained.
- Bind new account and management-database receipts to a persistent filesystem
  UUID/export identity plus the original inode. Keep strict transaction-time device/inode
  checks and require deliberate review for legacy receipts. Verify a recreated
  owned Bazarr container through its image, mounts, providers and persistent
  application directory instead of treating its old container ID as permanent.
- Keep guarded systemd failure retries running at a 15-second interval instead
  of permanently exhausting a three-attempt limit. Propagate Docker restart
  jobs to generated stack services, while explicit stack stops stay stopped.
  Recognize both historical and current exact generated units in recovery and
  installed-project checks.
- Wait for the configured LAN address before launch and the default route before
  VPN launch. Add bounded owned-container startup completion checks so a hung
  Compose supervisor with exited containers fails and retries through the
  original storage/network guards instead of appearing successfully started.
- Verify actual Docker port publications and bridge endpoints in addition to
  running/health state. Recreate containers from the same pinned recipe and
  retained bind data on a whole-stack start, so stale detached namespaces from
  a previous failed launch do not leave apparently healthy but unreachable apps.
- Permit reviewed replacement of the exact released v0.2.1 capacity watcher
  after proving its existing owned policy and generated units. Bind old/new
  source hashes, keep a private source backup and retain unknown local edits.
- Supersede the immutable v0.2.1 release after its actual reboot regression;
  retain earlier tags and validation history instead of rewriting them.

## 0.2.1 - 2026-10-25

- Correct a management reservation test that assumed temporary application
  directories belong to UID 1000. GitHub's Ubuntu Python 3.11 runner uses UID
  1001, which correctly failed the fixture's ownership proof. The test now
  matches its intended media identity to its actual temporary-directory owner.
- Add an explicit UID 1001 regression proving that a mismatched intended owner
  is still rejected. Production account, directory and VPN safeguards are
  unchanged; the runtime commissioning recorded for 0.2.0 still applies.
- Hold the existing `v0.2.0` tag and draft before release publication after that
  hosted CI failure. Keep the tag immutable and supersede it with this patch;
  do not present the canceled/unobserved CI matrix entries as successful.

## 0.2.0 - 2026-10-25

- Complete the supported staged Debian media-server installer with consistent
  Bash operator entry points and a standard-library Python backend. Keep trusted
  configuration outside the downloaded source and preserve application/media
  identities across updates, interrupted setup and reviewed recovery.
- Test existing Prowlarr sources and report actual protocol/category coverage
  separately from container health. Offer private, additive NZBGeek, Newznab and
  Torznab intake with source tests and reviewed plans. Missing or failed sources
  remain pending; no subscriptions or provider accounts are supplied.
- Offer reviewed future-import Sonarr/Radarr naming with Jellyfin TVDb/TMDb
  folder IDs and release metadata in filenames. Back up original naming policy
  and verify exact readback; do not rename or move existing media.
- Add optional native free-space floors plus an owned qBittorrent pause watcher
  for both media and application storage. Preserve seeders, pause only selected
  incomplete downloads and never automatically resume, delete data or relax VPN
  safeguards. Reserves are thresholds, not disk quotas.
- Add optional English subtitle policy for future additions without launching
  whole-library backfill. Use the pinned Bazarr runtime's stored profile-item
  contract and narrowly repair only proven installer-owned legacy profile items.
  Keep other profiles/settings and save a full before snapshot. Provider
  registration is distinct from a successful subtitle download.
- Automate proven-new Dockge/Kuma first administrators and additive private TCP
  monitors. Verify real logins, incorrect-password rejection and exact project/
  monitor readback. Resume saved first-run intent through bounded Kuma startup
  races. Existing owners must authenticate; no passwords are reset or extra
  administrators inserted. Notification delivery remains operator-configured.
- Preserve custom Jellyfin/Seerr library display names through canonical path,
  type and ID checks. Verify both Bazarr Starr connections completely. Wire
  native Starr-to-Jellyfin library refresh notifications without user-facing
  messages; new libraries enable normal metadata providers explicitly.
- Retain the earlier RC's hardlink, restored-account, Compose-name immutability,
  exact owned-service recovery, authenticated VPN and source-package safeguards.
  Validate actual disposable first-run management and packaged entry points as
  well as unit tests; keep target-machine commissioning separate from parser
  and container checks.

The supported core is a private Linux guest with CPU playback, mounted Linux
storage, the documented media applications, optional Usenet/VPN torrents and
management. Photos/music have separate onboarding and acceptance. Hardware
passthrough, public exposure, automatic collection migration, arbitrary provider
support and line-rate throughput are not implied. See [validation](docs/VALIDATION.md).

## 0.2.0-rc.3 - 2026-10-25

- Include the naming dependency in the standalone hardlink repair payload.
  RC2's main installer worked, but its focused `samurai-hardlinks.pyz` could
  not start. Keep the published RC2 tag unchanged and supersede it here.
- Exercise the actual packaged entry points under isolated Python from an
  empty directory, so archive-content checks cannot hide missing imports.

## 0.2.0-rc.2 - 2026-10-25

- Ask new guided installations for meaningful Compose project names shown in
  Dockge. Use the same identity in generated Compose files, stage directories,
  systemd commands, ownership checks, account wiring and recovery. Existing
  numbered projects retain their identity; changing an installed name is refused.
- Document private remote access to applications bound to a VM's LAN address,
  including SSH browser forwarding and the distinction between direct Tailscale
  node access and advertised subnet routes.

## 0.2.0-rc.1 - 2026-10-25

- Standardize operator entry points on Bash: source `./setup` and `./stack`,
  plus a self-contained downloadable `samurai-setup.sh`. Python remains the
  dependency-light implementation behind the launchers. Source and portable
  paths share preflight/help code, preserve arguments and working directory,
  and isolate Python imports from the caller's environment.
- Keep read-only recovery usable without a TTY, while credential enrollment
  and installation still require an interactive terminal in the intended VM.
  Validate the portable payload before private extraction and clean up only
  its owned temporary directory.
- Add reviewed, optional original-audio HD profiles using live Sonarr/Radarr
  schemas. Offer them during fresh media enrollment and through menu action p.
  New Seerr connections prefer the managed profile. Existing titles, accounts
  and Seerr defaults are retained. Shared size limits require a separate review.
- Clear incompatible provider-assigned WireGuard addresses when changing the
  selected VPN provider before torrent deployment. Preserve the old private
  configuration and keep the qBittorrent tunnel binding and fail-closed guard.
- Add an explicit reviewed account-directory reauthorization after a real
  application backup restore. Verify existing accounts and server identities,
  keep the original receipt and remove first-user enrollment authority from
  restored entries. Ordinary setup still refuses replaced or empty directories.
- Carry forward all alpha recovery, outside-source configuration, blank fresh
  setup, hardlink verification, hidden media-account enrollment and automatic
  app/provider wiring fixes. Refresh installation instructions around the
  same numbered Bash workflow.

This candidate is for the documented Debian CPU-only deployment path. Actual
provider credentials, ports, playback clients and notification destinations
still require commissioning on the intended installation. See VALIDATION.md
and ROADMAP.md for measured evidence and remaining release gates.

## 0.1.0-alpha.7 - 2026-10-25

- Add `./setup --fresh` for an intentional application reset. Open a new blank
  phases 1-6 wizard without an old `site.json`, credentials or setup cache.
  Phase 1 checks existing OS prerequisites rather than reinstalling working
  packages; storage selection and new account prompts follow normally.
- Retire old installer configuration and exact generated systemd units only
  after proving their application roots are absent or empty and their projects
  and installation UUIDs have zero containers, including stopped ones. Preserve
  originals in private archives. Old progress is retained without reuse;
  existing appdata blocks the fresh-start path.
- Keep the new session outside the source directory. Normal setup resumes it
  before storage registration and uses its canonical configuration afterward;
  running `--fresh` again deliberately opens another blank session.
- Leave mounted filesystems, storage markers and existing media untouched by
  fresh-start cleanup. New storage uses a new empty directory; no formatting,
  media migration, credential reuse or application-data overwrite is performed.

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
