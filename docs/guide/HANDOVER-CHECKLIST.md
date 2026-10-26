# Your server handover sheet

Fill this in with the person installing the server. Keep the completed sheet
private: service addresses, account names and storage details describe your
network. Store passwords and API keys in your password manager, not this sheet.

**Blank does not mean passed.** Mark each acceptance item as passed, pending,
not enabled, or failed, with the date and the person who checked it.

## The short version

| Detail | Your installation |
| --- | --- |
| Server name and owner contact | Fill in |
| Installed release and date | Fill in |
| Physical host / hypervisor | Fill in |
| Application VM name and ID | Fill in |
| Reserved LAN address / hostname | Fill in |
| Private remote method and address | Fill in; or not configured |
| Everyday viewer account name | Fill in; password-manager entry separately |
| Application administrator account name | Fill in; password-manager entry separately |
| Dockge owner and Kuma owner account names | Fill in; these may differ from the media administrator |

## The links people actually need

Record working URLs, including `http://` or `https://` and any port. Test each
from the device/location it is meant to serve. A remote URL should say whether
Tailscale must be connected.

| Application | At-home URL | Private remote URL | Login tested? |
| --- | --- | --- | --- |
| Jellyfin | Fill in | Fill in or not configured | Pending |
| Seerr | Fill in | Fill in or not configured | Pending |
| Sonarr | Fill in | Fill in or not configured | Pending |
| Radarr | Fill in | Fill in or not configured | Pending |
| Prowlarr | Fill in | Fill in or not configured | Pending |
| Bazarr | Fill in | Fill in or not configured | Pending |
| SABnzbd | Fill in or not enabled | Fill in or not configured | Pending |
| qBittorrent | Fill in or not enabled | Fill in or not configured | Pending |
| Dockge | Fill in | Fill in or not configured | Pending |
| Uptime Kuma | Fill in | Fill in or not configured | Pending |

## Where things live

| Item | Actual saved path / name |
| --- | --- |
| Local installation root | Fill in |
| Appdata directory | Fill in |
| Media disk/NAS filesystem and stable identity | Fill in; no private access keys |
| Media data root on Linux | Fill in |
| Container movie / TV roots | Normally `/data/library/movies` and `/data/library/tv`; confirm |
| Manual torrent folder on Linux | Fill in |
| Compose project names | Fill in |
| Generated systemd unit names | Fill in; do not infer from the Dockge display name |
| Saved installer configuration path | Fill in |
| Backup destination and most recent date | Fill in |
| Person responsible for backups | Fill in |

Do not use a screenshot of a different server as a directory plan. The chosen
host path can differ while the container `/data` layout stays consistent.

## Acceptance before calling it ready

| Check | Result / date / person |
| --- | --- |
| The intended media filesystem is mounted; its identity and free space match | Pending |
| Existing important files are preserved and independently backed up | Pending |
| Everyday Jellyfin login works and does not have administrator permissions | Pending |
| Seerr uses the intended Jellyfin login and permitted request behavior | Pending |
| One permitted test request reaches the intended Sonarr/Radarr manager | Pending |
| Each enabled download method completes one controlled legal test job | Pending |
| The resulting file is on media storage and imports into the correct library | Pending |
| A real torrent import uses hardlinks where supported and intended | Pending / not applicable |
| A real title plays on the actual TV/Kodi/phone used by the owner | Pending |
| Audio language and subtitle selection work on that device | Pending |
| Required external subtitles are actually delivered, not merely configured | Pending / not enabled |
| Torrent namespace, tunnel binding and provider forwarding checks pass | Pending / not enabled |
| A disposable tunnel-loss test proved no ordinary-WAN torrent fallback | Pending / not enabled |
| Guest reboot returns the enabled apps without manual starts | Pending |
| Hypervisor automatic guest startup is enabled; host restart scope is recorded | Pending |
| Missing media storage blocks data-dependent starts instead of writing locally | Pending |
| A deliberate whole-stage stop stays stopped | Pending |
| Disk-space floors/watchers are verified and their limits understood | Pending |
| Dockge/Kuma owner logins work; projects and monitors match this server | Pending |
| A real monitoring notification reached the chosen destination | Pending |
| An application-consistent backup exists outside this VM | Pending |
| Restore has been tested separately without overwriting the working server | Pending |
| Remote access works privately with only the intended people/devices allowed | Pending / not configured |

Run disruptive tests in a disposable environment or a planned maintenance
window with the installer. This sheet records proof, not permission to interrupt
someone else's live storage or VPN.

## Decisions still owned by a person

| Decision | Chosen answer / person responsible |
| --- | --- |
| Allowed viewers, request approvals and quotas | Fill in |
| Torrent / Usenet preference and provider ownership | Fill in |
| Movie/series quality and original-audio policy | Fill in |
| Subtitle languages and provider account | Fill in |
| Free-space floors and manual cleanup decisions | Fill in |
| Backup schedule, retention and offsite copy | Fill in |
| Who receives alerts and who responds | Fill in |
| Whether optional photos, music or GPU acceleration are enabled | Fill in |
| When to review application image updates | Fill in |

## When asking for help

Record the release, exact app, time, error text, whether LAN/remote access
differs, and the one test already attempted. Do not include passwords, API
keys, provider configuration, full databases or unredacted private filenames.
Use [Troubleshooting](TROUBLESHOOTING.md) for the next useful check.

[Guide home](README.md) | [First-machine technical acceptance](../ACCEPTANCE.md)
