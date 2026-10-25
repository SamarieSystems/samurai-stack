# Release validation

The dated sections record measured results for releases and their candidates. They
do not claim that every provider, device or deployment has been tested.

## 0.2.1: Hosted-runner UID fixture correction

October 25, 2026. Hosted Python 3.11 CI exposed a management reservation fixture
whose temporary directory belonged to Ubuntu runner UID 1001 while its intended
media identity remained the default UID 1000. The production ownership check
correctly refused that mismatch. Publication of the `v0.2.0` draft was held;
the existing tag is retained unchanged and superseded by 0.2.1. Other canceled
or unobserved hosted matrix results are not counted as passes.

Only the test fixture and release metadata/documentation changed. The fixture
uses its actual nonroot directory owner as its intended PUID. A separate
regression simulates owner UID 1001 and proves that intended UID 1000 is still
refused while intended UID 1001 is accepted. Runtime ownership constraints,
application code, provider/VPN safeguards and the commissioning results below
are unchanged.

The full 944-test suite then passed in the isolated Debian guest as root
(14 explicit skips) and as an actual UID/GID 1001 process (13 skips). That
process used a private copied cached Node binary without a sudo grant, new
login account or global runtime installation. All 21 Compose parser scenarios
passed, and the 137-file source audit reported zero findings. This closes the
local UID regression; hosted Python-version CI remains a distinct publication
gate rather than an assumed result.

## 0.2.0: Integrated installer and assisted commissioning

October 25, 2026. Earlier candidate evidence is retained below rather than
rewritten as broader proof. The supported path remains a private Linux guest.

The complete 943-test suite passed on Windows with 45 explicit skips, on the
Debian guest as root with 14 skips, and as an ordinary account with 13 skips.
The offline guest lacks Git for eleven cases; other skips depend on platform,
runtime or the account needed for a permission/elevation case. Both Linux runs
used an isolated cached Node runtime for Socket.IO protocol tests. All 21 actual
Compose parser scenarios passed without starting containers. The public source
audit had zero findings; the guest scan included one retained private test helper
in addition to the 137 public files. Initial ordinary-account fixture failures
were fixed by isolating management discovery and temporary-file ownership proof
from the real host's private directories.

- A new management deployment in the isolated CPU-only Debian 13 guest exercised
  the public engine's pre-start freshness reservation and actual Dockge/Kuma
  first-owner setup. Correct passwords authenticated, incorrect passwords were
  rejected, Dockge discovered its managed project and Kuma returned eight named
  TCP-listener monitors. Retry kept account/monitor IDs and private credential
  bytes unchanged. Independent engine verification passed. An unused media
  directory stayed absent. The exact test service was stopped and disabled.
- The initial actual run caught a Kuma listener race after its database became
  visible. The fix retries only bounded readiness reads, waits for in-progress
  setup, rejects unknown schemas and never retries the setup write. The retained
  partial attempt resumed without replacing its Dockge owner. A separate fresh
  deployment passed the full path. A private harness assertion was corrected
  to expect `configured` from configure and `verified` from verification.
- Source intake, future-import naming, capacity and subtitle policy have
  dedicated stateful tests and guided/CLI checks. Source authentication is
  separate from downloads. Naming writes only naming settings, never existing
  file/title APIs. Capacity stops only selected incomplete hashes and never
  resumes or deletes. Reserves are thresholds, not quotas.
- Maintainer commissioning on one assisted friend installation completed an
  official SAB test download of roughly 100 MiB, with repair/unpack and about
  9.5 MB/s during that bounded test. An actual Radarr completed-download import
  was recorded on the intended media filesystem. This is not a full bandwidth
  benchmark or evidence for arbitrary providers.
- The same commissioning interrupted the exact owned Proton tunnel while
  retaining its firewall. HTTPS egress failed without an address; the torrent
  safeguard closed the client binding during failure and restored `tun0` after
  recovery and a verified forwarded port. A harmless canary completed and
  uploaded to an interested peer. No all-provider or throughput claim is made.
- Actual Bazarr title assignment caught opaque profile JSON that a settings
  roundtrip accepted but the pinned runtime could not consume. Stored item keys
  now follow that runtime's snake_case contract. A reviewed, receipt-bound
  repair changes only the exact installer-owned legacy shape, preserves other
  profiles and saves a complete before snapshot. Actual title assignment then
  succeeded. Two bounded missing-English jobs were accepted; subtitle sidecar
  delivery is not claimed. Embedded-English media in the sampled set was kept.
- Native Starr-to-Jellyfin library-refresh notifications passed actual backend
  tests and saved readback with shared paths and user-facing messages disabled.
  New libraries explicitly enable normal metadata providers; existing library
  provider preferences are retained. A live naming review also caught Radarr's
  string enum versus Sonarr's integer field; both representations are now
  preserved rather than coercing the existing colon-replacement policy.

This is one assisted rollout plus bounded disposable evidence. Existing
management owners with unknown credentials still need their current login;
the installer does not reset them or insert another administrator. Notification
delivery, best-effort subtitle providers, arbitrary clients, other VPNs,
sustained load and broader independent hardware qualification remain separate.

## 0.2.0-rc.3: Standalone repair packaging

October 25, 2026. Verification of RC2's actual downloaded assets caught a
missing dependency in the standalone hardlink repair payload. The full setup
payload worked, but `samurai-hardlinks.pyz --help` failed to import `naming`.
The immutable RC2 tag is retained; use RC3 for new downloads.

The focused builder now includes that module. New tests build the actual
payload, run it with isolated Python from an empty working directory and assert
that its imports come from the archive. A read-only custom-project hardlink
verification uses real private fixture files with injected Docker/HTTP
boundaries; no live applications or media are involved. The release-builder
test also launches both generated Python entry points rather than checking
archive contents alone. All 753 Windows tests completed successfully with 44
platform/runtime skips; the public source audit scanned 117 files with zero
findings. The live naming and Linux functional evidence below remains unchanged.

## 0.2.0-rc.2: Compose naming and private remote access

October 25, 2026. The same isolated Debian 13 disposable VM tested a new custom
management project, using previously inspected immutable image-cache entries.
No household services or friend installation were changed by these tests.

- The actual generated systemd unit retained its numeric stage ID while
  Compose, the stage directory and container labels used `friend-tools`.
  Authenticated Dockge discovery returned exactly that managed project and
  both healthy services. Repeat apply retained the container IDs and start
  timestamps. A subsequent attempted rename was refused without changing
  containers or the saved stage configuration. The test services were stopped
  and disabled afterward; their application data and containers were retained.
- The naming review found and fixed unit-only recovery inventing a numbered
  project, a fresh-cleanup collision with another stage's old numbered default,
  and an advanced-CLI rename when runtime snapshots were missing. Canonical
  identity, exact owned systemd directives and runtime snapshots are checked
  independently. Legacy absent/empty name maps retain their exact registry
  identity. Unreadable identity fails closed with an actionable sudo message.
- All 749 tests completed successfully on Windows with 44 platform/runtime
  skips. In Linux, the root run had 13 explicit skips and the ordinary-account
  run had 12. Eleven guest skips need Git, which the offline guest lacks; the
  remaining skips depend on which account can execute the permission/elevation
  case. The real ordinary-user permission-denial regression ran successfully.
  The first Linux runs exposed test fixtures reading the lab's real private
  registry; those fixtures now use isolated temporary identity roots.
- All 21 real Compose parser scenarios passed, including custom-named media,
  torrent, management, photo and music sets. The public source hygiene audit
  scanned 116 files with zero findings. The private guest had one additional
  retained test helper, which was not part of the public release source.

The private remote-access guide describes LAN-bound application ports and SSH
forwarding over Tailscale. No automatic public exposure or migration of existing
project names was added. The commercial-provider and independent commissioning
gates below still apply to final 0.2.0.

## 0.2.0-rc.1

October 25, 2026. The core acceptance work runs inside the same owned,
isolated CPU-only Debian 13 VM, with 2 vCPUs and 3 GiB RAM. Existing household
services and the friend's installation are outside this test environment.

- The source and portable Bash entry points passed real Linux terminal runs
  for help/version and identical read-only recovery JSON. Focused tests cover
  numbered steps, fresh mode, paths with spaces, argument and working-directory
  preservation, exit status, hostile Python paths, corrupt payloads and private
  temporary-directory cleanup. Ordinary-user elevation invokes Bash explicitly
  so a downloaded non-executable script works with the documented command.
- Actual pinned Sonarr and Radarr APIs accepted the named original-audio HD
  profiles and custom formats. Retry and readback passed. Profile-only setup
  preserved every global quality definition; a separately reviewed size apply
  changed exactly eight definitions per app. Live testing caught and fixed
  omitted quality-leaf IDs, the minimum upgrade-score validator and omitted
  nullable size limits. Unit tests bind reviewed plans to host and configuration
  identity and refuse conflicting existing resources before writes.
- Both real Starr importers handled an owned three-second H.264/AAC test video.
  Explicit ManualImport copy mode retained the download and created a library
  entry with matching device/inode and link count two. Move mode removed the
  source and retained the same device/inode in the library, with link count one.
  This verifies application path mapping, media UID access, hardlink imports and
  same-filesystem moves. It does not prove an indexer's release selection,
  unattended completed-download handling or commercial SAB delivery/history.
- The six enabled media applications were stopped consistently and their app
  state archived using PAX, then restored at the original application path.
  The original tree and backup were retained. All six applications verified
  after restore; private credential bytes, modes, owners and nanosecond mtimes
  matched, along with saved site bytes, managed profile IDs and size policy.
  Media bytes, inodes and metadata were unchanged. An initial ordinary-tar
  fixture rounded mtimes; that evidence was retained and the backup test was
  corrected instead of being counted as a pass.
- A subsequent installer-resume check identified that restored appdata has new
  directory inodes, while account reservations intentionally remember their
  original inodes. The new explicit restore-account review proved all six saved
  account/server identities, preserved the exact old receipt and updated only
  each changed entry's directory, eligibility and restore-verification marker.
  Credentials, account IDs/roles, application settings and container start times
  were unchanged. The strict ordinary guard then passed; a second restore plan
  had no changes, and verified restored accounts could not regain first-user
  creation authority. No general inode-check bypass was added to setup.
- After restore, the existing non-administrator Jellyfin viewer indexed and
  played that generated sample. HTTP 200 full-stream bytes matched its SHA256;
  HTTP 206 range bytes matched the requested range. The tokenless direct-stream
  behavior already described in [security boundaries](SECURITY.md) was
  reproduced too. This is playback proof, not a claim that every stream route
  enforces account authentication or that an arbitrary client can transcode.
- Two actual cold boots exercised storage startup. With the exact owned bind
  mount present, the six media apps and all 14 wiring checks passed; Dockge and
  Uptime Kuma returned HTTP 200. Without that mount, the generated guard refused
  before any media container started, no fallback tree appeared, and management
  remained available. Original fstab bytes/metadata, unit enablement and the live
  fixture mount were restored afterward. The old media sentinel and production
  VM configuration matched their baselines. Management used the previously
  verified immutable image cache, not a fresh registry download.
- All 720 Linux tests completed successfully as root and as the ordinary
  test account, with 12 explicit skips in each run. Eleven require Git, which
  the intentionally offline guest lacks. Root skips the ordinary-user elevation
  case; the ordinary account skips one root-only permission check. Node-backed
  tests ran through the owned, isolated test container. The source hygiene audit
  scanned 113 files with zero findings. Git-dependent builder tests also run in
  the Windows environment and hosted Linux CI; those results are separate from
  the offline guest count.
- Windows completed the expanded 720-test suite, with 42 platform/runtime skips.
  Hosted Ubuntu CI completed it on Python 3.11 and 3.13, with one root-only
  permission skip per matrix job. Those Linux jobs executed the Git-dependent
  release-builder tests and all 16 real Compose parser scenarios without
  starting application containers.
- MediaBot's separate public installer remains present at its existing stable
  revision. Its bounded installer suite was rerun in a private environment:
  32 passed and one Windows symlink case skipped. See the separate
  [installer audit](MEDIABOT-AUDIT.md); no live bot was started or modified.

The fixture uses owned ext4 bind mounts and generated videos. Official metadata
lookup used an allowlisted SSH proxy; app proxy settings were restored exactly
and no general guest WAN route was retained. Provider secrets and personal data
are excluded from public artifacts. A new commercial VPN profile, actual
subscriber download/import commissioning and independent-operator readback are
still separate [0.2 release gates](ROADMAP.md). The local VPN stress results
below do not establish those provider-specific results.

## Alpha 7 explicit fresh setup

October 25, 2026. The exact portable payload ran in a real root Linux
pseudo-terminal inside isolated disposable VM 102. The fixture had no source
site or application directory, but retained an enabled broken generated unit,
a canonical reservation for that missing app root and existing media.

- `--fresh` archived the obsolete unit byte-identically and preserved the old
  canonical configuration in a private archive. The wizard opened with all six
  phases marked not started and no inherited configuration or progress.
- The test quit before phase 1, then relaunched without arguments. The private
  fresh-session pointer resumed that same blank session without an old site
  path or another `--fresh` invocation.
- Phase 1 verified existing prerequisites without reinstalling the OS. Phase 2
  prompted for new choices, reused the intended application path with a new
  UUID, selected an owned mounted local filesystem and verified storage access
  and download/library hardlinks as the media UID/GID.
- Phase 3 prompted for new hidden administrator and everyday-viewer credentials,
  enrolled all six enabled media services, wired them and passed verification.
  No browser setup was required. The torrent-only fixture left SABnzbd off.
- Existing media bytes, inode, mode and mtime matched before and after. An
  unrelated custom isolation service and unrelated running container were
  unchanged. No disk was formatted; the media fixture used an owned ext4 bind
  mount. Old disposable container objects were removed only while preparing
  this controlled wipe fixture, with appdata, images and volumes retained.
- All 657 tests passed on Linux as root. The ordinary Linux test user passed
  656 with one root-only skip. Windows passed 623 with 34 platform/runtime
  skips. The release hygiene audit scanned 102 files with zero findings.

The media acceptance used existing exact pinned images in the isolated cache.
It does not prove registry download availability, a commercial VPN, provider
credentials, physical disk health, throughput or the friend's machine.
Phases 4-6 retain their earlier implementations and validation boundaries.
Fresh mode never uninstalls a running stack: surviving application contents,
Samurai containers including stopped ones, custom generated-name services or
unknown metadata block the archival operation instead.

## Alpha 6 source-independent state and clean-restart recovery

October 25, 2026. These checks ran in isolated disposable VM 102 with synthetic
accounts, owned test directories and restricted networking. They did not
modify the friend's machine or existing household services and personal data.

- A real enabled obsolete systemd unit with a foreign installation UUID and
  zero project containers was stopped, disabled and archived byte-identically.
  Fresh media setup then enrolled and verified all six enabled services:
  Jellyfin, Sonarr, Radarr, Prowlarr, Bazarr and Seerr. SABnzbd was disabled in
  this torrent-only fixture; its alpha 5 acceptance remains recorded below.
- The original source/config folder was renamed out of the way. Default
  discovery selected the private canonical site, retained its installation
  UUID and guided progress, and resumed media verification with zero input
  prompts or container restarts. Regression cases cover the initial migration
  of matching older checkout progress and portable execution from another
  extraction directory.
- Mounted-storage correction rejected an occupied default directory and
  selected a new empty child. It retained the UUID, old tree and a private
  original-config backup, then verified mount identity, access and real
  download/library hardlinks as the configured media UID/GID. The fixture used
  an owned ext4 bind mount inside the VM, not a formatted disk or a physical
  drive passthrough test. Existing-file bytes, inode, mode and mtime matched.
- The exact portable release payload passed actual root Linux pseudo-terminal
  runs for both existing media resume and the storage-correction CLI, including
  the normal storage apply/verify and canonical registry save. Its source site
  stayed byte-identical during media resume; no accounts were re-prompted and
  no existing containers restarted.
- Acceptance caught and fixed two directory compatibility cases: a private
  runtime site beneath a root-owned mode-755 stage directory, and private
  drafts beneath a root-owned mode-755 setup parent. Group/other writable
  parents remain refused, with no permission repair or chmod of existing
  directories. Canonical registry and host draft directories remain private.
- The regression suite checks stopped-container refusal, changed appdata
  inodes, custom units/drop-ins, loaded-unit mismatches, failed-state reset,
  queued/restarting service refusal during storage correction, host/config
  identity conflicts, interruption, private backups and unchanged old files.
- Linux passed all 632 tests with no skips. Windows ran 632 tests: 600 passed
  and 32 platform/runtime cases were skipped, with no failures. All 16 real
  Compose parser cases passed without starting containers.
- The same suite also passed as the ordinary Linux test user: 631 passed and
  one root-only permission case was skipped. This caught and corrected test
  fixtures that assumed root ownership or inspected default deployment paths;
  runtime ownership checks were not weakened.

These checks prove the bounded recovery paths, not deleted-database recovery,
physical storage health, a commercial VPN/provider connection, real download
throughput or playback on the friend's devices. Registered storage migration
and adoption of existing media are deliberately outside the fresh correction.

## Alpha 5 media enrollment and provider setup

October 25, 2026. Work used the isolated disposable VM and synthetic test
accounts. The friend's installation, existing household media services and
personal data were not modified by these tests.

- A new empty application root completed guided media setup for all seven
  services: Jellyfin, Sonarr, Radarr, Prowlarr, Bazarr, Seerr and SABnzbd. Hidden
  terminal prompts supplied the administrator/viewer and NNTP credentials;
  account enrollment, automatic wiring and readback verification passed without
  browser first-user wizards or API-key copying. Seerr's owner and everyday
  viewer use Jellyfin sign-in, with no separate local Seerr password.
- An owned NNTP fixture negotiated TLS 1.3 with strict certificate verification
  and actual service authentication. SAB's provider setup and wizard completion
  passed. A second guided run completed with zero input prompts and zero
  container restarts, reusing the saved account and provider configuration.
- The real Seerr API enabled the two managed Jellyfin movie/TV libraries after
  their names, paths, IDs and types were verified. The implementation uses
  supported metadata discovery and individual enabled-flag updates, without
  starting a content scan or requesting media. Regression tests check preserved
  custom selections and refuse discovery that would drop stale custom rows.
- A separate legacy simulation temporarily hid the original onboarding receipt
  and shared credential profile, then inspected all seven already-configured
  apps. All remained ineligible for new account enrollment and passed without
  prompts. Account IDs, password hashes and permission policies matched before
  and after. Its 38 read-only HTTP operations included five supported SAB POSTs
  for read-only `version`/`get_config` operations; there were zero account or
  application-configuration write attempts. Both original private files were
  restored with identical bytes, inodes, modes and modification times.
- The portable `samurai-setup.pyz` ran in an actual Linux root pseudo-terminal
  from a simulated older source ZIP installation. It used the original site
  configuration and completed media verification; the old source and site file
  remained byte-identical. No Git update was required.
- Linux passed all 507 tests with no skips. Windows ran the same 507-test suite:
  482 passed and 25 platform/runtime cases were skipped, with no failures.
  All 16 Compose cases passed the real `docker compose config --quiet` parser;
  those parser checks started no containers.

The fresh run caught Bazarr accepting TCP connections before its settings API
was ready. Enrollment now waits for the supported API readiness checks rather
than treating an open port as completed setup. Live acceptance passed after
that fix.

This proves the isolated guided media path and its preservation/resume checks.
It does not verify the friend's machine, a commercial NNTP subscription or
quota, an actual provider download/import, or a physical playback device.
Indexer accounts, subtitle providers/languages and quality profiles remain
operator choices. Complete the intended installation's
[acceptance checks](ACCEPTANCE.md) before handover; independent operator rollout
and commercial-provider checks remain part of [the 0.2 gates](ROADMAP.md).

## Alpha 4 hardlinks and first accounts

October 25, 2026. Tests ran in isolated disposable VM 102. The friend's machine,
household services, NAS, VPN credentials and personal files were not modified.

- All 338 tests passed on Linux. Windows passed 316 with 22 platform/runtime
  skips. The 32 account tests also passed as an ordinary Linux user, with one
  root-only permission test skipped. All 16 Compose configurations passed the
  real `docker compose config --quiet` parser without starting containers.
- Against real Sonarr/Radarr APIs, the focused repair changed both
  `copyUsingHardlinks` fields from false to true and read them back. A second
  apply issued no PUT; read-only verification issued GETs only. All unrelated
  media-management settings, container IDs/start times, and media file
  inodes/sizes/mtimes were unchanged. The standalone zipapp CLI passed too.
- A separate empty application root and media tree completed reviewed storage
  and media applies. The pre-start receipt reserved all four supported apps.
  Jellyfin enrolled its administrator and restricted viewer; Sonarr, Radarr
  and Prowlarr enrolled their UI administrator. Real logins passed. Jellyfin
  rejected the viewer and anonymous caller at an administrator endpoint. Starr
  rejected anonymous access and incorrect passwords.
- Account resume used the exact saved credentials, issued no account/config
  writes, and did not restart containers. Jellyfin networking readback confirmed
  no supported automatic port-mapping flag was enabled. Seerr, Bazarr and
  SABnzbd remained explicitly manual, rather than being reported complete.
- Reviewed DVD-source repair tests cover legacy and deb822 formats, preserved
  online mirrors/comments, private original backups, stale reviews, decline,
  mixed-source refusal, partial-write evidence and no repair on generic APT 100.
  These used controlled source fixtures, not the household's APT configuration.

The account tests caught an actual Jellyfin API incompatibility before release:
the public-user list can hide its initial account, and obsolete password flags
do not prove a password exists. First enrollment now requires successful
authentication of the actual empty-password seed. A later version-matched
source review confirmed that Jellyfin also refuses its startup password write
once the existing user has a password. The installer still does not coordinate
all applications with a separate human's browser changes; operators must leave
their setup wizards alone during automatic enrollment.

Settings readback and a filesystem hardlink probe are separate from a real
Starr download/import inode test. That complete import, commercial-provider VPN
commissioning and the independent operator rollout remain release gates in
[the 0.2 roadmap](ROADMAP.md).

## Alpha 3 guided installer acceptance

October 25, 2026. The new `./setup` entry point was exercised in actual Linux
pseudo-terminals on the isolated disposable VM, alongside real application and
storage tests. This changes the operator interface; the reviewed deployment
engine, storage guards, image locks and VPN namespace requirements remain.

- Help, quit, invalid menu input and invalid step handling passed. Screens did
  not print raw JSON, copied plan hashes or stored credential values.
- Guided step 1 recognized the existing verified runtime. Package/dpkg/apt
  evidence and the Docker PID were unchanged; no install or restart ran.
- Guided step 3 resumed healthy guarded media services, reused authenticated
  credentials and completed 14 configuration plus 14 readback checks. All seven
  container IDs and start times were unchanged. Actual browser URLs appeared.
- A separate fresh CPU/torrent-only wizard saved private configuration. Guided
  storage apply/verification ran twice; saved answers were reused unchanged.
- The guided NFS connection mounted an owned loopback-only NFSv4 export with
  root squashing, saved a persistent entry and exact private backup, and passed
  real UID 1000 folder/hardlink checks. Resume did not remount or rewrite it.
  Its fstab was a private acceptance fixture; the household NAS and its ACLs
  were not contacted or changed.
- A mistyped API key was rejected by the real Jellyfin endpoint before file
  creation. A valid key passed; explicit correction preserved the exact old
  installer copy privately. Neither value appeared in the operator messages.
- Regression tests cover missing/stale systemd guards, pending enrollment,
  exact accepted-plan validation, interrupted progress, owned subprocess-group
  interruption, per-config progress isolation and scoped NFS/fstab conflicts.
- Final Linux run: all 236 tests passed. Windows: 219 passed and 17
  platform/runtime skips. The existing real Compose parser checks remain green.

The first-user browser logins, real provider credentials and NAS export/identity
permissions still need the operator. A readable installer does not prove
commercial VPN forwarding, sustained throughput, GPU support or backups.

## Alpha 2 failure and recovery pass

The additional October 24 pass used the same isolated disposable VM and no
production NAS mounts, VPN credentials or personal files.

- Linux: all 186 tests passed. Windows: 183 passed, with three platform/runtime
  skips. All 16 Compose render scenarios parsed with the real Compose client.
- A real 64 MiB loop-backed ext4 test filesystem was filled until no allocatable
  blocks remained. Stage 0 failed visibly, recorded failure and released its
  apply lock. Removing only the test filler allowed a new reviewed apply to
  recover without rebuilding storage.
- Denied media-UID permissions, a read-only remount and an unmounted filesystem
  were refused. Remounting restored verification; no local fallback tree was
  created and the outside sentinel's bytes/metadata were unchanged.
- The pass found and fixed a cross-stage guard configuration bug. Each unit now
  uses its own saved config. A real management apply with different unavailable
  storage left the media guard unchanged. With the media guard's expected source
  deliberately wrong, systemd refused media startup while Dockge/Kuma returned
  HTTP 200. Restoring it recovered all 14 media checks.
- Three owned-WireGuard drop/recovery cycles blocked tunnel traffic and physical
  interface escape while an outside control remained reachable. Each recovery
  was followed by a new URI-only magnet with successful metadata exchange and
  a complete, hash-matched 1 MiB payload. No tag/approval gate was needed.
- A separate upload receiver received the complete hash-matched 1 MiB payload;
  qBit's uploaded counter increased by that amount. Reconnecting this reused
  fixture took about 95 seconds, so this is not a throughput benchmark.
- Eight concurrent authenticated API clients completed 160 read requests with
  correct results. Invalid-password and unauthenticated requests were rejected.
  Final qBit settings retained tun0, zero bandwidth caps and unlimited upload
  slots; discovery preferences were restored after controlled peer testing.
- Bootstrap retry planning succeeded with the corrected sudo example, which can
  read the private prior receipt without weakening its permissions.
- A separate fresh install/data tree exercised the torrents-only profile:
  stage 0 repeated, all six media UIs returned HTTP 200, SABnzbd was absent,
  and a repeated apply preserved container IDs and start times. Unfinished
  Jellyfin/Seerr onboarding remained pending and verification refused false
  success. The new test stage was stopped/disabled and its temporary guest-only
  IP removed afterward; the outside sentinel was unchanged.

The tunnel cycles took about 21 to 26 seconds including deliberate probe
timeouts; fresh magnets completed in about 17 to 18 seconds. These are small,
owned fixtures, not commercial VPN, swarm, line-rate or long-duration load
certification. Provider, hardware, storage-pool migration and actual client
playback checks below still apply.

## Disposable installation

A new Proxmox VE 9.0.3 guest used the official SHA512-pinned Debian 13 cloud
image, 2 vCPUs, 3 GiB RAM, 4 GiB guest swap and an 80 GiB fresh virtual disk.
Heavy stages were exercised sequentially to respect the host's spare memory.
This constrained test allocation is not recommended sizing for a combined
always-on deployment; follow the applications' requirements in [QUICKSTART.md](QUICKSTART.md).
QEMU user networking exposed only SSH on host loopback; guest firewall rules
blocked private LAN destinations before outbound Internet access was enabled.
The VM had no production NAS shares, personal-data mounts or production VPN credentials.

| Area | Actual result |
| --- | --- |
| Proxmox foundation | Created the explicitly selected new guest, imported/resized its new disk and verified owned definitions through the real Proxmox API |
| Guest bootstrap | Real official-repository package installation passed; Docker 29.9.2, Compose 5.6.1 and guest agent active |
| Bootstrap retry | Replanned/reapplied successfully; package-history/list files and Docker PID unchanged |
| Local storage | Actual stage-0 preparation, UID/GID access and hardlink verification passed; outside sentinel retained |
| NFS storage | Actual loopback NFSv4.2 root-squash run passed as UID 1000; root writes denied; existing sentinel data/permissions preserved |
| Missing NFS | After unmount, preparation refused the changed mount identity before creating fallback files |
| Media enrollment | Synthetic administrators and authenticated readbacks passed for Jellyfin, Seerr, Sonarr, Radarr, Prowlarr, Bazarr and SABnzbd |
| Media wiring | Configure, repeated configure and verify passed 14 real API checks; repeat created no duplicate managed resources |
| Apply retry | Replanned/reapplied the final pinned media recipe without changing any container ID or start time; all 14 API checks passed again |
| Video playback | Synthetic 3-second clip indexed; authenticated full stream matched all 184,977 bytes; range playback returned correct bytes with HTTP 206 |
| VPN and torrents | Owned WireGuard relay, real authenticated qBit wiring, exact 1 MiB download/upload hashes, and fresh URI-only magnet metadata/payload passed; bandwidth limits zero, upload slots unlimited, bind tun0 |
| Tunnel loss | Forced physical-interface traffic blocked while tunnel worked; owned endpoint down blocked transfer while outside control stayed alive; restoring endpoint recovered tunnel source and qBit stayed bound |
| Management | Dockge authenticated project/service discovery, Kuma administrator/readback, owned monitor UP heartbeat and actual guest-local webhook delivery passed |
| Management verification | Operator attestation recorded only after observed synthetic account, monitor, local notification and Homarr admin checks; verifier reported verified_with_manual_checks |
| Dashboard proxy | Homarr could read Docker through its allowed socket-proxy network; attempted Docker write returned HTTP 403 |
| Photos | First Immich owner/onboarding, API verification and synthetic PNG upload/original retrieval passed; tokenless original download returned HTTP 401 |
| Photo restore | Consistent database dump and complete assets copy restored into a fresh independent project; original owner login, asset ID and all 8,455 original PNG bytes survived; four restored services healthy |
| Music | Navidrome administrator, indexing and authenticated raw FLAC stream passed with all 44,400 bytes matching; slskd authenticated API and Music Assistant owner login passed |
| Cold boot | With deliberately wrong storage identity, media stayed stopped after Docker restarted; Dockge/Kuma remained reachable; restoring correct configuration recovered media; outside sentinel unchanged |

The known Jellyfin tokenless direct-stream behavior was reproduced too. Its
authentication does not protect every playback URL. Keep access private as
described in [SECURITY.md](SECURITY.md); a successful playback test does not
remove this upstream limitation.

## Automated checks

- Linux Python 3.13: 184 tests passed. Nine optional Socket.IO protocol tests
  used Node from the existing disposable Dockge image through a test-only
  wrapper; no host Node installation was needed.
- Windows Python 3.12.14: 184 tests, 181 passed and three platform-specific skips.
- Real Docker Compose 5.6.1: all 16 profile/provider render scenarios parsed;
  this check started no containers.
- Shell bootstrap syntax and release hygiene passed. The latter includes a
  private operator-specific deny list stored outside the source tree.

All 20 selected service images in the compiled installation match the final
release catalog. Its 21 immutable multi-platform indexes include the optional
port-sync Python runtime, which was pulled and executed separately. Registry
manifest hashes and platform descriptors were checked; see [IMAGES.md](IMAGES.md).

## Fixes found by the live run

- Proxmox cloud-init package upgrades are explicitly disabled. Reviewed guest
  bootstrap controls package installation instead of an inherited upgrade job.
- Jellyfin API calls use its standard MediaBrowser authorization header;
  Jellyfin 12.3.0 rejected the legacy `X-Emby-Token` header.
- Bazarr's generated YAML and SABnzbd's ConfigObj-style INI keys are read from
  their actual locations with constrained parsing and explicit ambiguity errors.
- Successful Starr/Prowlarr connection tests can return an empty JSON object.
  Nonempty error objects remain failures.
- Kuma first-run commissioning waits for its asynchronous Socket.IO readiness.
  Dockge commissioning reads actual stack-list/service states instead of a newly
  constructed object's unknown status.
- Invalid yes/no wizard input is retried; generated plan filename variants are
  ignored by Git.
- Apply rechecks its accepted plan after acquiring the deployment lock, so
  another completed apply cannot invalidate the review unnoticed.

## Boundaries still requiring your own checks

- Commercial VPN authentication, provider-specific forwarded ports and privacy
  of that provider's exit are separate from an owned-tunnel test.
- No paid indexer, NNTP, subtitle-provider account or public torrent was used.
  Real acquisition, Starr import rules and provider quotas need commissioning.
- Homarr first-administrator creation, repeat login and protected admin readback
  passed. Dashboard layout onboarding remains manual; no completed layout or
  personal dashboard was claimed by the API enrollment test.
- Soulseek login was deliberately blocked so no synthetic external account was
  created. Stage 6 correctly remains pending; manual attestation cannot waive
  its missing connected/logged-in API proof.
- Music Assistant's actual player discovery/playback needs a player LAN. The
  isolated guest proved administrator enrollment, not a physical speaker.
- GPU passthrough, guest drivers, hardware transcoding and GPU ML were not run.
  CPU-only remains the tested default; host GPU binding is manual in this alpha.
- Immich's CPU ML process started healthy, but recognition/inference jobs were
  disabled in the small synthetic instance to bound model downloads and memory.
- The photo acceptance drill is separate from configuring your ongoing backup
  destination, retention and off-site copies. Test your own restore before use.

The local webhook proves delivery to an owned HTTP receiver, not delivery to
Discord, Telegram, an email inbox or a pager. See [ACCEPTANCE.md](ACCEPTANCE.md)
for the checks to complete in the intended environment.

## Distribution and existing MediaBot

The release includes source, checksums and a source manifest. Site configuration,
plans, secrets, databases, private test evidence and lab SSH keys stay outside
the published archives. The tag identifies the source commit.

MediaBot's existing default branch and v2.12.1 release retain their standalone
installer, Compose definition, blank environment example, license and checks.
Its reachable-history credential audit covered 62 commits and 626 unique blobs,
roughly 22.5 MB, without identifying real credentials. This is pattern-based
evidence, not proof that an unknown secret never existed. See
[MEDIABOT-AUDIT.md](MEDIABOT-AUDIT.md).

Production services, their storage and their VPN/approval safeguards were not
modified by this disposable installation.
