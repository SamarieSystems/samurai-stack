# Proxmox foundation

This stage runs **on an already installed Proxmox VE host**. It creates new
owned VMs on storage and a Linux bridge you explicitly select. It never installs
Proxmox, partitions disks, changes RAID, modifies host bridges or boot files,
rebinds host PCI devices, removes VMs, or deletes their disks.

Installing Proxmox remains a separate operator task because selecting the wrong
installer target can destroy existing data. Finish that installation first and
keep a tested backup of any existing important data.

Integrated graphics can be used by Jellyfin, but VM passthrough requires separate
firmware and IOMMU-group verification. See [hardware acceleration](HARDWARE-ACCELERATION.md)
before changing GPU ownership; the foundation installer deliberately leaves it
alone.

## Configuration

The Python API receives the `proxmox` subsection, not the entire deployment
configuration. Example values below are examples, not detected defaults:

```json
{
  "proxmox": {
    "deployment_id": "52a140bb-e49c-4416-9d7d-bc1d4575a6ed",
    "node": "pve",
    "storage": "local-lvm",
    "bridge": "vmbr0",
    "image_path": "/var/lib/vz/template/iso/debian-13-genericcloud-amd64.qcow2",
    "image_sha512": "REPLACE_WITH_THE_128_CHARACTER_SHA512_DIGEST",
    "ssh_public_key": "/root/operator.pub",
    "admin_user": "stackadmin",
    "state_dir": "/var/lib/samurai-stack/proxmox",
    "vms": [
      {
        "id": 120,
        "name": "media",
        "cores": 4,
        "memory_mib": 4096,
        "disk_gib": 64,
        "ipconfig": "ip=dhcp",
        "start": true
      },
      {
        "id": 121,
        "name": "torrents",
        "cores": 2,
        "memory_mib": 2048,
        "disk_gib": 32,
        "ipconfig": "ip=192.0.2.121/24,gw=192.0.2.1",
        "start": true
      }
    ]
  }
}
```

Use a fresh deployment UUID for each installation and preserve it for retries.
Use free VM IDs and unique VM names. Choose actual addresses on your network;
`192.0.2.0/24` is reserved for examples. The default address mode is DHCP.
`start: false` creates the guest definition without starting it. `admin_user`
defaults to `stackadmin`; root login and passwords are not configured.
`onboot` independently defaults to `true` so a host reboot brings the new
server VMs back. Set both `start: false` and `onboot: false` for a VM that
should remain off until you start it deliberately.

The image must already exist locally and must match the configured SHA512.
`qemu-img info` must report a standalone unencrypted qcow2 image without backing
or external data files. The stage does not download files or modify the image.
Use an official supported Debian cloud image, not `nocloud`, which skips
cloud-init. Debian publishes image types and its checksum verification approach
in its [official cloud image directory](https://cloud.debian.org/images/cloud/).
Current images should be retrieved with their checksum manifest over HTTPS;
do not assume that every current manifest has a detached signature.

Only an existing active image-capable storage and existing active Linux bridge
are accepted. The stage leaves at least 2 GiB or 10 percent of host RAM,
whichever is larger, after existing allocations and the requested guests.
It conservatively requires enough free storage for each new VM's full requested
disk capacity. Thin provisioning is not treated as free additional capacity.

## Inspect, plan, apply, verify

Run the Bash advanced entry point from the complete source directory on the
Proxmox host. It uses a full private site file with a nested `proxmox` section;
the configuration fragment above alone is not a complete CLI site file.
For a new foundation configuration:

```sh
./stack --config foundation-site.json init --example
sudo ./stack --config foundation-site.json proxmox init
sudo ./stack --config foundation-site.json proxmox inspect
sudo ./stack --config foundation-site.json proxmox plan --out proxmox-plan-01.json
sudo python3 -m json.tool proxmox-plan-01.json
```

`init --example` creates configuration only. The foundation wizard asks for
the actual node, image, bridge, storage, SSH public key and guest resources.
The example application addresses do not configure the Proxmox host. Inside
the new compact application guest, use `./setup` to choose its real addresses
and mounted storage before deploying applications. For separate media/torrent
guests, follow [one shared installation](ADVANCED.md#several-vms-one-installation)
and preserve its UUID and shared storage identity instead of creating unrelated
guided configurations. Do not apply the example application values.

Read the complete plan. Replace `REVIEW_HASH` with its exact hash, then apply
and verify:

```sh
sudo ./stack --config foundation-site.json proxmox apply --plan proxmox-plan-01.json --accept-plan REVIEW_HASH
sudo ./stack --config foundation-site.json proxmox verify
```

Use a root console if sudo is unavailable and omit `sudo` from those commands.
Use a new plan filename after a failed or changed attempt. Installing Proxmox
itself remains manual. The guided guest installer intentionally refuses the
hypervisor; only the separate `proxmox` commands run there.

For Python coordinators, the equivalent module operations are:

```python
from samurai_stack import proxmox

host = proxmox.inventory()
review = proxmox.plan(config["proxmox"])
# Persist and inspect review; explicitly accept its review_hash in the CLI.
result = proxmox.apply(config["proxmox"], review)
verification = proxmox.verify(config["proxmox"])
```

Inspection and planning make no changes. The plan contains exact argument arrays,
the host machine ID, image checksum, SSH public-key fingerprint and a review
hash. Apply verifies the document hash and regenerates the plan against current
state. A different host, image, key or configuration requires a new review.
The CLI requires acceptance of the exact review hash. Apply also takes an
exclusive per-deployment lock. A stale lock is retained for manual inspection,
never assumed safe to steal based only on its age or a recycled PID. Calling
the Python API directly is for trusted coordinators.

Apply creates guests with VirtIO networking and SCSI, a serial console,
cloud-init public-key login and an agent communication channel. It imports a
copy of the chosen OS image into a new owned VM disk and expands that guest
disk to the explicit requested size. This does not resize physical partitions
on the Proxmox host. OS disk expansion inside the VM depends on the chosen
image's cloud-init behavior and must be checked after boot.

Creation explicitly disables Proxmox's automatic cloud-init package upgrade.
Package installation belongs to the separately reviewed guest bootstrap; first
boot must not silently upgrade the image before that review.

The source image determines the guest operating system. The toolkit does not
assume that qemu-guest-agent, Docker or other application prerequisites are
already installed. Install and enable the agent during guest bootstrap; the
Proxmox VM's enabled agent option only exposes the communication channel.
[Proxmox cloud-init](https://github.com/proxmox/pve-docs/blob/master/qm-cloud-init.adoc)
and [guest-agent documentation](https://github.com/proxmox/pve-docs/blob/master/qm.adoc)
describe those separate responsibilities.

Apply returns `state: defined`, not application-ready. Verify checks ownership,
configuration, disks, boot order, running state and guest-agent reachability.
It deliberately returns `ready: false` until the separate guest bootstrap has
been verified. A reachable agent does not prove cloud-init or your applications
finished successfully. With DHCP and no functioning guest agent, find the lease
using your router or guest console rather than scanning an unknown network.

After SSH is available, run this **inside the guest through your SSH session,
not in cloud-init's own `runcmd`**:

```bash
cloud-init status --wait --format json
```

Check exit status zero, `extended_status`, `errors` and `recoverable_errors`.
Exit status 2 means cloud-init completed with recoverable errors. Running
`status --wait` inside cloud-init itself can deadlock.
[Cloud-init status and return codes](https://docs.cloud-init.io/en/latest/reference/cli.html)
document the distinction. Also verify the guest disk expanded, the intended
storage source is mounted, and the next stage's prerequisites actually work.

## Repeating a stage and recovering failures

Guests carry deployment and specification tags. An existing VM without both
matching tags is never adopted, even if its name matches. A repeat checks RAM,
CPU, SSH key, network and volume ownership before proceeding. Pending Proxmox
changes, a disk from another VM, an unused disk after an interrupted import,
or configuration drift cause an explicit stop.

Receipts live under `state_dir/deployment_id/vm-ID.json`, with private files and
atomic replacement. They record the failed step and retain prior-attempt
information on a retry. No rollback removes a VM or its disks. Inspect the
receipt and Proxmox task log; fix the specific prerequisite and rerun the same
reviewed configuration. Do not remove ownership tags as a recovery method.
An interrupted import that leaves an unused disk requires deliberate operator
inspection rather than another automatic import or disk deletion.

The reviewed configuration describes newly managed guests. It is not a general
reconciliation engine for an existing server. Renaming guests, changing their
CPU/RAM/network/keys, replacing disks, shrinking disks and adopting unrelated
VMs are intentionally outside this stage.

## GPU and small-machine support

CPU-only provisioning is supported. No NVIDIA runtime or GPU assignment is
required for the baseline. Software transcoding can be expensive, so start
with direct play and realistic concurrent-stream expectations.

**Automatic GPU passthrough is not implemented in this release.** A `gpu_pci`
configuration is rejected rather than quietly pretending passthrough is ready.
The inspect result includes PCI inventory for a separate review.

For a GPU-enabled host, identify the exact BDF, its current driver, whether it
is the host boot/display GPU, all its functions and its IOMMU group. Confirm
firmware support and console recovery before making any host changes. Do not
blindly bind the only host GPU, enable ACS override or allow unsafe interrupts.
Current upstream Proxmox docs state that IOMMU is normally enabled by default
on AMD and on Intel with kernels 6.8 or newer; an unconditional GRUB edit is
not a valid hardware-detection strategy.
[Upstream PCI passthrough guide](https://github.com/proxmox/pve-docs/blob/master/qm-pci-passthrough.adoc)
describes the actual prerequisites and Q35/PCIe options.

Normal full-device passthrough assigns a GPU to one VM. Put the accelerated
media containers together in that guest when they need to share it; do not
promise the same passed-through consumer GPU to several independent VMs.
GPU/firmware changes and any required reboot are a separately reviewed operator
procedure, not a side effect of running this stage.

The tests use a stateful fake Proxmox command boundary. They verify ownership,
review integrity, non-destructive retries and rejection paths. They do not
prove a cloud image boots on a physical Proxmox host; validate that on a spare
host or disposable VM IDs before calling the release production-tested.

## Bootstrap prerequisites inside each new guest

After creating the guest, copy this toolkit into it and run the separate guest
bootstrap there. It refuses the Proxmox host, bare metal and containers. It
supports Debian 12/13 and Ubuntu 24.04/26.04 on amd64/arm64 KVM/QEMU guests
booted with systemd. Python 3.11 or newer must already be available.

```bash
umask 077
sudo bash scripts/bootstrap-host.sh --plan > guest-plan.json
python3 -m json.tool guest-plan.json
```

Read the plan and copy its `review_hash`. Apply that exact plan:

```bash
sudo bash scripts/bootstrap-host.sh --apply \
  --plan-file guest-plan.json \
  --accept-plan PASTE_REVIEW_HASH_HERE \
  --ack-new-guest
```

Without `--apply`, this script only inspects. Applying requires all three
explicit acknowledgements above and root inside the guest. The plan binds the
guest's machine ID, OS/codename, architecture, installed packages, apt sources
and bootstrap script contents. If a partial attempt installed packages or
created repository files, generate and inspect a new plan before retrying.
Planning also uses root so retries can inspect the private bootstrap receipt.

It installs missing `ca-certificates`, `curl`, `nfs-common`, `qemu-guest-agent`,
Docker CE, its CLI, `containerd.io`, Buildx and the Compose plugin. It uses
Docker's official scoped apt repository, with a pinned public-key artifact.
Both [Docker's Debian key](https://download.docker.com/linux/debian/gpg) and
[Ubuntu key](https://download.docker.com/linux/ubuntu/gpg) were checked on
2026-10-23: primary fingerprint
`9DC858229FC7DD38854AE2D88D81803C0EBFCD88`, armor SHA256
`1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570`.
An upstream key rotation causes an explicit stop for review.

The script rejects conflicting distribution Docker/Podman/containerd/runc
packages instead of uninstalling them. It simulates each install and rejects
removals or existing-package upgrades. Existing unowned repository/key files,
duplicate Docker repositories and changed managed files are not overwritten.
Source snapshots and receipts remain private under
`/var/lib/samurai-stack/host-bootstrap`. It does not add anyone to the
root-equivalent Docker group.

Docker and the guest agent are started; no existing container is stopped or
recreated. Docker itself creates its normal bridge/firewall rules as part of
starting its daemon. No host interfaces, fstab, mounts, RAID, physical disks or
bootloader settings are changed by this script. Compose 2.24+ and active Docker
and agent services are required for success. Enable the VM's guest-agent
channel in Proxmox first. No `hello-world` image is downloaded automatically.

Docker's current supported OS/repository procedures are documented for
[Debian](https://docs.docker.com/engine/install/debian/) and
[Ubuntu](https://docs.docker.com/engine/install/ubuntu/). This script deliberately
does not follow their automatic conflict-removal step.
