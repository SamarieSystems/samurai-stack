# Toward 0.2

0.2 is the next repeatable-install milestone. The alpha label stays until the
complete supported path works for someone other than its developer, including
recovery from a failed step. A higher version number alone does not establish
that.

## Release gates

| Gate | Evidence needed |
| --- | --- |
| First-run accounts | All seven selected media apps enroll from hidden terminal credential prompts and wire without browser first-user wizards or API-key copying. Jellyfin has separate administrator and restricted viewer logins; Seerr uses those identities. Actual login and permission checks pass. Existing accounts survive retries and upgrades; unconfigured older apps require account-absence proof. Management/optional apps retain their explicit onboarding steps. |
| Storage selection | Local disks and NFS show the selected mount, source and capacity. A missing media mount prevents startup. Existing collections remain untouched until a separate reviewed import is requested. |
| Downloads and imports | Sonarr/Radarr hardlink settings read back as enabled. An actual controlled torrent import preserves device and inode across download and library paths. A Usenet import demonstrates an atomic move on the same filesystem. |
| VPN | A supported commercial-provider profile passes a controlled tunnel interruption and recovery test. The torrent client remains unable to use the ordinary WAN route. A local test VPN is useful development evidence but does not satisfy this provider check. |
| Recovery | Configuration and application-state backup can be restored to an isolated installation. Resume and source updates preserve deployment identity, credentials, appdata and media. |
| Hardware and independent rollout | Repeat the core path on CPU-only Debian with local storage and NFS, and torrent-only and Usenet configurations. Record at least one independent operator's clean install and corrected failures. |

The supported core is a Debian VM, storage, Jellyfin/Seerr/Starr applications,
optional Usenet, VPN-protected torrents, and management. Photos and music stay
optional, with their own acceptance checks. GPU passthrough, public exposure,
automatic collection migration and local LLM hosting do not gate this core
milestone.

## What stays manual

Proxmox installation and hardware allocation, selecting and mounting an existing
disk, NAS export administration, provider account creation, indexer and quality
preferences, subtitle languages, and real playback/notification tests require
the operator's choices. Backend automation should remove repetitive typing
without guessing those choices or claiming that a green container proves them.

If the above gates pass while the supported hardware matrix is still small,
0.2 should ship as a beta with those limits documented. General-purpose stable
deployment needs broader independent evidence.
