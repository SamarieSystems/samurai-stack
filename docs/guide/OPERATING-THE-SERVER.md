# Operating the server

[Guide home](README.md) | [Daily use](DAILY-USE.md) | [Troubleshooting](TROUBLESHOOTING.md) | [Backups and updates](BACKUPS-AND-UPDATES.md)

You do not need to run the installer every time you want to watch something. Once the server is commissioned, use Jellyfin for playback, Seerr for requests and the download applications to inspect progress. This page covers the occasional jobs that need the server owner.

## Know which machine you are controlling

A Proxmox installation can contain several virtual machines. **The Proxmox host** starts the virtual machines. **The application VM** runs Docker and your media applications. Dockge inside one VM controls that VM's Docker daemon; it does not control every VM on Proxmox.

Keep the following in a private handover note:

| Item | Write down |
| --- | --- |
| Application VM | Its name, reserved LAN address and SSH administrator |
| Remote access | Its Tailscale name/address and the exact working browser links, if configured |
| Installation name | The `deployment_name`, used in systemd unit names |
| Compose project names | The names shown in Dockge for media, torrents and management |
| Application storage | The configured `install_root` |
| Media storage | The configured `storage.data_root`, its mountpoint and expected filesystem/export |
| Private configuration | The installation's canonical `site.json` path |
| Credentials | Which password-manager entries are for each app; do not put passwords in a public document |
| Recovery | Backup location, date of the last restore test and who can recover owner accounts |

The examples below use installation name `homelab`, application storage `/opt/samurai` and media storage `/mnt/media/samurai`. **Your saved choices take precedence.** A custom Compose name does not change the installation name or numeric systemd unit.

## Use Dockge to understand what is running

1. Open the private Dockge URL from your handover note and sign in as its owner.
2. Find the expected media, torrent or management project by name.
3. Inspect its container status and recent logs. Open the affected application's own browser URL too.
4. If you need to restart a complete project, use the systemd procedure below. Do not press Dockge deploy/restart controls at the same time.

Dockge can read generated Compose files, show logs and open container terminals. A terminal inside a container is different from the VM's SSH shell. A command that checks VM disks belongs in the VM shell.

Samurai's systemd units supervise whole stages in the foreground. Dockge is another Docker controller. Stopping or redeploying individual containers underneath systemd can make systemd restart the stage or leave the two controllers disagreeing. Use one lifecycle authority: **systemd for generated Samurai projects**.

### What the statuses actually mean

| Signal | What it proves | What to check next |
| --- | --- | --- |
| A container is running | Its main process has not exited | Open the real app URL |
| A container is healthy | Its particular health check passed, if one exists | Sign in and perform a relevant task |
| A systemd stage is active | Its supervised process is active; current startup checks passed at startup | Check individual app behavior and monitoring |
| Setup says verified | The last setup verification passed | This is saved history, not continuous monitoring |
| An Uptime Kuma monitor is up | That monitor's specific network check passed | A TCP check alone does not prove a login, download or playback |
| A request is available in Seerr | Its library availability was reported | Play the intended episode/movie in Jellyfin |

Current startup checks also inspect actual Docker port publications and network attachments. They help catch failed starts, but do not replace ongoing monitoring or a real playback test.

## Find the right systemd unit

Run this **read-only** command in the application VM's SSH shell:

```bash
systemctl list-unit-files 'samurai-*.service'
```

Identify the units for your installation. The final number is the engine stage, which differs from the guided setup menu number:

| Workload | Guided setup menu | Engine stage / unit suffix |
| --- | --- | --- |
| Media apps | 3 | `2.service` |
| VPN and torrents | 4 | `3.service` |
| Management | 5 | `4.service` |
| Photos, when enabled | 6 | `5.service` |
| Music, when enabled | 6 | `6.service` |

For installation `homelab`, the media unit is `samurai-homelab-2.service`. Confirm your actual name from the command output before copying these examples.

Read status and the most recent 80 log lines:

```bash
sudo systemctl status samurai-homelab-2.service --no-pager
sudo journalctl -u samurai-homelab-2.service -n 80 --no-pager
```

These commands do not start, stop or delete anything. Logs may contain private information; inspect them before sharing.

## Deliberately restart or stop one project

First check that storage is present, there is free space, and nobody is downloading/importing something you must not interrupt. Use [Troubleshooting](TROUBLESHOOTING.md) if the stage is blocked. A restart is not a fix for a wrong mount or missing VPN credentials.

The following commands **change service state**. Each command affects the whole selected stage, so restarting the media unit briefly interrupts all its media applications. Use only the one action you intend, with your confirmed unit name:

```bash
# Restart the media project.
sudo systemctl restart samurai-homelab-2.service
```

```bash
# Stop the media project for deliberate maintenance.
sudo systemctl stop samurai-homelab-2.service
```

```bash
# Start it again after maintenance and storage checks.
sudo systemctl start samurai-homelab-2.service
```

Then inspect status, open its apps and test the affected task. Restarting recreates runtime containers from the saved pinned recipe and retains bind-mounted application data and media. It is still an interruption, so do not use it casually during an import or backup.

An explicit systemd stop stays stopped for that session. It does **not** disable the unit's next-boot startup. A persistent decision to keep a stage off needs an administrator to review the enabled unit, application connections and dependencies together. Do not turn off its safety guard to accomplish that.

## What should happen after a reboot?

There are two separate startup responsibilities:

1. Proxmox must be configured to start the application VM when the physical host boots. This is a VM setting and must be checked on the actual host.
2. The VM's enabled Samurai systemd units start their stages. Data-dependent stages verify their expected storage; all stages wait for their configured LAN address, and the VPN stage also waits for a usable default route.

Current units retry failed starts every 15 seconds. They refuse missing, read-only or incorrect storage rather than writing downloads into an accidental local directory. Management is independent of media storage, but it cannot run while its VM is off.

Do not add Docker restart policies as a workaround. They could start containers before the storage/VPN checks. Check the unit journal to learn what startup is waiting for. Startup checks have bounded timeouts; an unavailable prerequisite can keep the unit retrying until you fix that prerequisite.

A guest reboot test proves the guest path. It does not prove that a full power outage, the physical host, a separate NAS and the router all recover correctly. Test those separately when you can tolerate the interruption.

## Find your files and configuration

| Location | What belongs there |
| --- | --- |
| `INSTALL_ROOT/stages/PROJECT/compose.yaml` | The saved recipe for one named Compose project |
| `INSTALL_ROOT/appdata/SERVICE` | That application's databases, settings, cache and logs |
| `INSTALL_ROOT/secrets` | Private local credentials and VPN inputs |
| `INSTALL_ROOT/state` | Installer receipts, image locks and optional capacity policy/status |
| `INSTALL_ROOT/backups` | Private pre-change control-file backups; not a complete media backup |
| `DATA_ROOT/library` | Movies, TV and music libraries |
| `DATA_ROOT/downloads` | Download-client working/completed files |
| `/var/lib/samurai-stack/installations/DEPLOYMENT-UUID/site.json` | The host-bound saved installation configuration |
| `/var/lib/samurai-stack/setup` | Guided progress, drafts and private diagnostics |

`INSTALL_ROOT`, `DATA_ROOT`, `PROJECT` and `DEPLOYMENT-UUID` are descriptions to replace with your actual values, not literal directory names. Inside Starr/download containers, the shared data tree is mounted at `/data`. The corresponding host path may be `/mnt/media/samurai`; both refer to the same backing files in a correctly configured installation.

Do not manually rearrange generated project folders or edit a project name as if it were only a display label. Its directory, YAML name, systemd command and Docker labels need to agree. Ask for a reviewed migration if you want to rename an installed project.

## A useful five-minute check

Once a week, or after a power/network problem:

1. Open Jellyfin, Seerr and your chosen downloader. Play a known item and inspect an active or recently completed download.
2. Check free space on both application and media storage. Review queues before accepting large new jobs.
3. Check Uptime Kuma, if commissioned, and confirm your chosen notification channel still receives a test alert occasionally.
4. Check the latest backup completed and is accessible outside this VM. A green container is not a backup.
5. Record anything unusual rather than resetting the installation.

Further detail: [service layout](../SERVICES.md), [private remote access](../REMOTE-ACCESS.md), [capacity safeguards](../CAPACITY.md), [recovery](../RECOVERY.md).
