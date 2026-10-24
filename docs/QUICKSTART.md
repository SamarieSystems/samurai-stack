# Your first Samurai Stack

Start with a new Linux VM and disposable files. Finish the media stage before
connecting its torrent client, then add photos or music when you will use them.
Management can run earlier to help inspect the other stages. Each stage has a
separate plan, apply and verification result, so a failed optional component
does not require starting the installation again.

This guide is the operator walkthrough. The dated [validation record](VALIDATION.md)
states which parts of the current alpha have actually been exercised.

## 1. Choose where it runs

Use an existing supported Linux VM, or follow [the Proxmox foundation guide](PROXMOX.md)
to create new guests on an already installed Proxmox host. Installing Proxmox
onto a physical disk remains your separate, deliberate operation.

The application host needs Python 3.11+, systemd, Docker Engine and Compose
2.24+. [HOST-BOOTSTRAP.md](HOST-BOOTSTRAP.md) provides a separate reviewed
bootstrap for a new Debian/Ubuntu guest. Application stages belong inside the
guest, not on the hypervisor. A CPU-only machine is a valid starting point;
leave hardware acceleration set to `cpu` until playback works.

Size the guest for the applications you enable. Immich currently requires at
least 6 GB RAM and recommends 8 GB for photos alone; other stages need their own
headroom. Its v3 machine-learning image on amd64 also requires x86-64-v2 CPU
instructions. See the [upstream requirements](https://docs.immich.app/install/requirements/).
The small validation VM used swap and sequential stages; its 3 GiB allocation
is not a recommendation for an always-on combined stack. Leave photos and other
optional features off on a smaller host until you have the capacity.

Keep this source checkout on the guest. Run the commands below from its root,
where the `stack` file and `samurai_stack` directory are present. Use a published
alpha source archive or a reviewed Git tag rather than mixing files from
different releases. The toolkit uses the Python standard library, so it does
not need `pip install`.

If Git is already available on the guest:

```sh
git clone https://github.com/SamarieSystems/samurai-stack.git
cd samurai-stack
git checkout v0.1.0-alpha.1
python3 stack --version
```

Otherwise, download the source archive for that release on your computer and
copy the extracted source directory into the guest over SSH. Copy the complete
directory, including `scripts`, rather than only the `stack` launcher.

For the first rollout, one compact application VM is the shortest path. To
separate media and torrents, read the multi-VM note below before creating
configuration on each machine.

## 2. Connect storage, then describe the installation

Create and mount the intended NAS export yourself, or select an existing local
Linux filesystem. [STORAGE.md](STORAGE.md) covers numeric identities, NFS and
the directory layout. Choose a new dedicated data subdirectory. Keep family
photos, documents and the existing media collection outside this first test.

Check the mount before configuration:

```sh
findmnt --target /mnt/media --output TARGET,SOURCE,FSTYPE,OPTIONS
id
python3 stack init
python3 stack --config site.json validate
```

Use your actual mountpoint instead of `/mnt/media`. The wizard asks for:

| Setting | What to enter |
| --- | --- |
| Deployment name | A short, unique name for this installation, such as `homelab`. |
| Role and host addresses | `all` for one VM, or the actual role/address map for separated guests. |
| Application directory | A new local directory, such as `/opt/samurai`. Databases stay here. |
| Media UID/GID | The numeric account/group that can access the intended data tree on every relevant host. |
| Data directory | A dedicated child of the actual mounted filesystem. |
| Expected mount | The exact TARGET, SOURCE and FSTYPE reported by `findmnt`. |
| Optional features | Enable only what you will commission now. Choose Usenet, torrents, or both. |
| Download preference | The preferred protocol when both are available; quality/indexer rules still apply. |
| Acceleration | `cpu` initially; GPU access is a later deliberate choice. |

`init` writes private `site.json`; it does not install services or modify disks.
Keep that file. Re-running `init` will not overwrite it. Review later changes
with a local editor and run `validate` again.

For a torrent-only installation, turn Usenet off, enable torrents and choose
`torrents` as the preferred protocol. Stage 2 still supplies the media server,
requests, indexers and library managers; stage 3 adds its download client.

### Several VMs, one installation

Create the site configuration once, then copy it privately to each relevant
guest. Preserve the same `deployment_id`, deployment name, host map, media
UID/GID and shared storage identity. On each copy, change `role` and
`bind_address` to describe that guest. Local application state remains local
to each guest, even when `install_root` uses the same path name.

Do not independently run `init` on every guest and expect the resulting UUIDs
to share one storage marker. A different UUID is a different installation.
Do not change a storage marker to work around that refusal.

The mount's reported target/source/type must match the configuration on that
guest. An identically named directory on another machine is not proof that it
contains the same backing files.

## 3. Prepare and verify the data tree

```sh
sudo python3 stack --config site.json plan 0 --out plan-0-01.json
sudo python3 -m json.tool plan-0-01.json
```

Read the plan before applying it. Confirm the host and expected mount. Copy
the exact `review_hash` shown in the plan into `REVIEW_HASH` below:

```sh
sudo python3 stack --config site.json apply 0 --plan plan-0-01.json --accept-plan REVIEW_HASH
sudo python3 stack --config site.json verify 0
```

Plan files created through `sudo` have restricted permissions, so use `sudo`
to read them. The uppercase `REVIEW_HASH` is a placeholder, not a literal
argument to keep. The actual hash changes when the reviewed state changes.

Success means the expected mount, marker, directories and media identity
passed verification. Stage 0 creates missing directories and tests hardlinks.
It does not reorganize existing media or repair permissions recursively.

If preparation stops, fix the reported mount or identity issue first. Generate
a new plan with a new filename, such as `plan-0-02.json`, and retry. Preserve
the failed run's files and receipts for diagnosis.

## 4. Start media, enroll accounts, connect the applications

```sh
sudo python3 stack --config site.json plan 2 --out plan-2-01.json
sudo python3 -m json.tool plan-2-01.json
sudo python3 stack --config site.json apply 2 --plan plan-2-01.json --accept-plan REVIEW_HASH
sudo python3 stack --config site.json access 2
```

Use stage 2's new hash. Inspect `compose_preview` for the selected services,
LAN bindings and data paths. Open the addresses returned by `access` from a
browser on your private network.

1. Complete initial authentication in Sonarr, Radarr and Prowlarr. Add your
   own indexers and choose suitable quality profiles.
2. If Usenet is enabled, configure SABnzbd's Web UI authentication, complete its
   provider setup and run its server connection test. The installer supplies no
   paid subscription or provider login.
3. Create the Jellyfin administrator and an API key. Complete Seerr's initial
   Jellyfin sign-in against this installation's Jellyfin address.
4. Configure Bazarr's Web UI authentication, select subtitle languages/providers
   and obtain its API key. A running frontend does not prove authentication is on.

Import the keys through hidden local prompts:

```sh
sudo python3 stack --config site.json secret jellyfin_api_key
sudo python3 stack --config site.json secret seerr_api_key
sudo python3 stack --config site.json secret bazarr_api_key
sudo python3 stack --config site.json configure 2
sudo python3 stack --config site.json verify 2
```

Generated Starr keys and SABnzbd's key are read from their local configuration
where supported. Missing credentials produce specific pending tasks. Do not
put keys in `site.json`, shell arguments, a public issue or a shared transcript.

`configure` creates and tests supported connections. Rerunning it verifies
the installation's named resources; conflicting existing settings require a
deliberate decision rather than silent replacement. `verify` checks their
readback. [SERVICES.md](SERVICES.md) explains individual provider choices.

Before inviting users, complete one authorized test download, verify its real
file/size/path, import it and play it in Jellyfin. An API verification result
does not prove that your provider credentials or playback device work.

## 5. Add VPN torrents

Check [the VPN input table](SERVICES.md#vpn-and-manual-magnets) for the selected
provider/protocol. Import those private inputs before applying stage 3. For
example, a provider-managed WireGuard setup requests:

```sh
sudo python3 stack --config site.json secret vpn_wireguard_private_key
```

A custom VPN uses its reviewed configuration file instead. The exact secret
names and forwarding options depend on the selected protocol; do not reuse an
unrelated account password as a WireGuard key.

```sh
sudo python3 stack --config site.json plan 3 --out plan-3-01.json
sudo python3 -m json.tool plan-3-01.json
sudo python3 stack --config site.json apply 3 --plan plan-3-01.json --accept-plan REVIEW_HASH
sudo python3 stack --config site.json access 3
sudo python3 stack --config site.json configure 3
sudo python3 stack --config site.json verify 3
```

Use stage 3's new hash. qBittorrent gets generated local credentials in
`install_root/secrets/qbit_username` and `install_root/secrets/qbit_password`.
Read those files privately on the guest for the first Web UI login; they are
not shared/default passwords and are deliberately absent from `access` output.
On a separate torrent VM, import the media applications' required API keys
with the same hidden `secret` command so their download clients can be linked.

The normal qBit Web UI accepts authorized magnets. Manual jobs use the separate
manual directory. Antivirus scanning and download approvals are not included
in this simple stage. A completed download is still untrusted data.

Complete the disposable VPN egress, interruption/recovery and actual transfer
checks in [ACCEPTANCE.md](ACCEPTANCE.md). Configuration verification does not
interrupt an active VPN or claim an unperformed kill-switch test.

## 6. Add the parts you will use

| Stage | Next action |
| --- | --- |
| 4, management | Plan/apply, open `access 4`, create Dockge/Kuma administrators and a Homarr administrator if enabled, add monitors and receive a real test alert. Run `attest 4`, then `verify 4`. |
| 5, photos | Enable photos, plan/apply, complete Immich onboarding, configure/verify, then test upload and an isolated originals-plus-database restore. |
| 6, music | Enable music, supply the private Soulseek inputs, plan/apply, enroll Navidrome and test a real track. Configure Music Assistant only if selected. Run `attest 6`, then `verify 6`. |

Management can run before the media stages and remains independent of NAS
availability. Dockge controls only its own host's Docker daemon. Install it
on another guest too if you want local logs and terminals there.

Manual attestation records work you actually tested in the application's UI.
It supplements the live checks; it is not a way to dismiss an API or runtime
failure. Never attest an action that has not happened.

## When a stage stops

```sh
sudo python3 stack --config site.json status 2
sudo systemctl status samurai-homelab-2.service
sudo journalctl -u samurai-homelab-2.service --since '20 minutes ago'
```

Replace `homelab` and `2` with the selected deployment and stage. Journals are
private diagnostic material; inspect and redact them before sharing.

| Result | Meaning |
| --- | --- |
| `started` | Processes started; account enrollment and application verification may still be pending. |
| `pending`, `failed` or `blocked` | Finish the reported prerequisite or diagnose the actual failure, then retry that command. |
| `verified` | The automated checks for that stage passed. |
| `verified_with_manual_checks` | Automated checks passed with the recorded operator enrollment checks. |

Exit code `0` means that operation completed, `1` means a safety/prerequisite
failure, `2` means pending/failed application configuration, and `130` means
interruption. Keep partial state. There is no automatic destructive rollback.

Use the named systemd unit for a complete stage's routine start/stop/restart.
Dockge is useful for logs and container terminals; do not simultaneously use
its deployment buttons and systemd as competing lifecycle controllers.
See [RECOVERY.md](RECOVERY.md) for stale locks, retries, backups and upgrades,
and [SECURITY.md](SECURITY.md) for the security boundary.
