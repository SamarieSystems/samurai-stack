# New Linux guest prerequisites

Run this inside each new VM, never on the Proxmox hypervisor. Proxmox itself
is installed manually; [the foundation stage](PROXMOX.md) creates new guest VMs
on explicitly chosen existing storage and bridges.

Requirements:

- Debian 12/13 or Ubuntu 24.04/26.04, amd64 or arm64.
- A KVM/QEMU VM booted with systemd as PID 1.
- Python 3.11 or newer and this toolkit copied into the guest.
- Root through `sudo` for apply, with the Proxmox guest-agent option enabled.

Inspect first. This command performs no package installation or repository
changes; redirecting its output creates the private plan file you requested:

```bash
umask 077
bash scripts/bootstrap-host.sh --plan > guest-plan.json
python3 -m json.tool guest-plan.json
```

Read `effects`, OS/codename, architecture, machine ID, package list and repository
information. Copy the plan's `review_hash` into this command:

```bash
sudo bash scripts/bootstrap-host.sh --apply \
  --plan-file guest-plan.json \
  --accept-plan PASTE_THE_REVIEW_HASH_HERE \
  --ack-new-guest
```

All three apply options are required. A change to the machine, OS, apt sources,
installed packages or script invalidates the plan. After a partial installation,
generate and review a new plan before retrying. No stale lock is stolen; inspect
the private receipt/logs instead of deleting existing application files.

The bootstrap installs missing CA certificates, curl, NFS client support,
qemu-guest-agent, Docker CE and its official CLI/containerd/Buildx/Compose
packages. It uses Docker's scoped apt repository and a pinned official public-key
artifact. It rejects conflicting distribution Docker, Podman, containerd or runc
packages rather than removing them. Simulated package upgrades/removals are
rejected. Existing unowned or changed key/repository files are not overwritten.

Receipts and apt-source backups live under
`/var/lib/samurai-stack/host-bootstrap`. An upstream signing-key rotation stops
for review. Nobody is automatically added to the root-equivalent Docker group.
No disk layout, RAID, mounts/fstab, Proxmox host networking, bootloader or media
is changed. Starting Docker does create Docker's normal bridge/firewall rules.

Success reports `state: ready` only after Docker and qemu-guest-agent are active,
Docker's local server responds and Compose is at least 2.24. It does not download
or run a test container. If the guest agent fails, confirm Proxmox exposes its
virtio guest-agent channel, then inspect `journalctl -u qemu-guest-agent` in the
guest. The script does not reboot or force-stop the VM to fix that channel.

After bootstrap, check guest initialization from your SSH session:

```bash
cloud-init status --wait --format json
docker compose version
sudo systemctl is-active docker qemu-guest-agent
```

Cloud-init must return exit status zero without errors; status 2 means it
completed with recoverable errors. Do not put `cloud-init status --wait` inside
cloud-init's own `runcmd`, where it can wait for itself forever. Verify guest
disk expansion and the intended storage mount before application stages.

Official procedures and supported releases:
[Docker on Debian](https://docs.docker.com/engine/install/debian/),
[Docker on Ubuntu](https://docs.docker.com/engine/install/ubuntu/),
[cloud-init status](https://docs.cloud-init.io/en/latest/reference/cli.html).
The toolkit deliberately substitutes conflict refusal for Docker's documented
package-removal step.
