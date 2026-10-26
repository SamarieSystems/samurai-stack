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

For a new installation, storage setup also asks for each Compose stack's name
as shown in Dockge. Press Enter for the suggested `homelab-media`,
`homelab-torrents`, `homelab-management`, `homelab-photos` and `homelab-music`
names, or choose your own. Optional photos/music names reserve their identities;
those services remain disabled. An existing installation keeps its saved names.

The [deployment intake](DEPLOYMENT-NIGHT.md) includes hardware inventory commands,
resource planning and the existing-machine branch. Leave photos/music off until
the core download/import/playback path works. No GPU is required.

For access from another house, see [Private remote access](REMOTE-ACCESS.md).
Application ports bind to the selected LAN address; installing Tailscale alone
does not also bind those ports to a separate Tailscale address.

Download `samurai-setup.sh` and `SHA256SUMS` from the
[0.2.4 release assets](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.2.4).
Copy both files into one directory in the guest. In a terminal in that directory:

```sh
sha256sum --check --ignore-missing SHA256SUMS
bash ./samurai-setup.sh
```

Require `samurai-setup.sh: OK` from the checksum command before running it.
The portable Bash installer contains the setup runtime. It checks prerequisites
and offers reviewed installation of missing Python through the supported OS
repositories. It does not require Git, pip or a virtual environment. Run the
same command with `3` appended to resume media setup, for example.

If you already have the complete release source, use `./setup` in its root
directory instead. From a GitHub source ZIP, use `bash ./setup` if extraction
did not preserve executable permissions. After storage verification, the authoritative site is
saved at `/var/lib/samurai-stack/installations/DEPLOYMENT-UUID/site.json`.
Guided progress lives under `/var/lib/samurai-stack/setup`, independently of
the checkout. Before storage is verified, a private host-specific draft also
lives there. The launcher uses sudo when available, or explains how to return
as root. Passwords go into hidden terminal prompts, not command arguments.

### Start fresh after removing the old applications

If the old application data, containers and source folder were intentionally
removed, use the new release's fresh entry point inside the Linux VM:

```sh
./setup --fresh
```

With the verified portable release asset, the equivalent is:

```sh
bash /path/to/samurai-setup.sh --fresh
```

This opens a **new blank menu, phases 1-6**, without requiring or reusing an old
`site.json`, credentials or setup cache. Select **1**, then continue in order.
Phase 1 verifies working OS packages and tools; it does not reinstall them just
to clear a saved status. Phase 2 asks for new configuration and a new empty
media directory on the intended mount. Phase 3 requests fresh administrator
and everyday viewer credentials through hidden prompts and performs account
enrollment and application wiring automatically.

Fresh cleanup may archive old private installer state and exact generated
systemd units only after their application roots are absent or empty and both
their projects and UUIDs have zero containers, including stopped ones. Existing
appdata, custom services or uncertain ownership are not overwritten. Mounted
filesystems, existing media and storage markers are retained; this is not a
disk format, media move or application uninstaller.

The new session is saved privately outside the source folder. Run normal
`./setup`, or the portable command without `--fresh`, to resume it, even before
phase 2 has saved configuration. After verified storage registration, its
canonical configuration is used normally. **Running `--fresh` again starts
another blank session.**

### Update an existing installation, including a source ZIP

Keep the installation/deployment ID, application directories, storage marker,
private credentials and saved configuration. Updating source does not require
deleting any of them. Default setup discovers trusted registered or managed
runtime configuration before opening another wizard. Multiple candidates
require a choice; unresolved existing-installation evidence pauses setup.

The shortest update for an existing source ZIP is the portable
`samurai-setup.sh` release asset. Obtain it from the
[0.2.4 release](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.2.4)
and verify its supplied `SHA256SUMS`. For an installation already registered
outside its source folder, run from a root console in any directory:

```sh
bash /path/to/samurai-setup.sh 3
```

It contains the setup runtime, so the old extracted source can stay in place.
For a pre-registration release, explicitly name the original private site:

```sh
bash /path/to/samurai-setup.sh --config /absolute/path/to/old-source/site.json 3
```

The saved application and credential locations are reused. Storage is verified
before a canonical recovery copy is saved. A config copy does not contain the
application databases or secret files at those paths.

A directory extracted from GitHub's source ZIP does not need Git. Extract the
updated source into a **new directory beside the old one**, leaving the old
directory intact. For a registered installation, run in the new directory:

```sh
chmod +x setup stack
./setup 3
```

Use `./setup --config /absolute/path/to/old-source/site.json 3` when explicitly
resuming a pre-registration site. There is no need to copy secrets or databases
into the new checkout. Do not extract a ZIP over appdata or a storage mount.
An older extracted ZIP does not acquire new behavior by itself; run the new
source or the verified portable asset.

For a Git checkout only, update using the account that owns it. For example,
the release source can be selected with:

```sh
git fetch --tags origin
git checkout v0.2.4
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

- **Local:** select a suitable already-mounted Linux filesystem by its actual
  mountpoint, source and filesystem, then choose a new empty child directory.
  A disk mounted at `/mnt/media` can use `/mnt/media/samurai`; selecting an
  unrelated `/srv` path would instead consume that path's backing filesystem.
  Formatting, partitioning and mounting a new physical disk remain manual.
  The alternative local-directory option includes the VM's OS filesystem, so
  choose it deliberately rather than assuming it means the large media disk.
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

### Correct an unused storage choice

If a first-run local configuration selected the VM's root filesystem, this
action can choose a mounted local data filesystem before applications are in use:

```sh
./setup --config /absolute/path/to/site.json --choose-storage
```

It requires an **unregistered** local setup whose saved backing mount is `/`,
no application state, no containers for its installation or projects, and an
absent or empty managed data tree. It preserves the original configuration
privately under the persistent setup directory. The UUID, application root,
name and feature selections stay the same. After reviewing a new empty child
directory, the action runs normal storage preparation and verification. The
disk must already be mounted; this correction does not edit mounts or fstab.

This does not move media, change an existing registered installation's storage
identity, adopt another marker or repoint running applications. Existing media
at `/mnt/media/samurai` stays where it is; a directory with files cannot be
silently selected as a new tree. Use a separate migration/restore procedure
when there is data to keep.

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

For newly enrolled Sonarr/Radarr accounts, setup offers optional reviewed
720p/1080p original-audio profiles before wiring. You can decline and keep the
application defaults. On an existing installation, use menu **p** or
`./setup --quality-profiles` to review them separately. Existing profiles and
defaults are preserved; global size-definition changes need a separate review.
See [Media profiles](MEDIA-PROFILES.md) for the policy and its limits.

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
`copyUsingHardlinks` and selecting the two canonical Jellyfin movie/TV
libraries in Seerr after verifying their actual paths, types and IDs. Custom
display names and unrelated Seerr
library selections are preserved. Setup does not start a content scan or
request media; scheduled availability checks can use the enabled libraries.
An actual download/import inode check remains a separate acceptance test.

The optional [future-import naming review](MEDIA-NAMING.md) adds Jellyfin
TVDb/TMDb folder IDs and retains release metadata in future filenames. It does
not rename current files or move existing title folders.

Open the printed LAN URLs after account setup. These choices still belong to
you; they are not unfinished account wizards:

| Choice | Where to make it |
| --- | --- |
| Indexer accounts and searches | Step 3 tests existing sources and offers private NZBGeek/Newznab/Torznab intake when needed. Other source types can be configured in Prowlarr, then verified by the same step. See [source readiness](INDEXERS.md). |
| Quality, language and size preferences | Opt into the reviewed preset, or review profiles/cutoff rules in Sonarr/Radarr. Global sizes are a separate change. |
| Subtitle languages and providers | Review the optional future-additions [English policy](SUBTITLES.md), or select your own Bazarr policy/provider accounts. Existing-library backfill is not run. |
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
addresses after terminal enrollment. Proven-new Dockge/Kuma owners can use the
saved media administrator, or a management login entered through hidden prompts.
Existing owners must use their current credentials. Setup never resets a password
or adds another administrator behind an existing owner. It adds missing named
private TCP-listener monitors, preserves custom monitors and verifies real logins.
The optional Homarr owner still needs its own onboarding.

Choose the notification destination in Kuma and receive a real test notification.
Listener/account checks do not prove delivered alerts or full application health.

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
Keep canonical configuration/progress, appdata, image locks, storage markers
and secrets. A failure preserves partial state; deleting those files is not a
retry strategy. `./setup --recover` reports current paths, ownership labels,
mounts and leftover service references without changing them.

After a deliberate clean restart that removed all old application containers
and appdata, an older source-tree `site.json` can be supplied explicitly with
`--config`. A matching obsolete generated unit is archived only after repeated
checks prove its project and old UUID have no containers. Default discovery
still refuses dangling service references with no trusted saved configuration.
Deleted application databases and passwords need a real backup to be restored;
the leftover service file cannot recover them. See [Recovery](RECOVERY.md).

Before handover, complete one authorized download, confirm its path/size,
import and play it, test VPN failure/recovery and reboot the guest. Record
the private URLs, backup responsibility and protected credential locations.
[Acceptance](ACCEPTANCE.md) is the full checklist.

Keep the first installation on a trusted LAN or authenticated private VPN.
No public ingress is configured. Read [Security](SECURITY.md) before exposing
services, and [Recovery](RECOVERY.md) if something remains blocked. Use
[Advanced CLI](ADVANCED.md) for individual commands, receipts and explicit plans.
