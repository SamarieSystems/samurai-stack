# Guided setup

Run `./setup` inside the new Linux guest. Choose a numbered step and answer its
questions. It shows the changes for review and asks before applying them. Media
account setup and application wiring happen in the terminal; a browser is for
using the apps and choosing your providers and preferences. You do not copy
hashes, paste API keys or edit JSON to follow this path.

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
| First users | One media application administrator username/password and a different everyday Jellyfin/Seerr viewer username/password. For an existing Jellyfin owner, have its current login ready. Do not reuse Linux root/SSH credentials. |

The [deployment intake](DEPLOYMENT-NIGHT.md) includes hardware inventory commands,
resource planning and the existing-machine branch. Leave photos/music off until
the core download/import/playback path works. No GPU is required.

In the guest, open a root console (`su -` if needed), then run:

```sh
apt-get update && apt-get --no-remove --no-upgrade install -y git python3 ca-certificates
git clone --branch v0.1.0-alpha.5 https://github.com/SamarieSystems/samurai-stack.git && cd samurai-stack
./setup
```

Keep the complete source directory and its private `site.json`. Reopen the menu from
this same directory whenever you need to continue. The launcher uses sudo
when started by a normal account, or explains how to return as root if sudo
is unavailable. Media account and provider passwords go into hidden terminal
prompts, not command arguments.

### Update an existing installation, including a source ZIP

Keep the existing `site.json`, installation/deployment ID, application
directories, storage marker and private credentials. They identify the running
installation. Do not run a new configuration wizard, generate a replacement ID
or delete appdata to update the toolkit.

The shortest update for an existing source ZIP is the portable
`samurai-setup.pyz` release asset. Obtain it from the
[alpha 5 release](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.1.0-alpha.5)
and verify its supplied `SHA256SUMS`. From a root console in the old source
directory containing the correct private `site.json`, run:

```sh
python3 /path/to/samurai-setup.pyz --config site.json 3
```

It contains the setup runtime, so the old extracted source can stay in place.
Use the actual absolute `--config` path if you run it from another directory.
The saved application and credential locations are reused.

A directory extracted from GitHub's source ZIP does not need Git. Extract the
updated source into a **new directory beside the old one**, leaving the old
directory intact. In the new directory, point setup at the existing private
configuration, substituting its actual absolute path:

```sh
chmod +x setup stack
./setup --config /absolute/path/to/old-source/site.json 3
```

Keep using that `--config` path on later runs. There is no need to copy secrets
or application databases into the new source tree; their saved locations remain
in the existing configuration. Do not extract a ZIP over appdata or a storage
mount. Use the source for the release you intend to install; an older extracted
ZIP does not acquire new setup behavior by itself.

For a Git checkout only, update using the account that owns it. For example,
the alpha 5 source can be selected with:

```sh
git fetch --tags origin
git checkout v0.1.0-alpha.5
./setup
```

Select the intended release tag rather than assuming this example is the newest
release. If Git reports conflicting local changes, preserve and review them.
Do not use a forced checkout. Updating source does not rotate application
passwords or provider credentials.

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

Select **3**. The installer starts the selected media apps and handles their
accounts, API keys and supported connections. For a new installation, enter one
application administrator username/password and a separate everyday
Jellyfin/Seerr viewer username/password. Password entry and confirmation are
hidden; use distinct passwords of at least 12 characters, separate from Linux
root/SSH. Exact saved credentials are reused on retry.

The media account setup covers **Jellyfin, Sonarr, Radarr, Prowlarr, Bazarr,
Seerr and SABnzbd**. Torrent-only installations omit SAB. It verifies account
access and permissions, including rejecting administrator access for the
everyday Jellyfin viewer. API keys are discovered or created and saved
privately. There is no first-user browser wizard or API-key copying step for
this supported path.

For **Seerr**, select **Sign in with Jellyfin** and use the Jellyfin
administrator or everyday viewer account. These are not separate local Seerr
passwords. Use the everyday viewer for watching and requesting; the
administrator account manages the services.

Do not create accounts in a second browser wizard while automatic enrollment
is running. The first-run reservation proves initial empty application state,
but the applications do not offer an atomic lock against a separate operator.

**Existing installations:** configured accounts are inspected and preserved.
The installer does not reset passwords, rename an existing owner or replace its
permissions. If an existing Jellyfin administrator login is needed to finish
the connection, enter its current credentials when requested. An existing
viewer must authenticate with its current password; a missing viewer can be
created with the separate identity you supply. It does not change an existing
viewer's policy to make a check pass.

Older alphas may have started empty applications without reserving them first.
The updated installer can enroll these only after proving that their owned
running containers have no configured accounts. This uses app-specific API
checks and, where required, read-only database checks. An incomplete wizard
flag alone is not proof. Ambiguous state pauses for review instead of resetting
an account.

**Usenet enabled:** enter the NNTP hostname, TLS port (default `563`), allowed
connection count (default `8`), service username and hidden service password.
Use your provider's NNTP credentials, which may differ from its website login.
The installer tests an actual authenticated connection with TLS certificate
verification enabled, saves the provider and completes SAB's setup wizard. It
does not lower certificate verification to make a failing connection work.
Existing configured servers are connection-tested with their saved credentials
and kept as they are. If a separate existing SAB administrator login is needed,
the terminal requests it without changing that account.

The installer then tests, saves and reads back the supported
library/client/application connections. You do not construct inter-app URLs
and paths yourself. This includes enabling and reading back Sonarr/Radarr's
`copyUsingHardlinks` and selecting the two deployment-named Jellyfin movie/TV
libraries in Seerr after verifying their actual paths and IDs. Unrelated Seerr
library selections are preserved. Setup does not start a content scan or
request media; scheduled availability checks can use the enabled libraries.
An actual download/import inode check remains a separate acceptance test.

Open the printed LAN URLs after account setup. These choices still belong to
you; they are not unfinished account wizards:

| Choice | Where to make it |
| --- | --- |
| Indexer accounts and searches | Add your authorized indexers in Prowlarr and test them. |
| Quality, language and size preferences | Review the profiles and cutoff rules in Sonarr/Radarr. |
| Subtitle languages and providers | Select them in Bazarr using your own provider accounts. |
| Usenet subscription and connection allowance | Obtain these from your provider; terminal setup handles the server connection. |

For only the hardlink setting on an existing install, use
`./setup --hardlinks`. It does not redeploy the media stage or scan media.

If account or provider verification is incomplete, the result stays **pending**
or reports the failed check. Follow that specific action, then retry the same
step. With the existing configuration in this directory:

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
