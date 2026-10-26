# Troubleshooting without making it worse

[Guide home](README.md) | [Operating the server](OPERATING-THE-SERVER.md) | [Media and downloads](MEDIA-AND-DOWNLOADS.md) | [Backups and updates](BACKUPS-AND-UPDATES.md)

Start with what failed and the last thing that worked. Do not reinstall the stack, delete application folders or change storage identities to clear an error. Those actions can destroy the information needed to repair an otherwise recoverable problem.

## First decide which problem you have

| Symptom | First useful check |
| --- | --- |
| No app opens | Is the correct VM running, and can you reach an app from its own LAN? |
| Apps work on the LAN but not remotely | Check the remote computer's Tailscale connection and the exact documented forwarding URL |
| One app will not open | Inspect that app's project/unit logs and try its direct private URL |
| A download will not start | Identify metadata, source, VPN, free-space or intentional stopped state |
| Download completed but Jellyfin has nothing | Check the import in Sonarr/Radarr, not only the downloader |
| Playback buffers or fails | Check the actual client and whether Jellyfin is direct-playing or transcoding |
| Subtitles never appear | Check language/profile, provider login and one real subtitle search |
| It failed after reboot | Check VM autostart, service logs, the expected mount and the configured LAN address |
| An account stopped working | Confirm which application's account you are using; use that app's supported recovery |

## Nothing opens

1. Confirm the application VM is running in Proxmox. Starting the browser or Dockge on your laptop does not start the server.
2. Use the private LAN link from the handover note on a device on that LAN. Check the current/reserved VM address; a hostname is useful only if its name resolves correctly.
3. If LAN access works, troubleshoot the remote connection separately. Tailscale must be signed in and permitted on both ends. Use the VM's address, not a different machine in the tailnet.
4. If LAN access also fails, inspect the generated systemd unit and recent logs in the application VM. Use [the unit-name guide](OPERATING-THE-SERVER.md#find-the-right-systemd-unit).
5. Check the actual app URL. Docker saying running does not prove that its host port was published or its application is ready.

An SSH browser tunnel needs its terminal/session to stay alive. A server reboot normally ends that session. Reconnect the documented tunnel. A separately commissioned persistent Tailscale forward may survive reboots; that is not automatic just because Tailscale was installed. See [private remote access](../REMOTE-ACCESS.md).

Do not open router ports, enable public Funnel, advertise your home LAN, or bind all apps to every interface as a shortcut. First establish which private connection is failing.

## Startup says storage is missing or wrong

This refusal is there to keep a missing NAS or HDD from becoming an empty directory on the VM's system disk.

In the application VM, the following are **read-only** examples. Replace `/mnt/media` with the actual expected mountpoint and `/mnt/media/samurai` with your configured data root before running them:

```bash
findmnt --mountpoint /mnt/media --output TARGET,SOURCE,FSTYPE,OPTIONS
findmnt --target /mnt/media/samurai --output TARGET,SOURCE,FSTYPE,OPTIONS
df -hT / /mnt/media/samurai
```

Compare the result with the private handover/configuration:

- The exact mountpoint must be present, with the expected filesystem UUID or NAS export and filesystem type.
- Its options must permit the intended writes. A read-only mount requires diagnosis.
- `findmnt --target` reports the filesystem containing that path. If it unexpectedly reports the VM's root filesystem, that is not proof the NAS/HDD mounted correctly.
- Having a folder with the expected name is insufficient. Having the correct disk size alone is insufficient.

For a NAS, confirm it is online and exporting the intended share to this guest. For a passed-through local disk, confirm it is still attached to the correct VM and mounted by its original persistent identity. Keep the expected filesystem UUID and deployment marker unchanged.

Older local installations may have saved a volatile `/dev/sdX` name that changes after reboot. The current [reviewed UUID upgrade](../STORAGE.md#upgrade-an-existing-local-installation) proves the original filesystem and updates dependent control files together. Do not substitute whichever disk now occupies the old letter, delete the marker, or switch off the guard.

Once the actual mount is restored, current units retry automatically. If you deliberately stopped a unit during maintenance, start that same unit after the checks pass. Verify the real apps afterward.

## Download stuck, stopped or slow

Open the downloader that owns the job, and read its state before changing anything.

| State or observation | What it usually means | Next check |
| --- | --- | --- |
| qBit fetching metadata / size not known | The magnet's file metadata has not arrived | Peer/tracker availability, DHT and VPN connectivity; do not call it a completed zero-byte file |
| Stopped/paused | A person, application or capacity policy stopped it | Free space, the saved capacity status, and whether you deliberately stopped it |
| Downloading with no interested/connected peers | Sources may not currently supply data | Trackers/peers and another authorized known-good torrent |
| Seeding at a low rate | Other peers may not request pieces | Interested peers and connection/forwarding status; unlimited settings do not manufacture demand |
| SAB authentication or TLS failure | Provider access, hostname or trust failed | NNTP service details, subscription and the actual reported error |
| Download finished; Starr has an import warning | Downloading succeeded but library import did not | Sonarr/Radarr Activity, category, path, free space and permissions |

For Usenet, the NNTP server is your **Usenet provider**, not the indexer. An indexer helps find an item; the provider delivers its payload. Your website password may differ from its NNTP service password.

For torrents, keep qBittorrent in Gluetun's namespace and bound to `tun0`. A VPN failure or invalid forwarded-port state can intentionally block transfers. Do not select a physical interface to get past that refusal. A real kill-switch interruption test belongs in controlled commissioning, not random troubleshooting while others rely on the tunnel.

The simple Samurai torrent stage has **no ClamAV approval queue**. Manual magnets can be added through the normal authenticated qBit UI. If another installation has a security hold, identify that installation's policy and owner before changing it. This guide does not authorize bypassing a different stack's controls.

If the optional capacity watcher stopped incomplete qBit downloads, it does not automatically resume them. Confirm enough free space for download plus extraction/import headroom, then deliberately resume the selected jobs. See [capacity safeguards](../CAPACITY.md). Do not clear space by deleting unknown files or Docker volumes.

## Downloaded, but not available to watch

Follow the actual chain: **request -> search -> download -> import -> Jellyfin discovery -> playback**.

1. Find the title/episode in Sonarr or Radarr. Inspect its Activity/import warning and History.
2. Confirm it has an actual imported file, not only a monitored title or a completed downloader row.
3. Confirm the file is in the intended library tree, using the same underlying storage as the downloader. See [media and download paths](MEDIA-AND-DOWNLOADS.md).
4. Check the intended Jellyfin library contains that folder and the expected library is enabled in Seerr.
5. Allow the configured library notification/scan to finish, then check Jellyfin directly. If a scan is needed, select the affected library rather than repeatedly rescanning everything.

Do not fix an import error by adding arbitrary remote-path mappings. A path mapping cannot turn separate filesystems into one filesystem or repair a wrong mount. Do not point a downloader's working directory directly at the final movie/TV library.

## Playback is slow

Try one known file on one client. In Jellyfin, inspect the active playback information if available to the administrator. Determine whether the server is sending the original file directly or converting video/audio for the client.

Direct playback and CPU transcoding have very different hardware requirements. CPU-only is supported, but a small CPU may not convert demanding video in real time. Resolution, bitrate, unsupported codecs and some subtitle rendering can change the work required. Check client compatibility and network quality before changing every quality setting.

GPU acceleration is a separate commissioning task: hardware exposure, guest drivers/device permissions, Jellyfin configuration and an actual transcode must all work. Selecting a GPU mode in setup alone does not prove it. See [service hardware notes](../SERVICES.md#photos-and-music).

## Subtitles or accounts are the problem

For subtitles, check the title's language/profile, embedded subtitles, the Bazarr provider result and the provider's own authentication/error. A saved provider entry does not prove a successful download. Start with one title rather than a whole-library search. See [subtitle setup](../SUBTITLES.md).

For logins, distinguish Linux/SSH, Proxmox, Jellyfin, Seerr, Dockge/Kuma and provider accounts. Seerr's managed media users use **Sign in with Jellyfin**. qBit has generated private local credentials; it does not necessarily share the media administrator's password. Existing Dockge/Kuma owners are retained, not reset when setup is retried.

Use the affected application's supported password recovery with its owner and a backup. Do not edit an arbitrary database row or delete appdata to force a first-user wizard.

## Collect a useful support report

Gather this small set before making another change:

1. Release version, guest OS and whether one app or the whole stack fails.
2. The exact time, action and visible error; what still works on the LAN versus remotely.
3. The affected systemd unit's status and a bounded section of its recent journal.
4. Expected and observed mount information, free space and the relevant downloader/import state.
5. What changed immediately beforehand, including reboot, power loss, storage, router or credentials.

From the directory containing the verified source launcher, these are read-only recovery/version commands:

```bash
bash ./setup --version
sudo bash ./setup --recover
```

Recovery inventory does not repair, adopt or delete anything. Keep its output private until reviewed: hostnames, addresses, usernames, paths and personal filenames can identify people. Vendor logs can include API keys, provider passwords, cookies, tokens or magnet URLs. Redact those values before sharing. Do not attach an entire database, secret directory or unreviewed diagnostic bundle to a public issue.

Technical recovery procedures: [Recovery](../RECOVERY.md), [Storage](../STORAGE.md), [Security](../SECURITY.md), [first-machine acceptance](../ACCEPTANCE.md).
