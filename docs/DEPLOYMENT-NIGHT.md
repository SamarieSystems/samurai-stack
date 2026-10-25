# Deployment night: intake and order of operations

The supported first rollout is a new Debian 13 KVM/QEMU VM, a new dedicated
data directory, and private LAN access. Existing Proxmox is optional. Start with
media, torrents and management; add photos and music after the core works.
No GPU is required. CPU direct play is the first playback test, even when a GPU
will be added later.

If the friend already runs Jellyfin/qBit on combined disks, start with the
existing-machine branch below. Do not install Proxmox over that machine or
rebuild its storage pool just to follow the new-guest path.

This is an alpha with a recorded disposable-machine acceptance pass, not a
replacement for checking the actual machine and provider. Read
[QUICKSTART.md](QUICKSTART.md) for the guided menu and
[VALIDATION.md](VALIDATION.md) for the evidence and remaining limits.
The individual plan/apply commands remain in [Advanced CLI](ADVANCED.md).

If Samurai is already installed and the immediate task is its hardlink setting,
use the [existing-install repair](INSTALL.md#enable-hardlinks-on-an-existing-installation).
Keep that VM, configuration and data tree; the new-install sequence below is not
needed for this focused change.

## Collect these facts first

| Area | Information to have before starting |
| --- | --- |
| CPU and platform | Exact CPU model, core/thread count, x86-64 or ARM64, motherboard/system model, virtualization support enabled. Existing hypervisor/OS and workloads that must stay running. |
| Memory | Installed RAM, currently committed VM RAM, and headroom for the host. Do not allocate every byte to guests. |
| Local disks | Model, size and free space of each disk; which is the OS/VM disk, which holds existing data, and which disk may deliberately receive a new Proxmox install. Application state/databases need local Linux storage. |
| Data storage | NAS model/OS or local filesystem; how disks are combined (RAID, Storage Spaces, DrivePool, mergerfs, LVM, etc.); exact mount/export/subdirectory; available capacity; current backup and independent backup destination. Keep personal documents/photos outside first-run testing. |
| Identity and permissions | Chosen numeric media UID/GID, NAS identity mapping/ACLs, and which VM addresses may access the dedicated export. |
| Network | Router subnet, gateway, DNS, intended reserved VM addresses, NAS address/export, and available Ethernet links. A DHCP reservation is fine. Avoid overlapping address assignments. |
| GPU, if wanted | Exact GPU/iGPU model and VRAM, current use/driver, whether it drives the host display, all PCI functions/IOMMU group, and a recovery console. Passthrough and drivers remain manual. |
| Actual use | Torrents only, Usenet only, or both; preferred download protocol; simultaneous streams; direct-play clients versus required transcodes; photos/music now or later. |

Planning estimate, not a tested minimum: for a modest media+torrent+management
guest, start the resource discussion around 4 vCPUs, 8 GiB RAM and an 80 GiB
local OS/application disk, with media capacity separate. Provider queues,
metadata, thumbnails, transcodes and other workloads can require more. The
foundation wizard's suggested allocations are proposals, not an application
capacity calculation. Immich needs at least 6 GB and recommends 8 GB for photos
alone; budget that separately and follow its
[current requirements](https://docs.immich.app/install/requirements/).
The 3 GiB validation guest used swap and sequential stage testing; do not copy
that allocation for an always-on combined installation.

Useful read-only inventory on an existing Linux host:

```sh
lscpu
free -h
lsblk -o NAME,MODEL,SIZE,TYPE,FSTYPE,MOUNTPOINTS
df -hT
ip -br address
ip route
lspci -nnk
```

On Windows, these read hardware only; they do not prepare disks:

```powershell
Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, VirtualizationFirmwareEnabled
Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer, Model, TotalPhysicalMemory
Get-Disk | Select-Object Number, FriendlyName, Size, PartitionStyle, OperationalStatus
Get-VirtualDisk | Select-Object FriendlyName, ResiliencySettingName, Size, HealthStatus
Get-Volume | Select-Object DriveLetter, FileSystemType, Size, SizeRemaining, HealthStatus
Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion
Get-NetIPConfiguration
```

Do not infer GPU VRAM from Windows' `AdapterRAM` property. Confirm it from the
GPU's actual model/vendor tooling. Review inventory for private names/addresses
before sharing it publicly.

## Existing-machine deployment: inspect before changing anything

An existing Jellyfin library plus working manual qBit downloads is a migration,
not an empty machine. First record the current OS, pool/filesystem, full media
paths, free space, Jellyfin/qBit versions and installation method, listening
ports, administrator access and application-consistent configuration backups.
Identify Kodi's current Jellyfin server URL, user and plugin connection too.
The existing Kodi setup should keep pointing at the old server until the new
one has passed playback testing. The installer does not migrate Kodi clients,
Jellyfin users/watch state or existing qBit jobs/configuration.

Use a new isolated supported guest with a different LAN address and new
application/data directories while the old services remain available. Do not
give the new Sonarr/Radarr a writable existing collection during first testing.
Their configured import workflows can rename/move files even though storage
preparation itself is additive. Start with a copied authorized test item.
Keep existing torrent payloads and resume state together until an explicit
migration and rollback plan is tested; this alpha will not adopt them.

For manual qBit and Kodi/Jellyfin, disable Usenet, enable torrents, prefer
`torrents`, and leave photos/music off initially. SABnzbd and paid Usenet accounts
are unnecessary. The new stage 3 Web UI accepts ordinary magnets without an
approval/scanning queue. The toolkit's baseline still includes the Starr/request
apps; it has no Jellyfin-plus-qBit-only profile in this release.

The application bootstrap supports KVM/QEMU systemd guests. It is not an
in-place Windows/Docker Desktop, WSL, container or bare-metal converter. The
supported data filesystems are local ext4/XFS/Btrfs/ZFS or the reviewed NFS
export; an existing NTFS/SMB/mergerfs pool cannot simply be declared ext4 in
configuration. Determine a compatible storage arrangement first. Do not break
up a working pool, repartition disks or move personal files to satisfy the
installer. If a compatible guest/storage path is unavailable tonight, keep the
existing installation running and prepare the migration instead of forcing it.

Even on an existing Linux Docker host, unowned app directories, colliding
projects/ports or conflicting Docker packages/repositories require deliberate
review. A different dedicated directory does not by itself isolate every host
port or Docker lifecycle action. The new-VM route avoids those collisions.

## Existing Samurai installation: hardlink repair

On its media VM, from the directory containing the existing `site.json`, run
`./setup --hardlinks`. It checks Sonarr and Radarr, asks before enabling a disabled
flag, then verifies API readback. The advanced equivalent is:

```sh
sudo ./stack --config site.json hardlinks
sudo ./stack --config site.json hardlinks --apply
sudo ./stack --config site.json hardlinks
```

The flag is `copyUsingHardlinks`, shown in both apps as **Use Hardlinks instead
of Copy**. This action preserves other settings and existing media. It does not
restart or redeploy services, scan/import files, re-enroll accounts, alter VPN
policy or migrate storage. A standalone `samurai-hardlinks.pyz` repair asset is
planned for older ZIP installations; see [INSTALL.md](INSTALL.md) for its intended
interface and publication/checksum boundary.

Keep the actual configured path. `/opt/samurai` is this project's conventional
local app-state default. `/srv/samurai-data` is the guided local-data default;
an installation using a mounted `/mnt/media/samurai` tree should keep that tree.
Starr and download clients use the common `/data` container layout under either
host prefix. The [path explanation](INSTALL.md#what-the-paths-mean) covers why
matching the real filesystem matters more than renaming directories.

API readback proves the flags are enabled. Before calling the download/import
path commissioned, verify matching device/inode numbers for a controlled seeding
torrent and its normal library import, then test playback. The narrow repair
does not perform that import or retroactively deduplicate existing copies.

## Credentials and access

Have your friend enter secrets in their own UIs or a hidden local terminal
prompt. A checklist of what exists is enough for planning; do not collect a
plaintext password bundle or reuse the developer's credentials.

| Needed when | Credential or access | Where it is used |
| --- | --- | --- |
| Every deployment | Root console or administrator/sudo access to each new guest; operator SSH public key and matching private key kept on the operator's computer | Guest login/setup. Only the public `.pub` key goes into Proxmox cloud-init. |
| Proxmox provisioning | Root shell on the intended existing Proxmox node; selected existing bridge/storage; official cloud image plus its published SHA512 | Stage 1 runs on that node. A Proxmox API token is not required by this alpha's local CLI workflow. |
| NAS storage | NAS administrator access to create a dedicated user/export/ACL, plus an allowed media identity | Vendor UI, then the guided guest connection review. NFS normally authorizes hosts and numeric identities rather than taking an SMB password. |
| Torrents | VPN subscription and protocol-specific service credentials/configuration | Hidden `secret` inputs on the torrent guest. A website password is not necessarily the VPN credential. |
| Usenet | NNTP server hostname, TLS port, service username/password and connection allowance | SABnzbd's own wizard and connection test. |
| Indexers/subtitles | Chosen provider URL and API key/account, if required | Prowlarr/Bazarr UIs. No commercial account or indexer is supplied. |
| App enrollment | New distinct administrator logins for the enabled applications | First-run LAN UIs. Jellyfin and Seerr API keys are then imported privately into the toolkit. |
| Monitoring | Chosen email/chat/webhook destination and its required credential | Kuma UI, followed by an actually delivered test alert. |
| Optional music | Your friend's Soulseek credentials; selected music-provider/player access | `secret soulseek_username`, `secret soulseek_password`, then separate Music Assistant enrollment if enabled. |

See [SERVICES.md](SERVICES.md#vpn-and-manual-magnets) for exact VPN secret names.
Proton WireGuard uses its generated private key; Mullvad also needs the
provider-issued tunnel address in site configuration. OpenVPN uses the
provider's service credentials. Custom VPN profiles use a private file import.
qBit's Web UI login, slskd's Web UI/API credentials and internal database keys
are generated locally; they are not credentials to obtain from the friend.

## Normal installation

Follow [Quickstart](QUICKSTART.md), then run `./setup`. Its steps are 1, guest
preparation; 2, storage; 3, media; 4, VPN torrents; 5, management; 6, optional
photos/music. Use `./setup 3` to resume media enrollment, for example. It handles
the private plans and asks for confirmation without copied hashes or JSON edits.
New NFS guest mounts include a reviewed persistent entry and a private fstab
backup; NAS export/user/ACL setup still happens in the vendor interface.

## Advanced order of operations

The following is the lower-level workflow for operators using [Advanced
CLI](ADVANCED.md). Its engine stage IDs are different from the guided menu
numbers above. You do not need to run both workflows for one installation.

1. **Choose the blast radius.** Back up important existing data, select a new
   VM and a separate empty test tree. If Proxmox must be installed, do that
   manually and positively identify its physical target disk. The toolkit does
   not install Proxmox, repartition disks or turn an existing Windows desktop
   into a server.
2. **Create the guest.** Either create a supported KVM/QEMU VM manually or use
   [stage 1](PROXMOX.md) on an already installed Proxmox node. Download the
   official cloud image and checksum first. Review free VM IDs, resources,
   storage, bridge and SSH key before accepting the foundation plan. Verify
   cloud-init completion, guest disk expansion and SSH separately.
3. **Copy the complete release source into the guest.** Keep `scripts/`,
   `stack` and `samurai_stack/` together. Run commands from the source root.
   Follow [host bootstrap](HOST-BOOTSTRAP.md), including the reviewed plan, then
   deliberately configure and persist the selected local/NFS storage mount.
4. **Create application configuration once.** Run `python3 stack init` in the
   application guest, enter the actual addresses/mount identity/UID/GID, and
   validate. Use role `all` for a compact guest; keep photos, music, dashboard
   and Music Assistant disabled until you intend to enroll them.
5. **Storage, stage 0.** Plan, read the plan, accept that exact hash, apply and
   verify. Confirm the expected storage and hardlink test, not merely that the
   directory exists.
6. **Media, stage 2.** Plan/apply, then `access 2`. Create app administrators,
   finish Jellyfin/Seerr enrollment, add provider/indexer/subtitle credentials
   and select quality/language profiles. Import the requested keys through
   `secret`, then run `configure 2` and `verify 2`. Torrents-only is supported:
   disable Usenet and prefer torrents in configuration; SAB is omitted.
7. **VPN torrents, stage 3.** Import the selected VPN inputs, plan/apply, obtain
   the generated qBit login privately, configure and verify. Test a legal
   magnet in the normal Web UI. Complete positive egress, tunnel interruption
   and recovery tests on this new setup, then verify a real payload/import/
   playback. There is no approval queue or ClamAV service in this simple stage.
8. **Management, stage 4.** Plan/apply, enroll Dockge/Kuma and optional Homarr,
   add monitors and receive a real notification. Only then attest/verify.
   This stage can run earlier if helpful; it needs no NAS mount.
9. **Optional stages 5/6.** Add Immich only when resources and an independent
   originals-plus-database backup/restore plan exist. Add music when library,
   Soulseek and optional player/provider enrollment can be completed. A fresh
   account or a running frontend alone does not prove playback/backup.
10. **Cold boot and handover.** Reboot the new guest, verify stage recovery,
    test a missing test mount without personal data involved, restore it, and
    check the intended playback device. Record addresses, protected credentials,
    backup responsibility and recovery commands before inviting users.

For every application stage `N`, the sequence is:

```sh
sudo python3 stack -c site.json plan N --out plan-N-01.json
sudo python3 -m json.tool plan-N-01.json
# Read it. Replace N and REVIEW_HASH with the actual stage and its exact hash.
sudo python3 stack -c site.json apply N --plan plan-N-01.json --accept-plan REVIEW_HASH
sudo python3 stack -c site.json access N
# Complete that stage's enrollment, then configure/verify as documented.
```

`N` and `REVIEW_HASH` are placeholders. Stage 0 has no separate `configure`.
Use a new plan filename after a failure or configuration change. Do not erase
appdata, image locks, storage markers or partial VM disks to make a retry pass.
Check [RECOVERY.md](RECOVERY.md) for preserved-state recovery.

### If using separate media and torrent VMs

Bootstrap each guest and mount the same actual data export at the same local
mountpoint, using the same canonical export spelling, NFS version and media
UID/GID. The shared marker requires identical `expected_mount` target/source/type
across guests. Initialize the application config once, copy it privately to the
second guest, then edit its role and bind address. Preserve the installation
UUID/name, host map and shared storage configuration. Do not
copy the whole application/secret directory between guests or rerun `init` to
get a second installation UUID.

Run stage 0 on both guests. Finish stage 2 enrollment/configuration on media
first. On the torrent guest import `sonarr_api_key` and `radarr_api_key` through
hidden `secret` prompts, using the keys from those applications' settings.
Import its VPN inputs and run stage 3 there. Stage 3 registers and tests the
qBit clients against those remote media apps. The media guest must reach the
torrent guest's qBit port 8081, and the torrent guest must reach the media
guest's Sonarr/Radarr APIs. Existing firewall/VLAN policy must permit those
private flows. Copy configuration, never reuse a plan generated on another VM.

Stage 4 can run on each host for local Docker control. A central Dockge does
not automatically manage another VM's daemon. A photos/music role is a separate
guest; to combine several application roles on one guest, use `all` and point
those selected host-map entries at that guest.

## What is still manual

Physical installation and disk choice; NAS accounts/exports/ACLs; review of
existing persistent mounts; reserved IPs and network policy; GPU passthrough/guest drivers and actual
transcode tests; first administrator accounts; paid/provider logins; quality,
language and notification choices; actual device playback; independent backups
and restore tests. Automatic cloud provisioning, GPU assignment, media migration,
WAN ingress and ongoing image/database upgrades are outside this alpha.

Keep first deployment private on the trusted LAN or an authenticated private
VPN. The alpha does not publish services to the internet. In particular, the
tested Jellyfin image can serve a known direct stream URL without a session;
login alone is not a sufficient WAN boundary. Dockge has host-level Docker
power. See [SECURITY.md](SECURITY.md) before changing exposure.

Tonight's core finish line is a real test download that lands in the intended
tree, imports correctly and plays, with a proven VPN stop/recovery test and a
successful guest reboot. Add personal photos only after a separate restore has
actually worked. Home Assistant, MediaBot, public access and local LLMs are
separate optional projects, not requirements for this rollout.

## Proposed v0.2 readiness gates

These are acceptance criteria for the next release, not claims about an untested
friend's hardware or provider:

- Complete fresh-guest setup through both a normal Debian installation and a
  cloud image. Missing guest-agent channels, active installation-DVD sources and
  package failures must stop with useful diagnostics, preserving existing
  repositories, packages and data.
- Resume an existing source-ZIP installation with its original configuration,
  credentials and ownership markers. Verify the focused hardlink repair without
  container recreation, restarts or media changes.
- Complete a controlled download, normal hardlinked library import and playback
  on the intended client. Test actual transcoding separately if that deployment
  enables GPU acceleration.
- Commission the actual VPN provider with positive egress, controlled tunnel
  failure, recovery and upload checks. Test guest reboot and missing-NAS startup
  refusal on a disposable fixture before relying on the installation.
- Restore application configuration and any enabled personal-data services from
  independent backups. Document recovery and retain clean source/artifact
  credential audits before inviting other operators to deploy the release.
