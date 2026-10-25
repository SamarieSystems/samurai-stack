# Guided setup

Run `./setup` inside the new Linux guest. Choose a numbered step and answer its
questions. It shows the changes for review, asks before applying them, and
explains anything you still need to do in a browser. You do not copy hashes or
edit JSON to follow this path.

This guide uses **guided menu numbers 1 through 6**. The engine's stage IDs are
different; use [Advanced CLI](ADVANCED.md) for individual plan/apply commands.

## Before you begin

Use a new Debian 13 KVM/QEMU VM with a reserved LAN address and a dedicated
test data directory. Debian 12 and Ubuntu 24.04/26.04 systemd guests are also
supported; [validation](VALIDATION.md) records the actual disposable-machine
tests. The guest needs root access and internet access. A normal account with
sudo also works; a root console is enough if sudo is not installed.

If you have not created the guest yet, use [Proxmox](PROXMOX.md) or create a
supported VM yourself. Installing Proxmox and selecting its physical target
disk remain separate manual operations. This does not convert an existing
Windows machine or adopt its applications and disks.

Have these facts ready:

| Input | What to know |
| --- | --- |
| Guest | Its LAN address, timezone, available CPU/RAM and a new local application directory. |
| Storage | Local Linux filesystem or NAS address and exact dedicated NFS export. Decide the numeric media UID/GID and allowed guest addresses on the NAS. |
| Downloads | Torrents, Usenet or both; choose which protocol you prefer. |
| Providers | VPN protocol/configuration, Usenet service credentials if enabled, and your own indexer/subtitle accounts. |
| First users | Administrator logins you will create in the applications. Do not reuse a developer's credentials. |

The [deployment intake](DEPLOYMENT-NIGHT.md) includes hardware inventory commands,
resource planning and the existing-machine branch. Leave photos/music off until
the core download/import/playback path works. No GPU is required.

In the guest, open a root console (`su -` if needed), then run:

```sh
apt-get update && apt-get --no-remove --no-upgrade install -y git python3 ca-certificates
git clone --branch v0.1.0-alpha.3 https://github.com/SamarieSystems/samurai-stack.git && cd samurai-stack
./setup
```

Keep the complete checkout and its private `site.json`. Reopen the menu from
this same directory whenever you need to continue. The launcher uses sudo
when started by a normal account, or explains how to return as root if sudo
is unavailable. Credentials go into hidden prompts or an application's form.

### Continuing an alpha 1 or alpha 2 installation

In your existing checkout, using the account that owns it:

```sh
git fetch --tags origin
git checkout v0.1.0-alpha.3
./setup
```

Keep the existing `site.json`, private credentials and application directories.
The new menu resumes them; it does not require a new installation identity or
app accounts. If Git reports conflicting local changes, preserve and review
them. Do not use a forced checkout or delete the old installation to update.

## 1. Prepare this guest

Select **1**. Review the detected operating system and proposed package changes,
then confirm the new-guest preparation. It installs the missing Docker/Compose,
guest-agent and NFS prerequisites using the reviewed bootstrap.

This step refuses the Proxmox host, containers and unsupported environments.
It does not repartition storage or remove conflicting Docker packages to force
a pass. If an existing Docker installation conflicts, preserve it and review
the reported issue. [Host bootstrap](HOST-BOOTSTRAP.md) covers the exact boundary.

If it reports a missing QEMU Guest Agent channel, enable that option for this
VM in Proxmox, shut the VM down cleanly, then start it again and resume step 1.
A guest reboot alone does not add the missing virtual device.

If APT reports an unavailable `cdrom:` repository, the Debian installer left
its installation DVD enabled as a package source. Keep the working online
Trixie repositories. For a normal `deb cdrom:` line in `/etc/apt/sources.list`,
run these as root from this checkout, then resume step 1:

```sh
cp -an /etc/apt/sources.list /etc/apt/sources.list.before-samurai
sed -i '/^[[:space:]]*deb[[:space:]][[:space:]]*cdrom:/s/^/# /' /etc/apt/sources.list
apt-get update && ./setup 1
```

Only that DVD line is commented out; the backup and online sources are kept.
An empty `/etc/apt/sources.list.d` is valid when the OS mirrors are in
`/etc/apt/sources.list`. For another source format or a different APT error,
follow [host bootstrap diagnostics](HOST-BOOTSTRAP.md#apt-failures).

## 2. Connect storage

Select **2**. For a new installation, answer the questions about its name,
host, features, media UID/GID and storage. A compact single guest uses the
`all` role; leave acceleration on `cpu` for the first playback test.

- **Local:** choose a dedicated new directory on an existing supported Linux
  filesystem. This does not initialize or format another disk.
- **NFS:** create the dedicated export and identity rules in the NAS interface
  first. Enter its server/export and the guest mountpoint. Review a proposed
  new persistent mount before accepting it. An unrelated existing mount is refused.

The installer prepares and verifies its data tree, including a real write and
hardlink check as the media UID/GID. It does not move existing media or apply
recursive ownership changes. A denied write means the NAS identity/export
permissions need attention, not that root access to the entire NAS is needed.

Existing installations reuse `site.json` and their storage marker. Keep the
same installation identity when returning. For separate guests sharing one
tree, follow [the multi-VM instructions](ADVANCED.md#several-vms-one-installation)
before initializing a second configuration.

## 3. Set up media

Select **3**. The installer starts the selected media apps and prints their
actual LAN addresses. Open those URLs in a browser on the same private network.
Finish the listed first-run tasks, then return to the terminal and continue:

1. **Sonarr, Radarr and Prowlarr:** create the requested administrator access;
   add your indexers in Prowlarr and choose the quality profiles you want.
2. **SABnzbd, when enabled:** finish its wizard, enable Web UI authentication,
   enter the NNTP server/service credentials and run the provider connection
   test. Torrent-only installations omit SAB.
3. **Jellyfin:** create the administrator, finish onboarding and create an API
   key in its administrator dashboard.
4. **Seerr:** finish its initial Jellyfin sign-in against the printed Jellyfin
   address, then obtain its API key.
5. **Bazarr:** configure authentication, subtitle languages and your providers.

The installer reads locally generated application keys where supported and
asks privately for missing keys. It checks a newly entered API key against its
application before saving it. Existing valid keys are reused; correcting a
saved installer copy requires explicit confirmation and keeps a private backup.
It tests and saves the supported library/client/application connections, then
reads them back. You do not construct the inter-app URLs and paths yourself.

If enrollment is incomplete, the result stays **pending** with the next action.
Finish that screen and continue, or leave the installer and later run:

```sh
./setup 3
```

Returning to healthy owned services resumes enrollment and verification.
It does not create new administrators, erase appdata or redeploy the containers
because an API key was missing. If runtime repair is needed, the installer
reviews that repair before applying it.

Your provider accounts, quality preferences and playback device still need real
tests. A connected API does not prove an indexer can find an item or that a
television can play it.

## 4. Set up VPN torrents

Select **4** after the media apps are enrolled. Choose the actual VPN provider
and protocol, then supply the private input it requests:

| VPN | Input |
| --- | --- |
| Proton WireGuard | Generated WireGuard private key. Forwarding is an explicit provider-supported option. |
| Mullvad WireGuard | Generated private key and provider-issued tunnel address. No forwarded port is supplied. |
| OpenVPN | The provider's service username/password, which may differ from its website login. |
| Custom | A reviewed private WireGuard/OpenVPN configuration file, plus service credentials if required. |

Keep the key/profile available in its private provider download. A website
password is not a WireGuard key. Existing secret files are reused and never
silently overwritten. Correcting an installer copy does not rotate the
provider account itself; provider-side credential changes remain deliberate.

The installer starts Gluetun and qBittorrent together, verifies the exact
namespace and `tun0` binding, and connects the media managers. It prints the
qBit URL. Choose **View generated login** only in a private terminal; this login
is not a shared default or written into the normal report.

Paste an authorized magnet into the normal qBit Web UI. Manual downloads use
their separate directory. There is no antivirus approval queue in this simple
stack, and completion does not declare a file safe to run.

Finish the egress, controlled tunnel interruption/recovery and actual transfer
checks in [Acceptance](ACCEPTANCE.md). Ordinary verification does not interrupt
an active VPN or invent kill-switch evidence. Resume with `./setup 4`.

## 5. Set up management

Select **5** for Dockge and Uptime Kuma. Homarr is optional. Open the printed
addresses, create each enabled administrator and configure the monitors and
notification destination you use. Receive a real test notification before
confirming the checklist in the installer.

Management does not require the NAS. Dockge controls this guest's Docker daemon,
not every other VM automatically. Use it for logs and container terminals. The
generated systemd units own whole-stack startup/recovery; do not deploy the
same project through two controllers simultaneously.

## 6. Add photos or music when ready

Select **6**, then choose the optional component deliberately. This updates its
feature selection and runs that component's setup. It is not an unattended
install-everything button.

- **Photos:** finish Immich onboarding, upload disposable test pictures and
  verify retrieval. Establish independent originals-plus-database backups and
  perform an isolated restore before adding personal photos. Budget resources
  separately; [deployment intake](DEPLOYMENT-NIGHT.md) links the requirements.
- **Music:** provide your Soulseek credentials when requested, enroll Navidrome
  and play a real track. slskd downloads do not automatically become a trusted,
  tagged music library. If Music Assistant is enabled, enroll its administrator,
  providers and real players separately.

Confirm manual checklists only after doing them. They supplement the live
checks and cannot waive a failed API, mount or provider connection.

## Pause, resume and hand over

Leave the installer when needed, then rerun `./setup` or the relevant step.
Keep `site.json`, appdata, image locks, storage markers and secrets. A failure
preserves partial state; deleting those files is not a retry strategy.

Before handover, complete one authorized download, confirm its path/size,
import and play it, test VPN failure/recovery and reboot the guest. Record
the private URLs, backup responsibility and protected credential locations.
[Acceptance](ACCEPTANCE.md) is the full checklist.

Keep the first installation on a trusted LAN or authenticated private VPN.
No public ingress is configured. Read [Security](SECURITY.md) before exposing
services, and [Recovery](RECOVERY.md) if something remains blocked. Use
[Advanced CLI](ADVANCED.md) for individual commands, receipts and explicit plans.
