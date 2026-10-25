# Install or repair Samurai Stack

For a new installation, follow [Guided setup](QUICKSTART.md) and the
[deployment-night checklist](DEPLOYMENT-NIGHT.md). Run setup inside the intended
Linux guest. Physical Proxmox installation, disk selection, NAS export permissions
and GPU passthrough remain separate decisions.

An existing Samurai installation keeps its current `site.json`, installation
identity, credentials and data paths. Use the focused action below when the task
is to enable hardlinks in already-running Sonarr and Radarr.

## Enable hardlinks on an existing installation

Open a terminal on the existing **media VM**, enter the source directory that
contains its saved `site.json`, and run:

```sh
./setup --hardlinks
```

The guided action reads both settings first. If either is disabled, it asks
before enabling them, then independently checks the saved values. For an
existing configuration elsewhere, pass its actual path with `--config`.

The equivalent advanced commands are:

```sh
sudo ./stack --config site.json hardlinks
sudo ./stack --config site.json hardlinks --apply
sudo ./stack --config site.json hardlinks
```

The default command is read-only. `--apply` changes only Sonarr and Radarr's
`copyUsingHardlinks` boolean, the API setting behind **Use Hardlinks instead of
Copy**. It preserves their other media-management settings and reads both flags
back. The action checks that the configured local API ports belong to this
installation's running, owned containers before contacting them.

This repair does not redeploy or restart containers, recreate accounts, scan or
import media, move downloads, migrate directories, or change VPN settings. Other
applications do not need to be enrolled first. Keep the existing configuration;
creating another installation identity would prevent the ownership checks from
matching the running services.

The standalone release asset, `samurai-hardlinks.pyz`, packages the same
focused repair for machines using an older source ZIP. Obtain it from the
[alpha 4 release](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.1.0-alpha.4)
and verify its supplied `SHA256SUMS` checksum.
Its command interface is:

```sh
sudo python3 samurai-hardlinks.pyz --config /path/to/existing/site.json
sudo python3 samurai-hardlinks.pyz --config /path/to/existing/site.json --apply
```

Use the existing media VM and configuration for that artifact too. An older
source directory does not need replacing to run the standalone repair.

## What the paths mean

[TRaSH's Docker layout](https://trash-guides.info/File-and-Folder-Structure/How-to-set-up/Docker/)
distinguishes host storage paths from container paths. Its important design is a
shared tree that lets Starr see downloads and libraries together. The host
prefix can follow the actual disk or NAS mount. Separate container mounts for
downloads and libraries can prevent hardlinks even when their host paths look
related.

Samurai maps the chosen `storage.data_root` to `/data` in Starr and the download
clients. The library and download paths are children of that same tree:

```text
<existing data_root>/
  downloads/torrents/{movies,tv,manual,incomplete}
  downloads/usenet/{complete,incomplete}
  library/{movies,tv,music}
```

| Host path | Purpose |
| --- | --- |
| `/opt/samurai` | Conventional default for this toolkit's local application state, configuration and databases. A different supported, dedicated local directory can be selected during initial setup. This is a project convention, not a claim that the applications require that prefix. |
| `/srv/samurai-data` | Guided default for a new local media tree on an existing supported Linux filesystem. |
| `/mnt/media/samurai` | Example of a dedicated child on an existing reviewed disk or NAS mount at `/mnt/media`. An installation already using this path should retain it. |

Changing `/mnt/media/samurai` to `/srv/samurai-data` is not a hardlink repair.
Keep the actual saved mount and path. Hardlinks require the source and target
to share a filesystem that supports them, with suitable permissions; a shared
prefix alone does not prove that. No path or media migration is needed to enable
the application flags.

## Settings verification versus import proof

A successful repair proves both API flags are `true`. It does not prove a real
Starr import used hardlinks, and it does not replace existing duplicate copies
with hardlinks. Storage setup's temporary hardlink probe establishes a separate
filesystem capability check.

For end-to-end proof, use one controlled, authorized torrent that remains
seeding after its normal Starr import. Compare that file's download and library
paths on the media host with this read-only command, substituting the actual
two paths:

```sh
stat -c 'device=%d inode=%i links=%h %n' -- \
  '/path/to/download-file.mkv' '/path/to/imported-file.mkv'
```

Both paths must report the same device and inode, with link count at least two.
Equal sizes alone do not prove a hardlink. Choose a normal completed-download
import for this test; a manual import deliberately set to Move can consume the
source instead. The repair itself does not initiate either import mode.
