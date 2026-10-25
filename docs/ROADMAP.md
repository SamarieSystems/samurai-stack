# Supported 0.2 scope and next gates

0.2 is the repeatable-install milestone for the documented private Linux guest
path. It combines the release candidates with terminal provider intake,
future-import naming/subtitle policy, storage reserves and management enrollment.
It does not certify every hardware combination, provider, playback client or
existing installation. The dated [validation record](VALIDATION.md) distinguishes
disposable tests from target-machine commissioning.

## Core evidence

| Area | Recorded evidence and boundary |
| --- | --- |
| Media accounts | Earlier disposable runs enrolled all seven selected apps through terminal credentials and verified logins/wiring. Existing accounts are authenticated rather than reset. Jellyfin has a restricted viewer; Seerr uses Jellyfin sign-in. |
| Management | Fresh actual Dockge/Kuma owners, wrong-password rejection, managed-project discovery and additive TCP monitors passed in an isolated guest. Retry preserved IDs and secrets. Existing owners still need their current credentials; notification delivery is separate. |
| Storage | Local/NFS checks, actual Starr hardlink and same-filesystem move imports, preserved sentinels and missing-mount cold-boot refusal were recorded in the disposable guest. No partitioning, RAID conversion or automatic collection migration is performed. |
| Providers | One friend installation completed an official SAB test download, a completed-download import and a controlled Proton tunnel failure/recovery check. No ordinary-WAN fallback occurred. This does not establish line-rate throughput or other VPN providers' behavior. |
| Recovery | Consistent application-state backup/restore, restored-account identity reauthorization and portable/source resume checks passed. Ordinary setup still refuses replaced or unowned application state. |
| Hardware | The disposable core runs CPU-only Debian 13. A friend rollout used an existing local media filesystem. Other bootstrap configurations have narrower parser/unit evidence; there is no general GPU passthrough qualification. |

The friend began the guided installation independently and corrected early
failures with assistance. Maintainer commissioning completed the recorded
provider checks. That is one assisted deployment, not a broad independent
hardware or user study.

## What stays deliberate

The operator chooses storage, numeric media identity, protocol/VPN, provider
subscriptions, indexers and policies. Backend automation handles the supported
choices without inventing accounts. Proxmox installation, physical disk
selection, NAS export administration and GPU passthrough remain separate.
Photos/music retain their own owner/player/provider/backup acceptance steps.

Source authentication and a green container do not prove acquisition. A
registered subtitle provider does not prove it delivered a subtitle. The
optional English policy applies to future additions and does not launch a
whole-library backfill. Capacity reserves are pause/import thresholds, not
quotas; other processes and in-flight work can still consume disk space.

## Next qualification work

Broaden independent clean-install/recovery reports, storage/hardware coverage,
long-duration upgrade testing, provider variants and notification delivery.
Keep published evidence scoped to what was exercised. Public ingress,
automatic collection migration, local LLM hosting and unattended hardware
changes remain outside this milestone.
