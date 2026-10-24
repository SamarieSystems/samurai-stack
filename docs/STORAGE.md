# Storage before applications

The tool does not create NAS accounts or exports remotely. Synology, TrueNAS and ordinary Linux have different ACL, identity mapping and export controls. Guessing is how an installer damages an unrelated share.

Create a dedicated user/group or ID mapping with access only to the intended media tree. Decide your numeric media UID/GID first; the same IDs must mean the same access on each guest and NAS. Map a non-root media identity deliberately rather than enabling unrestricted root access to an entire NAS.

Export one media tree containing both downloads and libraries. Mount it on each relevant guest before stage 0. Separate NFS exports, ZFS datasets or sub-mounts can prevent hardlinks even on the same physical disks. The stage tests an actual hardlink between its download and movie-library directories.

For example, on a new guest with the NFS client installed:

```bash
sudo mkdir -p /mnt/media
# Substitute the exact dedicated export you created, not the NAS's whole volume.
sudo mount -t nfs4 -o rw,hard nas.example:/export/media /mnt/media
findmnt --target /mnt/media --output TARGET,SOURCE,FSTYPE,OPTIONS
```

Configure a persistent mount deliberately in your guest's `/etc/fstab`, then test that exact entry. The installer does not edit fstab, guess an NFS version, or turn a failed mount into a local directory. Use the exact TARGET, SOURCE and FSTYPE that `findmnt` reports as `storage.expected_mount`. A NAS using NFSv3 may report `nfs`; do not claim `nfs4` in that case. Avoid soft NFS mounts for application writes.

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

Local storage also works. Set `storage.mode` to `local`, select an existing ext4/XFS/Btrfs/ZFS filesystem, and record its actual mount identity. This does not create a filesystem or allocate a disk. Do not place application databases on NFS/SMB.

Photos need an independent backup of **both originals and the database**, with a tested restore. A second share on the same NAS is not an independent failure domain.
