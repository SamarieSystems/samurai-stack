# Alpha validation

October 24, 2026. These are measured results from the release candidate, not a
claim that every supported provider, device or deployment has been tested.

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
