# Storage before applications

The tool does not create NAS accounts or exports remotely. Synology, TrueNAS and ordinary Linux have different ACL, identity mapping and export controls. Guessing is how an installer damages an unrelated share.

Create a dedicated user/group or ID mapping with access only to the intended media tree. Decide your numeric media UID/GID first; the same IDs must mean the same access on each guest and NAS. Map a non-root media identity deliberately rather than enabling unrestricted root access to an entire NAS.

Guided `./setup 2` (or `bash ./samurai-setup.sh 2` with the verified portable
release) lists suitable mounted local Linux filesystems and NFS exports. It
can also connect one new export at an empty local mountpoint. It previews that exact connection and
asks before mounting or adding its persistent fstab entry. Existing conflicting
mounts/entries and nonempty mountpoints are refused; the original fstab is backed
up privately before one reviewed entry is appended. A previously connected
export can finish persistence on retry without being remounted. No NAS account,
export ACL, existing media or recursive permissions are changed. The advanced
storage stage still only validates/prepares a mounted tree; it does not manage
fstab by itself.

Export one media tree containing both downloads and libraries. Mount it on each relevant guest before stage 0. Separate NFS exports, ZFS datasets or sub-mounts can prevent hardlinks even on the same physical disks. The stage tests an actual hardlink between its download and movie-library directories.

For example, on a new guest with the NFS client installed:

```bash
sudo mkdir -p /mnt/media
# Substitute the exact dedicated export you created, not the NAS's whole volume.
sudo mount -t nfs4 -o rw,hard nas.example:/export/media /mnt/media
findmnt --target /mnt/media --output TARGET,SOURCE,FSTYPE,OPTIONS
```

When using the advanced commands, configure a persistent mount deliberately in your guest's `/etc/fstab`, then test that exact entry. The advanced storage engine does not edit fstab, guess an NFS version, or turn a failed mount into a local directory. The guided connection above instead records the actual mount type after a successful connection. Use the exact TARGET, SOURCE and FSTYPE that `findmnt` reports as `storage.expected_mount`. A NAS using NFSv3 may report `nfs`; do not claim `nfs4` in that case. Avoid soft NFS mounts for application writes.

When multiple guests share this tree, use the same mountpoint path, canonical
NAS/export spelling and NFS version on all of them. The marker records the exact
target/source/type as well as the deployment UUID; different mount paths or
hostname aliases are not interchangeable in this release. Never edit the
marker to bypass an identity refusal.

Choose a new child directory such as `/mnt/media/samurai`. Stage 0 makes this tree additively:

```text
samurai/
  library/movies/
  library/tv/
  library/music/
  downloads/usenet/complete/
  downloads/usenet/incomplete/
  downloads/torrents/movies/
  downloads/torrents/tv/
  downloads/torrents/manual/
  downloads/torrents/incomplete/
  downloads/music/complete/
  downloads/music/incomplete/
  photos/upload/
  photos/external/
```

Existing files are never moved, renamed or deleted, and existing ownership/modes are not recursively changed. Directory creation and tiny write/hardlink probes use the actual media UID/GID. Permission failures stop setup and leave existing ACLs intact.

The marker `.samurai-storage.json` contains a deployment UUID and expected mount identity. It contains no credential. It is created before subdirectories so interrupted preparation can resume. Other deployment markers are not adopted.

For an existing personal media collection, the safest start is a separate new tree. Point an additional read-only library at existing media after you have reviewed it. There is no automatic migration option in this release. A move/copy wizard needs independent manifests, collision checks, free-space calculations and recovery tests; it must not be disguised as directory setup.

Local storage also works. Set `storage.mode` to `local`, select an existing ext4/XFS/Btrfs/ZFS filesystem, and record its actual mount identity. New guided local setups record block-backed ext4/XFS/Btrfs sources as `UUID=<filesystem UUID>`, retaining the exact mountpoint, filesystem type and any Btrfs subvolume suffix. Linux `/dev/sdX` letters can change after reboot and are not a persistent identity. ZFS datasets and NFS exports retain their exact source names. This does not create a filesystem or allocate a disk. Do not place application databases on NFS/SMB.

Existing configurations that recorded a `/dev/sdX` source are not silently rebound. A changed device name still stops their applications until an explicit recovery review proves the original filesystem UUID and deployment marker, then updates the current configuration, marker and dependent receipts together. Do not simply replace the source with whichever disk currently occupies the old letter. New UUID-based guards continue to reject a different filesystem, missing/read-only mounts, different mountpoints, subvolumes or ownership markers.

## Upgrade an existing local installation

Use the verified [0.2.6 release installer](https://github.com/SamarieSystems/samurai-stack/releases/tag/v0.2.6)
to recover an existing local installation. From the portable release:

```bash
bash /path/to/samurai-setup.sh --upgrade-storage-identity
```

From the matching source checkout:

```bash
sudo ./setup --upgrade-storage-identity
```

The guided recovery displays the current mount identities and asks for the original media and application filesystem UUIDs. Confirm them from the original `/etc/fstab`, earlier inventory or a backup. Current disk letters are insufficient evidence. The tool checks the selected deployment marker, mountpoints, filesystem types and original application inodes, then saves a private review listing every affected control file. Applying that review updates the selected/canonical/stage configurations, dependent capacity/account/subtitle receipts and recognized generated guards together. Exact original control-file contents are saved under the existing installation's private `backups` directory. Unknown guard edits, different markers, wrong UUIDs and changed application inodes stop recovery.

This maintenance action does not restart containers or alter media, accounts, databases or VPN settings. After it completes, resume the existing media/torrent setup steps to regenerate their current boot units and verify service startup. Do not start fresh or delete application data. If several installations exist, pass the intended private `--config /path/to/site.json` explicitly.

Photos need an independent backup of **both originals and the database**, with a tested restore. A second share on the same NAS is not an independent failure domain.
