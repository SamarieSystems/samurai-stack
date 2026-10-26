# Start here: your server in ten minutes

You do not need to learn Docker to watch a film. Start with Jellyfin and Seerr.
The other applications keep the library supplied and the server running.

This handbook describes Samurai Stack 0.2.3. Your owner should give you the
actual service links and your own account. The printable
[handover sheet](HANDOVER-CHECKLIST.md) records those details without recording
passwords. Features not enabled on your installation will not appear.

## I just want to watch something

1. Open the **Jellyfin** link supplied by the owner. On a TV or phone, use a
   [Jellyfin client](https://jellyfin.org/clients/) and enter that same server
   address when asked. A server address is a URL, not your email address.
2. Sign in with your everyday viewer account. This is a local account on this
   server, not a paid Jellyfin subscription.
3. Open a movie or series already in the library and play it.
4. Use the player's audio and subtitle controls to choose available tracks.
   A title having subtitles on another service does not mean this copy has them.
5. Bookmark the server link. You can use the browser too; a desktop application
   is optional.

**Finished means:** you can hear and see the title on the device you intend to
use. A working home page alone is not a playback test.

For Kodi, the owner should finish the supported Jellyfin/Kodi connection and
test it with your existing library before replacing a working setup. Use the
[upstream Kodi instructions](https://jellyfin.org/docs/general/clients/kodi/)
for that client. Do not install a random plugin ZIP to fix a server timeout.

## I want something that is not there

1. Open **Seerr**, the request app.
2. Choose **Sign in with Jellyfin**, then use your everyday Jellyfin login.
3. Search for the exact title and year. Check whether it is already available
   before requesting another version. For TV, check the season selection.
4. Submit a request if your account permits it. The server may require the
   owner's approval or apply request limits.
5. Check the request status in Seerr. Once it is available, watch it in Jellyfin.

A request asks the library managers to find a suitable release. It does not
guarantee immediate availability, a particular language track, or a download
finishing at a specific time. See [daily use](DAILY-USE.md) for the full flow.

## Which application should I open?

| I want to... | Open | Who normally needs it |
| --- | --- | --- |
| Watch movies and TV | Jellyfin | Everyone with a viewer account |
| Request movies or TV, check my requests | Seerr | Viewers with request access |
| Manage series, seasons and anime imports | Sonarr | Owner/operator |
| Manage movie downloads and imports | Radarr | Owner/operator |
| Check Usenet download, repair and extraction | SABnzbd, if enabled | Owner/operator |
| Add a manual magnet or inspect torrent peers | qBittorrent, if enabled | Owner/operator or specifically authorized users |
| Manage search sources | Prowlarr | Owner/operator |
| Find missing subtitle files | Bazarr | Owner/operator |
| Inspect container logs and Compose projects | Dockge | Trusted administrators |
| Check service monitors and alerts | Uptime Kuma | Owner/operator |
| Reach private links from another location | Tailscale, if configured | Authorized remote users |

SABnzbd and qBittorrent are alternative download methods. An installation can
use either or both. Photos and music are optional additions; Home Assistant,
MediaBot and a local AI model are separate projects, not hidden requirements.

## What happens after I request a title?

```text
Seerr request
  -> Sonarr (TV) or Radarr (movies)
  -> search sources connected through Prowlarr
  -> SABnzbd (Usenet) or qBittorrent (torrents)
  -> completed file imported into the library
  -> Jellyfin discovers it and your player can open it
```

There are several distinct waiting points. **Requested**, **downloading**,
**download completed**, **imported**, and **playable** do not mean the same
thing. If a title is stuck, use [the download guide](MEDIA-AND-DOWNLOADS.md)
instead of requesting it repeatedly.

## Use the right account

| Account | What it controls |
| --- | --- |
| Everyday Jellyfin/Seerr viewer | Watching, personal watch state and allowed requests |
| Media application administrator | Library, source, download and user settings |
| Dockge/Kuma owner | Container administration or monitoring settings |
| Debian/SSH administrator | The Linux VM itself |
| Proxmox administrator | The hypervisor, VMs and attached storage |
| VPN/Usenet/indexer/subtitle provider | A separate external service |

These are different accounts even when someone originally reused a username.
Do not assume the Linux root password works in every app. The installer can
enroll supported fresh accounts; it does not overwrite an existing owner to
make a forgotten password disappear. Keep passwords in a password manager,
not in the handover sheet or a shared screenshot.

## At home versus away

The owner supplies the correct link for your location. A LAN link works on the
home network. A private remote link may require Tailscale to be connected and
your account/device to be allowed. Merely seeing the server in Tailscale does
not mean every application port has been forwarded.

If a remote link times out, first check Tailscale and ask the owner whether
that exact link is configured. Do not add router port forwarding, turn off the
firewall, or change a torrent's VPN interface to fix a browser connection.
See [remote access](../REMOTE-ACCESS.md) for the owner's setup options.

## I own the server. What should I learn next?

Read these in order, stopping when you have what you need:

1. [Daily use](DAILY-USE.md): accounts, requests, watching and reporting issues.
2. [Media and downloads](MEDIA-AND-DOWNLOADS.md): where files go, imports,
   languages, subtitles and manual jobs.
3. [Operating the server](OPERATING-THE-SERVER.md): Dockge, logs, disk space
   and controlled restarts.
4. [Troubleshooting](TROUBLESHOOTING.md): match a symptom to the next useful test.
5. [Backups and updates](BACKUPS-AND-UPDATES.md): keep a recoverable installation.
6. [Handover checklist](HANDOVER-CHECKLIST.md): prove the important paths work.

If this machine is not installed yet, use the
[new-server walkthrough](INSTALL-A-NEW-SERVER.md), then the detailed
[guided setup](../QUICKSTART.md) when needed. Run application setup **inside the Linux
guest**, not in the Proxmox host shell. Physical installation, disk selection,
NAS exports and optional GPU passthrough remain deliberate owner tasks.

## A few things worth knowing before clicking around

- Your viewer account is enough for normal watching and requests.
- Changing a **container path**, **media root**, **mount**, or **VPN interface**
  changes how the system works. Record its current value before investigating.
- A green container is one check. A real request, download, import and playback
  test is the proof that the whole workflow works.
- Downloaded files are not automatically safe to execute. This simple stack
  does not include a ClamAV approval queue.
- If the storage guard refuses startup, it is protecting against writing to
  the wrong disk. Fix the actual mount or identity; keep the guard.

[Guide home](README.md) | [Glossary](GLOSSARY.md)
