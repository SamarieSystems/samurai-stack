# Security and data boundaries

Samurai Stack is an administrator-operated installer for a private home server.
It creates a new, owned installation around storage and Linux guests you
deliberately selected. It is not a security boundary against an administrator
who can edit its files, control Docker or change the underlying machine.

The alpha's measured validation and remaining commissioning work are recorded
in [VALIDATION.md](VALIDATION.md) and [ACCEPTANCE.md](ACCEPTANCE.md). A green
container status is not proof of a safe download, a working backup or a VPN
kill-switch test.

## Preserve existing data

- The toolkit does not format physical disks, change RAID, repartition existing
  storage, delete VMs or migrate an existing media collection. Proxmox provisioning
  imports an OS image into a new owned virtual disk and can expand that new disk
  to its reviewed size.
- Application storage and media storage must be separate dedicated paths.
  Existing unowned application directories are refused. A populated media
  directory requires an explicit reviewed opt-in; preparation remains additive.
- Existing ownership and permissions are not repaired recursively. Storage
  preparation uses the configured media UID/GID and tests actual access and
  download-to-library hardlinks.
- Data-dependent stages require the exact mount target/source/filesystem type
  and deployment marker. Startup checks run again through systemd. Missing NAS
  storage must not become a local fallback tree.
- Jellyfin and Navidrome libraries, plus Immich's external-originals tree, are
  read-only mounts. Immich uploads use a different dedicated tree.

The applications still perform the workflows you enable. Sonarr/Radarr imports
may rename or move downloads; Immich manages its upload tree. A read-only mount
protects only that mount. It does not make other credentials, administrator
interfaces or explicitly writable directories harmless.

Keep important original documents and photos outside a first rollout. Maintain
independent backups, including application databases where required, and test
restoration elsewhere. A successful installer run is not a backup.

## Administrative interfaces stay private

Application ports bind to the configured LAN address. The installer does not
create public DNS, router port forwarding or a public reverse proxy. LAN binding
does not replace your host/network policy, especially on an untrusted shared
network or a host with pre-existing forwarding rules.

Complete first administrator enrollment from a trusted client before giving
other people access. Use distinct strong credentials. For remote administration,
prefer an authenticated private overlay or VPN with the intended access controls.
Publishing a dashboard or service to the internet is a separate deployment
decision that requires its own authentication, TLS and exposure review.

The disposable test reproduced Jellyfin's known upstream behavior: a direct
video-stream URL can return media without an authenticated session when the
item ID is known. Account login alone does not protect every playback URL.
Keep Jellyfin behind the private LAN/VPN access boundary too. See upstream
[issue 13984](https://github.com/jellyfin/jellyfin/issues/13984) and the proposed
[conditional authentication change](https://github.com/jellyfin/jellyfin/pull/16664).
The alpha does not claim that an unmerged upstream change fixes its deployed image.

| Component | Administrative power |
| --- | --- |
| Dockge | Docker socket access is effectively host administration. Its authenticated users can control containers and their mounts. |
| Homarr socket proxy | Write operations are disabled, but Docker configuration read access can still reveal sensitive settings. Keep the dashboard private. |
| Starr/Seerr/Jellyfin API keys | These authorize application operations. Treat them as credentials, not harmless configuration strings. |
| Music Assistant with host networking | Multicast/player discovery uses the host network. Its reachability depends on host firewall and LAN policy, not the other containers' IP bindings. |

The generated systemd unit owns guarded stage startup and shutdown. Direct
Docker/Dockge deployment actions can bypass that controller. Use `apply` for
toolkit changes and the named systemd unit for complete-stage maintenance;
consult [RECOVERY.md](RECOVERY.md) before mixing controllers.

## Credential handling

The `secret` command uses hidden local input or a private file import. If the
terminal cannot hide input, it refuses the prompt instead of echoing the value.
Do not put actual values into shell arguments, site JSON, examples, public issues
or pasted logs. Generated credentials are unique local files, not shared defaults.

Credentials live beneath `install_root/secrets`, with application-specific
private environment files where an image has no file-secret interface. Some
explicitly shared container secrets require restricted group readability; their
containing host directory remains private. Keep these files, site configuration,
receipts and application-consistent backups protected.

Verified site configuration is also retained in the root-private host-bound
registry under `/var/lib/samurai-stack/installations`; guided progress and
diagnostics live under `/var/lib/samurai-stack/setup`. Site JSON excludes
credentials, but private diagnostics and application files can contain them.
Do not publish these directories with a source archive. Deleting the checkout
does not erase this installation state, and deleting appdata cannot be repaired
from the registry alone.

HTTP provisioning requests do not follow redirects or use ambient proxy
variables. This prevents an API key from being forwarded to a redirected origin
or an unrelated environment proxy. Configure each provider's final reachable
address instead of relying on login-page redirects.

Installer reports omit secret values, and external error bodies are not echoed.
Vendor application logs may still contain sensitive details. Read them locally
and redact them before posting an issue. Never upload entire databases or secret
directories as a troubleshooting attachment.

## VPN torrents

qBittorrent shares Gluetun's network namespace and binds torrent traffic to
`tun0`. It has no separate Docker network or independently published host port.
Its Web UI authenticates, and automatic program execution hooks are disabled.
The supported forwarded-port watcher uses authenticated local calls, verifies
readback and holds qBit on loopback when required VPN/port information is lost.

These configuration controls need commissioning on the actual provider and host.
Compare normal host/VPN egress, interrupt only the disposable test tunnel,
verify failed egress while it is down, then verify recovery and an authorized
payload transfer. Do not infer that the negative test passed merely because
`tun0` exists. Routine verification does not interrupt an active tunnel.

The simple torrent stage does not scan or approve downloads. It does not modify
another installation's ClamAV or approval safeguards. A download completing,
passing a hash check, or receiving an antivirus result does not authorize
executing arbitrary software or establish that it is harmless.

## Updates, recovery and release hygiene

The release catalog pins immutable image indexes; local receipts also lock the
references used by each stage. Retries retain those locks. This alpha does not
automatically upgrade images, migrate databases,
downgrade applications, clear stale locks or delete partial installations.
Preserve the matching configuration/image locks and working backups before an
explicit update. Reverting Compose alone does not reverse a database migration.

Before publication, run the release hygiene check and inspect the candidate
contents. The scanner checks recognizable private keys/tokens, credential
literals, private configuration filenames and unscanned file types. An optional
private `--deny-file` can add operator-specific terms without shipping those
terms in the repository. Pattern matching cannot prove that an unknown secret
never existed. [MEDIABOT-AUDIT.md](MEDIABOT-AUDIT.md) describes its scope.

An exposed credential must be revoked or rotated at its issuer. Deleting its
latest visible line, rewriting a branch or replacing a release archive does not
make a previously public credential private again.

For a suspected vulnerability or credential exposure, use the repository's
private security-reporting channel when available. If it is unavailable, open a
minimal issue requesting a private contact without posting credentials, private
addresses, personal filenames or exploitable details. Ordinary reproducible
non-sensitive bugs can use public issues with the release tag, platform and a
redacted failure description.
