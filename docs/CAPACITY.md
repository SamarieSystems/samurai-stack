# Free-space safeguards

Run `./setup --capacity`, or choose **c** in the setup menu. Guided media
setup offers the same review after application wiring, before indexer intake.
The initial suggestion is **50 GiB on media/download storage** and **20 GiB
on local application storage**. Both values are configurable whole GiB.
These are starting points, not hardware sizing recommendations.

Setup measures the actual backing filesystems and shows available capacity,
the selected reserves and the native settings before asking to apply them.
Already saved reserves are reused on retries. Declining keeps existing
settings. This action does not import or move media, change accounts or
recreate containers.

**Reserves are pause/import thresholds, not filesystem quotas.** Running
downloads, extraction, another application and filesystem metadata can still
consume space between checks. Keep enough extra capacity for the largest
expected concurrent jobs, including unpacking. If application and media
storage share one filesystem, either threshold can apply to that same space.

## Native application settings

The reviewed policy enables Sonarr/Radarr's supported free-space checks and
sets their import minimum to at least the selected data reserve. A stricter
existing minimum is preserved. Where the live schema exposes a grabbing
free-space switch, it remains enabled as well. Existing unrelated media
management settings are retained and the complete saved resource is checked
after a change.

When Usenet is enabled, SABnzbd receives minimum free-space floors for its
temporary and completed download paths. Its full-disk automatic-resume option
is disabled. The installer preserves a stricter existing floor. These native
checks protect the filesystems behind SAB's configured paths, not arbitrary
other disks or categories redirected elsewhere. The generated stack keeps
its normal temporary/completed paths on the shared data tree.

Starr checks are import/grab checks, not admission control for another
download client's entire queue. Native Starr/SAB settings alone do not
enforce the local application reserve.

## Optional qBittorrent pause watcher

After successful local VPN/torrent setup, the installer offers to enable the
watcher using the saved reserves. It is also available from **c** or
`./setup --capacity` once the owned local qBittorrent stack is running.

The watcher checks both backing filesystems approximately every 30 seconds.
Before controlling qBittorrent it proves the installation UUID, Compose
project, persistent appdata path, private WebUI binding, exact Gluetun network
namespace, `tun0` binding and provider-forwarded listening port when enabled.
It authenticates using a fresh private HTTP session with authentication
bypasses disabled. It stops only the exact incomplete downloading hashes
confirmed immediately before the operation. Seeders and manually stopped
torrents stay as they are. An unverified target fails closed.

The watcher never deletes files, changes VPN settings or resumes downloads.
After freeing space, inspect the queue and resume selected downloads yourself.
If the guard or API check fails, inspect it rather than assuming the pause
happened. This is a bounded capacity safeguard, not a replacement for storage
monitoring, backups or the VPN kill switch.

The private policy, latest diagnostic status and backups live under the
existing installation directory:

```text
INSTALL_ROOT/state/capacity-policy.json
INSTALL_ROOT/state/capacity-guard-status.json
INSTALL_ROOT/backups/capacity-REVIEW_HASH.json
```

The timer and service are named
`samurai-INSTALLATION_NAME-capacity-guard.timer` and
`samurai-INSTALLATION_NAME-capacity-guard.service`. Numeric application service
IDs are unchanged. For an installation named `homelab`:

```sh
sudo systemctl status samurai-homelab-capacity-guard.timer
sudo journalctl -u samurai-homelab-capacity-guard.service --since '1 hour ago'
```

To disable this watcher, rerun the capacity review and answer **no** to its
watcher question. Only the exact generated owned units are stopped/disabled;
native application floors remain, and downloads are not resumed. A changed
or foreign unit/source file is retained for deliberate review.

A capacity review can also upgrade the exact released v0.2.1 watcher after
proving its existing policy and both generated units belong to this installation.
The review binds the old and new source hashes and saves the old script privately
as `backups/capacity-watcher-before-REVIEW_HASH.py` before replacement. Local edits
and unknown watcher versions are retained. A legacy storage identity must first
complete the separately reviewed storage UUID upgrade.

## Advanced reviewed interface

The normal Bash entry points remain `./setup` and `./stack`. Python implements
the validated backend and the small standalone installed watcher.

```sh
./stack --config /path/to/site.json capacity plan \
  --data-reserve-gib 50 --app-reserve-gib 20 \
  --qbit-guard --out capacity-plan.json
```

Inspect the private plan and its `review_hash`, then apply the exact document:

```sh
./stack --config /path/to/site.json capacity apply \
  --plan capacity-plan.json --accept-plan REVIEW_HASH
./stack --config /path/to/site.json capacity verify
```

Omit `--qbit-guard` to keep the saved watcher choice, or pass
`--no-qbit-guard` to review disabling it. On a new policy the watcher defaults
to disabled. Reserve and watcher choices belong only to `plan`; `apply`
rejects parameter overrides. Changes to application settings or storage
identity invalidate an old review. Available bytes may change normally while
the existing services remain online.

Setup saves a private pre-change review and verifies native API readback.
If a later operation fails, some stricter floors can already be applied.
Review a fresh plan and retry. The installer does not delete resources or
relax floors automatically to roll back an interrupted operation.

Upstream behavior is described in [SABnzbd's folder settings](https://sabnzbd.org/wiki/configuration/5.1/folders)
and the live [Sonarr](https://github.com/Sonarr/Sonarr/blob/develop/src/Sonarr.Api.V3/Config/MediaManagementConfigResource.cs)
and [Radarr](https://github.com/Radarr/Radarr/blob/develop/src/Radarr.Api.V3/Config/MediaManagementConfigResource.cs)
media-management schemas. The installer verifies supported fields instead of
assuming every application version exposes identical settings.
