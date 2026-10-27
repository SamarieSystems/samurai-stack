# Where did my download go?

Check the application responsible for the current step. A request, a completed
transfer, an imported file and a playable Jellyfin item are four different things.

For everyday watching and requests, use [Daily use](DAILY-USE.md). This page is
for the owner inspecting the pipeline or deliberately importing existing media.

## Which app does what?

| Application | Its job | Default browser port |
| --- | --- | --- |
| Seerr | Accept movie/show requests and show library availability | `5055` |
| Sonarr | Select and organize TV/anime episodes | `8989` |
| Radarr | Select and organize films | `7878` |
| Prowlarr | Connect search sources to the media applications | `9696` |
| SABnzbd | Download, repair and unpack Usenet jobs, when enabled | `8080` |
| qBittorrent | Download torrents and continue seeding, when enabled | `8081` |
| Bazarr | Manage additional subtitles | `6767` |
| Jellyfin | Scan the library and serve it to your player | `8096` |

Use the actual URLs in your handover sheet. For example,
`http://APP_HOST:8989` means Sonarr at the configured server address, not a
public website named `APP_HOST`. Split-host deployments use different addresses
for different machine roles.

SABnzbd is optional on torrents-only installations. qBittorrent is optional on
Usenet-only installations. Photos, music, Home Assistant, MediaBot and local AI
are separate features, not prerequisites for this pipeline.

## Follow one title through the pipeline

Use the same exact movie or episode name at each step:

1. **Seerr:** does the request exist, and is approval still pending?
2. **Sonarr for TV, Radarr for movies:** does the title exist there? Open its
   detail page, then inspect **Activity** and **History** for the current queue
   item or latest action. A release being grabbed means it was sent to a
   downloader, not that its file is already in the library.
3. **Downloader:** find the job in SABnzbd or qBittorrent. Note its state,
   percentage, error, category and destination path.
4. **Sonarr/Radarr again:** when transfer/post-processing finishes, inspect the
   queue for an import warning and the title for an imported media file.
5. **Jellyfin:** look for that imported title and test playback. Seerr's displayed
   availability may update after its next library synchronization.

**Success is an imported file that actually plays.** A green container, an
approved request or a downloader at 100% proves only its own part.

## Requested, but nothing is downloading

Start in Sonarr/Radarr, not in qBittorrent:

- Confirm the correct title and desired season/episodes are monitored.
- Review the assigned quality profile and any minimum availability setting.
- Check that a source exists and is healthy in Prowlarr and is connected to
  the intended media application.
- Use an interactive search for one intended item and read why candidates
  are rejected before overriding a decision.

Monitoring does not mean the app constantly searches the entire historical
catalog for everything missing. Its normal feed handling and a deliberate
search have different roles. A release can also be unavailable or outside your
quality/language policy. See the official
[Sonarr quick start](https://wiki.servarr.com/sonarr/quick-start-guide) and
[Radarr FAQ](https://wiki.servarr.com/radarr/faq).

Current guided step 3 also installs a separate
[Sonarr monitoring-change companion](../MONITOR-SEARCH.md). It baselines the
existing missing library without searching it, then checks about every 30
seconds for observed off-to-on monitoring changes on missing, aired episodes.
Those changes get a bounded native episode search. Existing files, unaired
episodes and newly observed backlog are not bulk-searched. A rapid off/on toggle
between polls cannot be observed. Check Sonarr's command result before concluding
that a lack of downloads means the helper did not run. Radarr monitoring retains
its native behavior; this helper is specifically for Sonarr episodes.

The installer verifies supported source authentication and advertised coverage;
it cannot supply subscription accounts or guarantee a result. A Usenet NNTP
provider downloads the articles; an indexer finds the NZB. They are different
services with different credentials. Torrent sources likewise need appropriate
configuration. See [source setup](../INDEXERS.md).

## SABnzbd: queued, repairing, unpacking or failed

Open **Queue** for active work and **History** for the result of a job. A job can
finish receiving data and still need verification, repair or extraction. That
work uses CPU and disk, so a 0 B/s network reading does not by itself mean it is
stuck. Read the current job state and error. See
[SABnzbd's status definitions](https://sabnzbd.org/wiki/extra/queue-history-searching).

If a job failed, open its history details and record the actual reason. Missing
articles, provider login errors and a full disk are different failures. Repeated
retries will not supply articles the provider does not have. Select another
release only after checking the cause. SAB documents unavailable posts in
[Downloads cannot be completed](https://sabnzbd.org/wiki/introduction/downloads-cannot-be-completed.html).

Do not clear failed history or delete its working directory as your first fix.
That can discard useful diagnostics or reusable partial data.

## qBittorrent: adding a manual magnet or torrent

For a movie or series you want the media applications to manage, start with
Seerr or a deliberate Sonarr/Radarr search. That creates the associated title,
category and download-client history.

For a manual download you are authorized to obtain:

1. Open qBittorrent and sign in with its application credentials.
2. Use the Web UI's add-link or add-torrent-file action, depending on whether
   you have a magnet/URL or a `.torrent` file.
3. Review the destination and selected files before starting. Keep the managed
   default/manual destination unless you intentionally need another path.
4. Inspect that job's state and its content list after metadata arrives.

New uncategorized/manual jobs normally use `/data/downloads/torrents/manual`.
Sonarr and Radarr use their separate `tv` and `movies` categories. Giving an
arbitrary manual torrent one of those categories does not create the correct
movie/series record or guarantee an import. Review a manual import deliberately.

Samurai's simple torrent stage has **no ClamAV approval queue**. It does not
require a Discord command before a pasted magnet can run. Another server can
have additional policies; do not remove its controls to make it behave like
this catalog. Completion also does not certify downloaded software as safe.

### Read the torrent state before changing settings

| State or symptom | Likely meaning | Useful next check |
| --- | --- | --- |
| Metadata missing; size unknown or zero | The magnet has not supplied its file list yet | Check peers, trackers and the item's state; it is not proof of an empty download |
| Downloading | Data is arriving | Watch bytes/progress and remaining disk space |
| Stalled | This job is currently getting no useful data | Check peer availability and whether other jobs transfer |
| Stopped/paused | Someone or a policy stopped the job | Check its error and capacity state before resuming |
| Queued | It is waiting under the client's queue policy | Inspect queue/activity limits rather than deleting it |
| 100% and seeding | Payload is complete and can be uploaded | Check Sonarr/Radarr import if it belongs in the media library |
| Errored or missing files | A storage/path/access problem needs investigation | Record the full error and check the mounted storage |

Low upload is not automatically a bandwidth cap. It also depends on peers wanting
your data, their capacity and your connectivity. Compare a known healthy torrent
before changing global settings. The VPN interface binding and Gluetun namespace
are deliberate safeguards. Never switch qBittorrent to a physical NIC or disable
the VPN to test transfer speed. See [torrent architecture](../SERVICES.md#vpn-and-manual-magnets).

## The path map

There are two views of the same storage. The host sees the selected
`storage.data_root`; media applications see it mounted inside their containers
as `/data`.

For example, **if** the selected host data root is `/srv/samurai-data`:

| Purpose | Host path | Application path |
| --- | --- | --- |
| Film library | `/srv/samurai-data/library/movies` | `/data/library/movies` |
| TV/anime library | `/srv/samurai-data/library/tv` | `/data/library/tv` |
| Manual torrents | `/srv/samurai-data/downloads/torrents/manual` | `/data/downloads/torrents/manual` |
| Movie torrents | `/srv/samurai-data/downloads/torrents/movies` | `/data/downloads/torrents/movies` |
| TV torrents | `/srv/samurai-data/downloads/torrents/tv` | `/data/downloads/torrents/tv` |
| Incomplete torrents | `/srv/samurai-data/downloads/torrents/incomplete` | `/data/downloads/torrents/incomplete` |
| Completed Usenet jobs | `/srv/samurai-data/downloads/usenet/complete` | `/data/downloads/usenet/complete` |
| Working Usenet jobs | `/srv/samurai-data/downloads/usenet/incomplete` | `/data/downloads/usenet/incomplete` |

That host path is an example, not a requirement. Use your installation's actual
data root. Do not create a second tree just to copy the example. The installer
creates its dedicated tree; it does not automatically adopt or relocate your
old folders.

The libraries and download directories intentionally share one data tree. That
lets supported imports use hardlinks or same-filesystem moves. Separate Docker
mounts that happen to have similar names can defeat that benefit. Jellyfin sees
only the library subtree read-only; it does not serve the entire download tree.
See [storage](../STORAGE.md) and [service mounts](../SERVICES.md#one-consistent-data-tree).

## Grab a completed file onto your own computer

Select the job in the downloader and inspect its saved/content path. Translate
the `/data` prefix to the actual host data root above.

- If the owner already provides an SMB/NAS share for that location, browse it
  with the normal file manager and **copy** the completed file to your computer.
- Otherwise, an administrator can use an existing SFTP connection to the VM
  and download it from its host path. Use the VM's Linux account, not the
  qBittorrent application password.
- For an imported video, a Jellyfin download action may be available if the
  owner's user policy and your client permit it.

Samurai does not automatically create an SMB share or a general browser-based
file manager. qBittorrent's file list shows server files; it is not a download
link to your laptop. Copying out a seeding torrent preserves the server copy.
Moving or renaming its server files can break seeding.

For a graphical SFTP client, create a connection using **SFTP**, the VM address,
port **22**, and the Linux account/key the owner provided. Verify the server's
SSH fingerprint with the owner on the first connection. Browse the server side
to the actual host download directory, select the finished item, and choose a
download/copy action to a folder on your computer. If that account cannot read
the file, ask the owner to fix the intended access; do not recursively change
permissions across the media disk.

## Bring an existing library under management

First decide which of these you actually have:

| Existing content | Appropriate operation |
| --- | --- |
| Organized series/movie folders already at the intended library location | Library import to register the existing folders |
| A completed download that needs episode/movie matching | A reviewed manual import |
| Old folders elsewhere on another disk/share | A storage/adoption plan before importing |
| A mixed directory containing documents, photos and media | Inventory and separate planning, not bulk media import |

For an organized library, review every title match, year, quality profile and
monitoring choice before confirming. In Sonarr, use **Library Import** for
already organized series; **Wanted > Manual Import** handles loose completed
episodes. The official [Sonarr import guide](https://wiki.servarr.com/sonarr/quick-start-guide)
distinguishes these operations. Use Radarr's corresponding library/manual
import flow for movies and inspect the proposed movie match and destination.

Do not use a download directory as the library root. Do not approve a file-move
or rename operation if the goal is only to register files in place. An import
preview can propose real filesystem changes; read its source, target and
copy/move choices before confirming. If the old location is not mounted into
the container, make a deliberate mount/adoption plan rather than inventing a
remote-path mapping. The generic installer does not automate media migration.

Start with one film or one small series. Confirm the expected location, library
match, playback and seeding behavior before processing the rest. Enabling the
[future-import naming policy](../MEDIA-NAMING.md) does not retroactively rename
or migrate the existing library.

## Hardlinks, seeding and freeing space

A hardlink gives one file's data another filesystem name. For a torrent import,
the download path can keep seeding while the library uses an organized name.
This needs compatible storage, permissions and the hardlink setting. If it is
not possible, Starr can copy the file instead, using additional space. See
[Sonarr's torrent import behavior](https://wiki.servarr.com/sonarr/faq) and
[Radarr's settings](https://wiki.servarr.com/radarr/settings).

Two pathnames do not necessarily mean two physical copies. With hardlinks:

- Removing one pathname leaves the data available through the other.
- Space is released only after the final hardlink is removed and no process
  holds the deleted file open. Filesystem snapshots can also retain blocks.
- Editing the data in place affects every hardlink to that same file.

When deleting intentionally, decide separately whether to stop monitoring,
remove the library file, remove a torrent job and delete its download data.
These actions have different effects. Read the confirmation: **remove torrent**
and **remove torrent plus files** are not interchangeable. An unmonitored or
removed request does not itself prove its stored media was deleted.

Do not clear download folders, appdata or Docker volumes as generic cleanup.
Check the exact items and backups first. An incomplete archive may also need
temporary space alongside its unpacked media.

If capacity protection stopped downloads, free space deliberately, inspect the
current queue and resume only the jobs you want. The optional qBittorrent
capacity watcher never resumes jobs for you. Its reserves are thresholds, not
hard disk quotas; see [capacity safeguards](../CAPACITY.md).

## Changing quality, language or subtitles

Use a small deliberate change, then test one title. The optional `1080p Original`
policy allows selected 720p/1080p SDR releases, retains original audio and avoids
automatic quality upgrades. It is not an English-dub policy or a guarantee
against mislabeled releases. Existing titles are not automatically reassigned
to a new profile. Global quality-size definitions affect every profile that
uses those qualities. See [media profiles](../MEDIA-PROFILES.md).

For a missing subtitle, check Jellyfin's existing tracks first, then Bazarr's
title, language profile, provider and error. Provider enrollment is not proof of
delivery. The optional policy preserves embedded tracks and does not run old
library backfill. See [subtitles](../SUBTITLES.md).

For an unexplained failure, keep the exact item name, error, time, downloader
category and path. Continue with [Troubleshooting](TROUBLESHOOTING.md) rather
than restarting every application.
