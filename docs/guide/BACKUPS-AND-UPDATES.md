# Backups, updates and recovery

[Guide home](README.md) | [Operating the server](OPERATING-THE-SERVER.md) | [Troubleshooting](TROUBLESHOOTING.md)

There are two different things to preserve: **the media you store** and **the applications' knowledge about it**. A server can retain every movie file and still lose users, requests, watch history, provider configuration and download state if its application databases are gone.

Samurai Stack 0.2.5 does not supply a universal scheduled backup, one-click restore, automatic image upgrade or database rollback. A folder named `backups` is not proof that the whole server is backed up. This page is an operator checklist; choose a real backup tool/destination and the applications' supported backup procedures before relying on it.

## What needs a backup?

Use the actual configured paths from the private handover note. The names below describe locations, not commands to paste.

| What | Why it matters |
| --- | --- |
| `INSTALL_ROOT/appdata` | Application databases/settings, media-server users/history, request and download-client state; use an application-consistent backup |
| `INSTALL_ROOT/secrets` and private project environment files | Provider, application and VPN credentials needed to restore the same connections |
| `INSTALL_ROOT/stages`, `state`, generated runtime helpers and image locks | Exact recipes, deployment identity, receipts and versions associated with the saved data |
| `/var/lib/samurai-stack/installations/DEPLOYMENT-UUID/site.json` | The canonical private host-bound configuration |
| Relevant generated systemd units, mount configuration and a hardware/network inventory | How the VM starts and reaches the intended storage; review before restoring onto different hardware |
| `DATA_ROOT/library` and any other valuable originals | Actual movies, episodes, music and files that configuration alone cannot recreate |
| Download working data, if you need to retain it | Partial downloads and seedable payloads; client state alone does not include their bytes |
| Immich originals **and** its database, if photos are enabled | Both are necessary; thumbnails/configuration alone are insufficient |
| Private access/recovery record | How an authorized person gets back into the host, VM, tailnet and apps |

The installer registry does not contain application databases. A copy of `site.json` cannot restore deleted accounts or photos. A Proxmox VM backup may exclude passed-through disks, NAS exports or other external media, so inspect what that backup actually includes.

Keep at least one usable copy outside the application VM and outside the same disk/pool failure. A second directory or share on the same failed NAS is not an independent copy. Protect backups with access controls and encryption appropriate to their credentials and personal files. Keep the decryption/recovery method somewhere accessible if the server itself is gone.

## Make a consistent application backup

Copying live SQLite/database files in the middle of writes can produce an unusable backup. Use an application's documented live backup/export if it provides one, or arrange a controlled stopped-service backup. Do not assume that zipping every live appdata directory is consistent.

For a planned stopped-service backup:

1. Tell users there will be an interruption. Pause new acquisition activity and wait for important imports, repairs and uploads to finish.
2. Identify the actual named systemd units using [Operating the server](OPERATING-THE-SERVER.md#find-the-right-systemd-unit). Do not mix Dockge lifecycle controls with those units.
3. Stop the media/importing stage first, then the torrent stage if enabled. Stop optional photo/music stages and any other process that writes the data you are copying. If backing up Dockge/Kuma databases, stop the management stage too and work over SSH. Confirm the selected stages have stopped; do not kill database processes to hurry them.
4. Use the selected backup tool to copy the stopped application data, private configuration/secrets, current recipes/locks and required media. Preserve numeric ownership, modes and relevant filesystem features. Record the release and exact paths included. Review symlinks, separate mounts and passed-through disks instead of assuming a recursive copy reaches everything.
5. Confirm the backup completed and is readable at its independent destination. Record whether this is a full data backup, application rollback baseline or only a configuration snapshot.
6. Start the intentionally stopped units through systemd. Management can return first; bring back the media stage, then its torrent stage, followed by selected extras. Verify logins, downloads and playback. This is not a reason to enable a previously disabled optional workload.

Download clients can resume work when restarted. Keep an eye on capacity and queues. A configuration snapshot taken after a repair can complement an earlier consistent app-state backup, but it does not make that older database backup current.

If you already use VM/filesystem snapshots, understand their consistency and external-storage limits. A snapshot on the same storage helps with some mistakes but does not survive loss of that storage and does not automatically include external media.

## Prove recovery before you need it

Choose a disposable VM or isolated restore location. Do not test by overwriting the working production copy.

1. Restore a selected backup without pointing the test copy at the writable production media tree or starting a second downloader against it.
2. Keep the matching application/database versions and the backup's configuration available. Block unintended notifications, imports and external writes during the rehearsal.
3. Verify the expected users can sign in, a known item is present and playable, application connections are understood, and valuable originals can be retrieved.
4. Record what was restored, what was excluded and the time needed. A photo restore must include originals and database; a metadata-only restore is not that test.
5. Retain a known-good recovery point until the newer one has been checked.

Samurai's account receipts deliberately bind application directories to an installation. Restoring files can change their inodes. For a verified **same-host, same-installation** restore, the reviewed `--restore-accounts` procedure checks existing backend accounts before accepting replacement directory identities. It does not restore files or reset passwords. A different host/UUID/address is not an automatic migration. See [restore-account recovery](../RECOVERY.md#accept-account-directories-after-a-verified-restore) before taking that action.

## Updating the installer is different from updating apps

There are three separate jobs:

| Job | What changes | What it does not mean |
| --- | --- | --- |
| Download a newer Samurai release | The setup/validation tools you can run | Your running apps or databases were upgraded |
| Resume a guided setup step | It reviews and applies that step against retained installation state | It is a factory reset or a universal application upgrade |
| Upgrade an application image | The software that reads its persistent database | An old Compose file can undo any database migration |

### Safely obtain newer setup tools

1. Read the release notes for your current and intended versions. Use a published stable release appropriate to your installation.
2. Download the release installer/source and its checksum file from the project's [releases](https://github.com/SamarieSystems/samurai-stack/releases). Keep the old source tools available until the newer version is checked.
3. Verify the documented checksum before execution. The source/portable verification procedure is in [Quickstart](../QUICKSTART.md). Extract source into a **new directory**, keeping all required files together.
4. Keep the existing canonical configuration, appdata, secrets, media and installation UUID. Replacing the downloaded source directory does not require deleting anything under the application or media roots.
5. Run normal `./setup` from that verified source directory to discover the retained configuration. Read the proposed step and changes. On an older unregistered installation, supply its actual private configuration path as described in [Recovery](../RECOVERY.md#keep-installation-state-separate-from-source).
6. Apply only the needed reviewed steps/maintenance action, then verify the affected behavior. Do not run every phase just because a new release exists.

Do not use `--fresh` for an update or recovery. It is for an intentional blank application session under strict separate conditions. Do not create a new deployment UUID or rename installed projects to escape an identity conflict.

In current releases, saved installation configuration lives outside the downloaded checkout. Deleting a ZIP/source folder does not erase the installation. Conversely, keeping a ZIP does not back up running application data.

### Upgrade application versions deliberately

The catalog pins images to immutable digests and retains per-site image locks. This makes retries repeatable; it does **not** automatically install security patches. There is no supported "pull latest and hope" upgrade path.

An application upgrade needs an owner to review upstream migration notes, obtain working backups, test the intended image family on a disposable copy, review image locks and a new deployment plan, then verify actual application behavior. Some applications require coordinated dependencies. Immich's server, machine learning and database-related images must remain a compatible set.

Avoid Dockge's pull/update/deploy controls as a substitute for that procedure. Do not hand-edit locks to make an untested version appear to be the previous one. Do not automatically downgrade a database by restoring only an old image reference. See [image pins and updating](../IMAGES.md#updating-deliberately) and [technical recovery](../RECOVERY.md).

## Keep this small maintenance record

| Field | Your private record |
| --- | --- |
| Current Samurai release and installation name | |
| Application image-lock backup date | |
| Full app-state backup date/location | |
| Media/originals backup date/location | |
| Last configuration-only snapshot | |
| Last isolated restore test and result | |
| Account/credential recovery owner | |
| Next review date | |

Use "not configured" or "not tested" where that is the truth. Knowing the gap is more useful than a misleading green status.
