# Watch, request and enjoy your library

For ordinary use, you need two bookmarks: **Jellyfin to watch** and **Seerr to
request**. The other applications do the work behind them. You do not need an
SSH terminal or a Docker login to watch a film.

New here? Read [Start here](START-HERE.md) first. Looking for a download? Jump to
[Media and downloads](MEDIA-AND-DOWNLOADS.md).

## Get connected once

Ask the server owner for the Jellyfin URL, Seerr URL and your everyday account.
Keep those in a password manager and bookmark both sites.

On a typical single-VM installation, the local URLs look like:

```text
Jellyfin: http://APP_HOST:8096
Seerr:   http://APP_HOST:5055
```

`APP_HOST` is a placeholder for the address your owner gave you. It is not
something to type literally. If the installation uses separate machines,
private remote links or custom ports, use its handover sheet instead.

At home, use the owner's LAN links. Away from home, use the private connection
they commissioned, such as Tailscale. Installing the stack does not automatically
make it reachable from the internet. A private remote link may require signing
in to Tailscale and permission to reach that server. See
[remote access](../REMOTE-ACCESS.md) if you administer it.

## Sign in with the right account

Use the **everyday viewer** account for watching and requesting. Keep the
administrator account for maintenance. A Jellyfin user is an application
account, separate from the Linux or Proxmox login.

For the installer-managed Seerr setup, choose **Sign in with Jellyfin** and use
that same Jellyfin username and password. Other Seerr sign-in methods may appear,
but do not create a second local account just because that is the first form you
see. Your owner controls which users can sign in and what they can request.
See [Seerr's sign-in settings](https://docs.seerr.dev/using-seerr/settings/users/).

An already configured server can have older accounts with different passwords.
An installer update does not reset those passwords. If your login is rejected,
confirm the URL, sign-in method and account with the owner before trying more
passwords.

## Watch something already available

1. Open Jellyfin and sign in.
2. Open a movie or TV library, or search for a title.
3. Confirm the title, year and, for a show, episode.
4. Start playback. Use the player's audio/subtitle controls to select the track
   you want. The exact menu differs between the browser, phone, TV and Kodi.
5. Continue later from the same user account so your watched state and playback
   progress belong to you.

**Success looks like:** the right video plays, audio is correct, and playback
continues for a few minutes without repeated buffering. A poster appearing in
the library alone is not a playback test.

If you prefer Kodi, use the owner's already configured Jellyfin add-on. Jellyfin
for Kodi synchronizes library metadata into Kodi; JellyCon browses the server
through an add-on. They behave differently, so do not replace an existing add-on
just to match a screenshot in a guide. Official installation and behavior are in
the [Jellyfin Kodi guide](https://jellyfin.org/docs/general/clients/kodi/).

## Request a movie

1. Open Seerr, sign in with Jellyfin, and search for the title.
2. Open the result and check its year and description. Remakes often share a name.
3. If it is already available, open it in Jellyfin. There is no need to request
   another copy.
4. Otherwise, submit the request using the controls shown to your account.
5. Read the resulting status. Your owner may require approval, set request limits
   or restrict quality choices.

**Success looks like:** your request appears in Seerr. This confirms a request,
not a completed download. Seerr passes supported requests to Sonarr or Radarr;
those applications select releases using the configured sources and policies.
See [what Seerr does](https://docs.seerr.dev/).

Do not request the same title repeatedly because it has not appeared in Jellyfin
yet. Check its existing request and the [download pipeline](MEDIA-AND-DOWNLOADS.md#follow-one-title-through-the-pipeline).

## Request a show or anime

Follow the same Seerr search flow, then review the seasons before confirming.
Request the seasons you actually want rather than the entire show by habit.
Large season collections can consume considerable storage.

For anime, check the year, synopsis, season grouping and whether the result is
a series, film or remake. Similar names and different episode numbering make
the wrong match easy. A film is handled by Radarr; an episodic series is handled
by Sonarr.

If a request appears but no episode downloads, tell the owner the exact series,
season and episode. The owner may need to review Sonarr's series type, source
coverage, naming or release availability. The installer does not guarantee that
every anime is available, dubbed or subtitled.

## What do the request states mean?

The exact labels differ by app version. These are the practical distinctions:

| What you see | What it actually tells you | Your next action |
| --- | --- | --- |
| Waiting for approval | The owner has not approved acquisition | Wait or ask the owner about that request |
| Requested or approved | The request exists and can move into acquisition | Check progress; do not duplicate it |
| Downloading | A download client has started work | Let it finish; the file may still need unpacking/import |
| Partially available | Some of the requested series is available | Check which seasons/episodes are ready |
| Available | Seerr sees it in a selected media library | Open Jellyfin and test playback |
| Failed or stuck | A specific step needs attention | Send the title, state and time to the owner |

Seerr's library view is synchronized, so it can briefly lag behind an import.
Jellyfin visibility, request approval and file download completion are separate
steps. The owner can inspect its library synchronization in
[Seerr's media-server settings](https://docs.seerr.dev/using-seerr/settings/mediaserver/).

## Subtitles and audio

First open the player's track menu. A video may already contain English,
another language, commentary, or multiple subtitle tracks. Select the intended
track before asking for another file.

Bazarr can obtain extra subtitle files when its provider, language profile and
the particular title support it. The optional installer policy applies to future
additions and preserves existing tracks. It does **not** promise subtitles for
every old title or run a whole-library backfill. Give the owner the exact title
and episode if a needed track is missing. See [subtitle policy](../SUBTITLES.md).

Ask the owner which optional quality policy is enabled. The original preset
keeps original-language audio. The four-choice request policy defaults to
1080p English or anime 1080p dual audio, with explicit 4K choices. Anime can
fall back to Japanese when an English dub is unavailable. Audio preferences
and subtitle preferences are separate; check the actual player track menu.
See [selection policy](../MEDIA-PROFILES.md).

## If playback buffers

Try one item at a time:

1. Confirm whether another title plays in the same app.
2. If only one title fails, record its name, episode, audio and subtitle selection.
3. If you are remote, try once on the server's local network when possible.
4. Tell the owner which device/app you used and whether disabling a subtitle
   track changed the result.

A client that supports the file can play it directly. An incompatible video,
audio or subtitle format can require conversion on the server. That conversion
can be expensive on a small CPU-only machine; lowering the playback quality
can request more conversion rather than less. The owner should inspect the
active playback session before changing server settings. See
[Jellyfin codec support](https://jellyfin.org/docs/general/clients/codec-support/).

Do not change the VPN, storage paths or Docker configuration to troubleshoot
one buffering video. Use the [troubleshooting guide](TROUBLESHOOTING.md).

## Add another person

The owner creates a separate everyday Jellyfin user, selects the permitted
libraries, then verifies that the new person can sign in to Jellyfin and Seerr.
Seerr request permissions and limits need their own review. A new viewer should
not receive administrator permissions just to make requests work.

An owner can import Jellyfin users into Seerr or allow new media-server sign-ins,
depending on the intended policy. See
[Seerr's user enrollment](https://docs.seerr.dev/using-seerr/users/adding-users/).
Keep each person's account separate so watched history and requests stay theirs.

## What you normally do not need

- Prowlarr: the owner manages search-source connections there.
- Sonarr/Radarr: the owner uses these to inspect selection, imports and library policy.
- SABnzbd/qBittorrent: the owner uses these for actual transfer progress or manual jobs.
- Dockge/Proxmox: these manage infrastructure and are administrator tools.

Want to learn those next? Continue with [Media and downloads](MEDIA-AND-DOWNLOADS.md)
or [Operating the server](OPERATING-THE-SERVER.md).
