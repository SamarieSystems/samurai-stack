# Install a new server

This is the first-install path for a **new Linux VM**. It does not replace
Windows, install Proxmox, format a disk, or convert an existing media server.
Already using a working installation? Start with [Daily use](DAILY-USE.md), not
another installation.

You run one saved installer and select its menu steps in order. It asks for
your choices, shows changes before applying them, and handles supported
application accounts and connections. Keep this page beside the terminal.

## Before opening the installer

These prerequisites are manual:

| Have ready | What that means |
| --- | --- |
| A Linux guest | Debian 13 is the main tested path. Debian 12 and Ubuntu 24.04/26.04 are supported. Use a KVM/QEMU VM with systemd, internet and root or sudo access. No GPU is required. |
| A guest-agent channel | Enable QEMU Guest Agent for this VM in Proxmox. If it was disabled, cleanly shut down and start the VM after enabling it. |
| A reserved LAN address | Reserve this VM's DHCP lease in the router. Its address must stay consistent with the saved application bindings. A hostname alone does not ensure that. |
| Prepared storage | Mount the existing Linux media filesystem in the guest, or create a dedicated NAS NFS export with the intended guest permissions. Disk passthrough, formatting and NAS accounts are outside this installer. |
| Provider details | Have your own VPN configuration for torrents, and NNTP/indexer credentials for Usenet if selected. |
| Two media logins | Choose an application administrator and a different everyday Jellyfin/Seerr viewer. Use separate passwords, at least 12 characters, not the Linux root password. |

For creating the VM, use [Proxmox setup](../PROXMOX.md). Hardware sizing and
storage inventory are in [Deployment intake](../DEPLOYMENT-NIGHT.md).

## Download once, check once, launch

Get **`samurai-setup.sh`** and **`SHA256SUMS`** from the
[v0.2.4 release assets](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.2.4).
Copy both into the same directory inside the Linux VM. Open an interactive
terminal in that directory, inside the guest, not the Proxmox host console.

Run this first:

```sh
sha256sum --check --ignore-missing SHA256SUMS
```

Continue only when it explicitly says **`samurai-setup.sh: OK`**. A mismatch,
missing file or no matching checked installer means stop and check the downloads.
Other release assets can be absent because you only need these two files.

Then run:

```sh
bash ./samurai-setup.sh
```

Those are the two startup commands. No Git, pip, Python virtual environment or
executable-permission fix is needed. The launcher requests sudo when available;
if it says root access is needed, use the guest's root console and return to
this directory. Missing Python is offered through reviewed OS package setup.

## 1. Prepare Debian

Choose **1**. Read the detected OS and package proposal, then accept the intended
guest preparation. Setup installs missing Docker, Compose, guest-agent and NFS
prerequisites. It does not repartition storage or remove conflicting packages.

If preparation pauses, read its specific error. An installation-DVD APT source,
missing guest-agent channel or failed internet connection needs that cause fixed.
Do not reinstall Debian because one package check failed. See
[bootstrap diagnostics](../HOST-BOOTSTRAP.md).

## 2. Connect storage

Choose **2**. Here is what the questions mean:

| Prompt | Your choice |
| --- | --- |
| LAN address | This VM's reserved address, not the Proxmox or NAS address. |
| Short installation name | A lowercase name for this deployment, such as `homebase`. |
| Local app/config directory | Local VM storage for databases and settings; `/opt/samurai` is the suggested default. |
| Timezone | Your location's timezone, for example `America/Denver`. |
| Download method | Torrents, Usenet or both. With both, choose the preferred protocol too. |
| Storage | The actual mounted data filesystem, or the NAS address/export and guest mountpoint. |
| Dedicated downloads/library directory | A new empty child on that storage, such as `/mnt/media/samurai` beneath an existing `/mnt/media` mount. |
| Media UID/GID | Numeric Linux ownership IDs. `1000:1000` commonly matches the first normal user; NAS permissions must agree. |
| Compose stack names | Names shown in Dockge. Accept the suggestions or choose distinct names for media, torrents, management, photos and music. |

**Check the backing filesystem shown in the review.** A directory on the VM OS
disk uses that disk, even if a large media drive exists elsewhere. Setup tests
write access and hardlinks, creates its dedicated folders, and preserves existing
media. It does not adopt old libraries or recursively change their permissions.
Photos/music remain off. See [Storage](../STORAGE.md).

## 3. Media applications

Choose **3**. Enter the administrator and separate viewer credentials through
the hidden prompts. Setup enrolls supported accounts and wires Jellyfin, Seerr,
Sonarr, Radarr, Prowlarr, Bazarr and, when enabled, SABnzbd. Do not simultaneously
create their first accounts through browser wizards.

Review optional quality, naming, capacity and subtitle policies. The HD preset
keeps original audio; it is not a promise of English dubbing. Existing accounts
are retained and require their current login.

Usenet has two different inputs:

- **NNTP server:** your download provider's hostname, TLS port, connection
  allowance and service login. SAB retrieves data there.
- **Indexer:** a search provider's URL/API key, such as NZBGeek. Prowlarr searches
  it. The NNTP password is not this API key.

Setup tests supported sources; other source types need deliberate Prowlarr
configuration. Subscriptions and successful subtitle delivery are not supplied.
See [source setup](../INDEXERS.md).

## 4. VPN and torrents

Choose **4** if torrents were selected. Choose the actual provider and protocol.
For Proton WireGuard, copy the generated configuration's **PrivateKey value**
when prompted. Mullvad also needs its assigned tunnel address. OpenVPN uses
service credentials; custom configurations use a private profile file.

A VPN website password is not a WireGuard key. Enable forwarding only when
your provider/plan supports it. Setup connects qBittorrent to Gluetun, verifies
its namespace/interface and wires the media managers. Save the generated qBit
login privately. Never bypass the VPN to fix a paused download.

## 5. Management

Choose **5** for Dockge and Uptime Kuma. Supported new owners and named private
listener monitors are enrolled in the terminal. Existing owners need their
current password and are not reset. If you want alerts, configure the sending
service and destination in Kuma and receive a test alert. Email requires an
SMTP sender, not only a recipient. Otherwise record notifications as not
enabled; the local dashboard checks still work. Optional Homarr has separate
onboarding.

## 6. Optional extras

Skip extras until the core works. This menu deliberately enables photos or
music, rather than everything at once. Immich needs independent photo/database
backups and a restore test. Music needs provider/player and library decisions.

## Prove it works, then hand it over

Use one small authorized film or episode that the sources can supply. Follow
its request, actual download, import and Jellyfin playback. Confirm its file is
on the intended data filesystem. A Linux ISO can test torrent transfer, but
cannot prove a movie import. Complete the controlled VPN failure/recovery check
in [Acceptance](../ACCEPTANCE.md) before bulk torrent use, then reboot the guest
and confirm the apps return.

Record private URLs, credentials, backup responsibility and any pending checks.
For a pause or failure, keep all state and rerun the **same Bash launch command**,
then select the same menu step. Progress lives outside the download directory;
deleting folders is not a retry method. See [Troubleshooting](TROUBLESHOOTING.md).
