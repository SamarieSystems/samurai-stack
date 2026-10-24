# Services, storage and first-run enrollment

Samurai Stack provisions one stage at a time. `apply` starts the stage, `configure`
connects its applications, and `verify` checks the saved configuration. A running
container is not proof that users can request or play anything. Missing accounts,
credentials and incompatible API schemas remain visible as pending or failed
checks. Fix those prerequisites and run the same command again.

The first supported host is a dedicated Linux VM with Docker Compose. Other
machine roles can share that VM or use separate VMs. The `hosts` map is the
source for cross-host application addresses; it never assumes another VM's
Docker DNS names work locally. The installer does not expose a WAN port,
configure router forwarding, or create a public reverse proxy.

## Stage boundaries

| Stage | Services | Automatic configuration | Operator input |
| --- | --- | --- | --- |
| 2 | Sonarr, Radarr, Prowlarr, Bazarr, Jellyfin, Seerr; SABnzbd when Usenet is enabled | Library roots; Starr download clients; Prowlarr application links; Bazarr connections; Jellyfin movie/TV libraries; Seerr Starr settings after first-run enrollment | App administrators, NNTP/indexer/subtitle provider accounts, quality/language choices, Jellyfin/Seerr onboarding |
| 3 | Gluetun, qBittorrent; authenticated port watcher when forwarding is enabled | Namespace boundary; initial authenticated qBit configuration; `tun0`; paths/categories; uncapped bandwidth; Starr clients and release protocol preference | VPN service credentials/configuration; copied Starr API keys on separate hosts; VPN commissioning proofs |
| 4 | Dockge, Uptime Kuma; optional Homarr with a restricted socket proxy | Persistent local state and LAN bindings | First administrators, monitor URLs, actual notification destination and delivery test |
| 5 | Immich server, CPU machine learning, Valkey, matched PostgreSQL image | Private DB credential, local DB storage, dedicated upload tree, external originals mounted read-only | Administrator onboarding, independent backup, controlled upload/playback/restore tests |
| 6 | Navidrome and slskd; optional Music Assistant | Local application state, read-only music library, separate downloads, private credentials | Administrator enrollment, Soulseek account, music import policy, player/provider enrollment |

MediaBot, local LLMs, public ingress and Home Assistant are not prerequisites.
SoulSync is deliberately excluded from this first catalog. Its upstream config
and Python-package layout changed during earlier deployments; mounting an old
configuration directory over its package path is not a reliable installer.

## One consistent data tree

The selected `storage.data_root` contains:

```text
library/movies
library/tv
library/music
downloads/usenet/complete
downloads/usenet/incomplete
downloads/torrents/movies
downloads/torrents/tv
downloads/torrents/manual
downloads/torrents/incomplete
downloads/music/complete
downloads/music/incomplete
photos/upload
photos/external
```

Starr, SABnzbd and qBittorrent see the same backing data root at `/data`.
Movie/TV roots are `/data/library/movies` and `/data/library/tv`; download clients
report `/data/downloads/...`. A matching path name on two machines is not enough:
their storage configuration must identify the same actual shared filesystem.
Hardlinks still depend on filesystem/export semantics and permissions. Do not
solve cross-filesystem imports by blindly adding remote-path mappings.

Jellyfin receives only `library` at `/data/library`, read-only. Navidrome receives
only the music library, read-only. Immich uploads go into its dedicated managed
upload tree; the separate `/external` mount is read-only. Existing family photos
are never automatically moved into the upload tree. Adding an Immich external
library in its UI is a separate, deliberate choice.

Application configs, caches and databases live beneath
`install_root/appdata/<service>` on local Linux storage. Immich's PostgreSQL
database is never placed on NFS. All bind mounts use `create_host_path: false`:
Docker cannot silently substitute a freshly created local directory when a
required source is missing. Runtime mount identity checks are still necessary
because a stale empty mountpoint can already exist.

## Media enrollment and wiring

After applying stage 2, list its addresses:

```sh
sudo ./stack -c site.json access 2
```

1. Complete Sonarr/Radarr/Prowlarr initial administrator authentication in their
   LAN UIs. Choose sensible quality profiles and connect the indexers you actually
   use in Prowlarr. The toolkit does not guess paid provider credentials or add
   public torrent sources.
2. If using Usenet, open SABnzbd and enter the provider server, account, TLS port
   and connection count. Run its provider connection test. The toolkit wires
   SABnzbd paths and movie/TV categories; it does not purchase or enroll an NNTP
   account. Directory names alone do not prove a working NNTP connection.
3. Complete Jellyfin's administrator wizard. Do not add writable media mounts.
   Create an API key in its administrator dashboard and import it privately:

   ```sh
   sudo ./stack -c site.json secret jellyfin_api_key
   ```

4. Complete Seerr's initial Jellyfin sign-in against the configured Jellyfin
   address. Import its API key with `secret seerr_api_key`. This is a vendor
   account-enrollment step, not an installer-generated shared login.
5. Run `configure 2`, then `verify 2`. Generated Starr keys are read locally where
   supported. On a different VM, use the private `secret` command to supply the
   relevant application keys. API responses and reports never print their values.
6. In Bazarr, select subtitle languages and providers. The connection wiring
   does not invent a language preference or imply that a provider login works.

Configure uses the application-reported provider schemas and tests proposed
connections before saving them. Names begin with `Samurai <deployment-name>`.
Re-running it verifies those saved resources. A conflicting named resource is
reported instead of overwritten; unrelated resources are not silently adopted.
Seerr's profile selection uses an existing `HD-1080p`, `HD - 1080p`, or `Any`
profile, or the sole available profile. If none is unambiguous, select a profile
in Seerr deliberately. A default Seerr server, its selected root and its profile
must all pass readback before verification succeeds.

Normal Starr imports can rename or move downloads as part of the workflow you
enable in those applications. This is different from installer migration. The
toolkit disables automatic download-client removal in its managed Starr client
entries so commissioning does not silently become a cleanup policy.

`downloads.prefer` can be `usenet` or `torrents`. The implementation sets both
download-client priorities and the default delay profile's `preferredProtocol`.
Client priority by itself does not decide between Usenet and torrent releases.
Custom/multiple delay policies are left for the operator to review. A protocol
preference is not a promise to ignore quality, availability or indexer ranking.

## VPN and manual magnets

Only Gluetun publishes the qBit UI, on the configured torrent host's LAN address
and port 8081. qBittorrent shares Gluetun's exact network namespace and has no
independent Docker network or published host port. Its config is seeded before
first startup with a salted password hash, authentication enabled and `tun0`
binding. The generated username/password remain in private local files.

The simple torrent stage has no ClamAV service, download approval queue or
automatic execution hooks. Paste an authorized magnet into qBit's normal Web UI.
Uncategorized/manual jobs stay under `/data/downloads/torrents/manual`; Starr
jobs use the separate movie/TV categories. Downloaded software is not declared
safe merely because it completed. Nothing changes the existing security controls
on another installation.

| VPN choice | Private inputs | Forwarding behavior |
| --- | --- | --- |
| Proton WireGuard | `vpn_wireguard_private_key` | Optional supported forwarding; watcher follows the current granted port |
| Mullvad WireGuard | `vpn_wireguard_private_key`, provider-issued public `vpn.wireguard_addresses` | No forwarding; outbound torrent connections still work |
| OpenVPN provider | `vpn_openvpn_username`, `vpn_openvpn_password` | Only enable features actually supported by that provider/profile |
| Custom WireGuard/OpenVPN | `vpn_custom.conf`; OpenVPN username/password if required | This release does not automate arbitrary providers' forwarding schemes |

Import full custom configuration without putting it on a command line:

```sh
sudo ./stack -c site.json secret vpn_custom.conf --from-file /private/path/client.conf
```

Gluetun reads supported credentials from Docker secret files. File contents never
appear in a public plan. For custom VPN files, review routes, endpoints, DNS and
provider prerequisites before applying the stage.

When forwarding is enabled, a restricted authenticated watcher reads Gluetun's
port file, checks namespace-local VPN health, sets qBit's listen port and reads
the settings back. If health/port information disappears it binds qBit to
loopback until a healthy tunnel and valid port return. The watcher cannot select
a physical NIC, change torrent data or disable authentication. A verified port
readback is not a measured end-to-end inbound peer connection.

Commissioning needs a positive VPN egress check and a controlled negative
kill-switch test while no other workloads depend on that VPN. Do not infer a
passing kill switch from `tun0` being present. The standard application verifier
does not interrupt an active VPN. Save the commissioning evidence and use a
harmless legal torrent for actual download/upload verification.

## Management and notifications

Dockge is a privileged management interface because it can use the Docker socket
and change its managed Compose directories. Keep it on a trusted LAN or an
authenticated private overlay. Its socket access is not made safe merely by
calling the stack a dashboard. Homarr receives a separate proxy with write
operations disabled and no public proxy port.
Read-only Docker API access can still reveal container configuration, so Homarr
is an administrator interface, not a public status page.

Stage 4 can run on any machine role. Its interfaces bind to that machine's
`bind_address`, and Dockge controls only that machine's local Docker daemon.
Installing it on a central management VM does not grant control of containers on
other VMs. Run stage 4 on each host where local container logs and terminals are
wanted. Kuma can monitor reachable endpoints on other hosts; remote Docker
daemon exposure is not configured.

Each Compose directory uses the exact project name, for example
`install_root/stages/homelab-2/compose.yaml` with `name: homelab-2`. Dockge merges
its directory inventory with Docker's project names; differing names create
duplicate UI entries and incorrect managed/status indicators. Its logs and
container terminals use Compose in that directory, so they select the same
project. Private per-service environment files stay inside that project
directory where Dockge's Compose client can read them. File-based secret sources
and absolute bind sources are mounted by the host Docker daemon; Dockge does not
need a separate mount of the full secrets directory.

The toolkit's systemd units own guarded startup and shutdown. Use `apply` for
toolkit changes and `systemctl` on `samurai-<deployment_name>-<stage>.service`
for routine stage stop/restart operations. Dockge's detached deployment and
lifecycle buttons can conflict with the foreground systemd supervisor. Logs and
container terminals remain available for inspection and troubleshooting. Dockge
itself is an administrative process with Docker socket access; it does not use
the media UID/GID as a security boundary.

Create the first Dockge and Kuma administrators yourself, then configure the
monitors and notification type you want. Kuma supports multiple destinations;
Discord/MediaBot is not required. Send a test notification and trigger one
controlled monitor failure. The installer cannot confirm an email or chat
delivery just because a webhook field was saved. The explicit manual enrollment
checklist supplements live frontend checks where no stable public enrollment API
is available.

## Photos and music

The catalog uses matching Immich server/ML version `v3.3.5` and the database
image from that release's official Compose recipe. All release images are
pinned to immutable indexes; see [IMAGES.md](IMAGES.md). Upgrades require a reviewed new lock and
working database/originals backups, not a blind pull of an incompatible database
major version. The application verifier checks ping, initialized/onboarded flags
and maintenance state. It does not claim a successful backup or restore.

Immich server and ML run with configured media UID/GID. Their upload/cache
directories must permit that identity. The PostgreSQL entrypoint manages its own
local data permissions. The DB credential is available only to the server and
database through their mounted private secret, with explicit group readability
for the server identity inside a private host directory.

CPU-only is the baseline. Jellyfin can opt into Intel/AMD `/dev/dri` or NVIDIA
device reservations after host drivers and container access are verified.
Supply actual render/video group IDs for Intel/AMD rather than assuming they
match another machine. Selecting passthrough in Proxmox does not install guest
drivers or automatically enable a working codec in Jellyfin. Test a real
transcode. Immich ML stays on CPU in this catalog even when Jellyfin uses a GPU.

Navidrome first-run administrator enrollment remains in its UI. slskd's web
credentials/API key are generated privately, but the Soulseek account must be
yours. Its download-complete folder does not automatically become a trusted
music library: review/import music deliberately. The verifier checks slskd's
authenticated `/api/v0/server` connected and logged-in flags. It never downloads
a sample from another person as an installation test.

Optional Music Assistant needs separate administrator, music-provider and player
enrollment. Host networking supports local multicast discovery, so its interface
does not follow the other containers' explicit IP port bindings. Put that host
on a trusted player LAN and enforce the desired host firewall policy. Do not
enable this option on a public host. It is not necessary for Navidrome playback.

## Upstream references

- [Jellyfin containers](https://jellyfin.org/docs/general/installation/container/)
  and [hardware acceleration](https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/).
- [Seerr Docker installation](https://docs.seerr.dev/getting-started/docker/)
  and [Seerr settings interfaces](https://github.com/seerr-team/seerr/blob/v3.5.1/server/lib/settings/index.ts).
- [Prowlarr API](https://prowlarr.com/docs/api/) and
  [SABnzbd API](https://sabnzbd.org/wiki/advanced/api).
- [Immich v3.3.5 Compose](https://github.com/immich-app/immich/blob/v3.3.5/docker/docker-compose.yml),
  [requirements](https://docs.immich.app/install/requirements/), and
  [file-based database secrets](https://docs.immich.app/install/environment-variables/#secrets).
- [Gluetun secret-file inputs](https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/docker-secrets.md).
- [Dockge project discovery and Compose commands](https://github.com/louislam/dockge/blob/1.5.0/backend/stack.ts)
  and [Docker Compose file-secret bind implementation](https://github.com/docker/compose/blob/v2.34.0/pkg/compose/create.go).
- [slskd configuration](https://github.com/slskd/slskd/blob/master/docs/config.md),
  [authenticated server state](https://github.com/slskd/slskd/blob/master/src/slskd/Core/API/Controllers/ServerController.cs),
  and [Music Assistant installation](https://www.music-assistant.io/installation/).

These links explain upstream contracts. They do not turn local unit tests into
proof that a fresh deployment was commissioned on every supported machine.
