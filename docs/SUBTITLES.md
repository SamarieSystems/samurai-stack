# English subtitles

Run `./setup 3` to review the optional Bazarr policy after the media applications
are connected. It uses Gestdown for TV and YIFY Subtitles for films. Both are
account-free providers included in the pinned Bazarr image. Provider coverage,
rate limits and subtitle availability can change independently of this stack.

The installer adds one `Samurai English` language profile and selects it for
**future additions only**. Existing media keeps its current profile. Existing
profiles, embedded subtitle tracks, SRT files and ASS files are preserved.
English embedded subtitles satisfy the profile, so another copy is unnecessary.

The policy runs one job at a time. It disables subtitle upgrades, AI fallback,
postprocessing, subtitle modifications and scheduled backlog searches. It does
not transcode, extract embedded tracks, modify video files, or search the whole
library. At least 50 GiB must be free on media storage and 20 GiB on application
storage before setup or verification proceeds. These are setup guards, not a
continuous quota on future subtitle downloads. See [capacity controls](CAPACITY.md)
for the stack's separate runtime capacity policy.

The review explicitly replaces the enabled-provider selection with the selected
providers. Existing provider credentials remain stored. Declining this optional
step leaves the current subtitle policy intact and lets media setup continue.
Reruns verify a saved completed policy and keep its provider choices.

Provider registration and current throttle status are checked after saving.
That does not prove a matching subtitle exists or that a download succeeded.
The installer reports this distinction and surfaces provider throttling.

If an earlier installer created the exact managed English profile with frontend
item field names instead of Bazarr's stored field names, setup offers a reviewed
repair. It saves a complete private before snapshot, changes only that
receipt-owned profile, and keeps every other profile and existing title
assignment. Edited or unrelated profiles are never converted automatically.

## Advanced reviewed commands

These are the same operations used by the guided installer. Run inside the media
VM as root, from the extracted source folder, using your saved site configuration:

```bash
./stack --config /path/to/site.json subtitles plan --out subtitles-plan.json
```

Inspect that private plan, then apply its exact `review_hash`:

```bash
./stack --config /path/to/site.json subtitles apply --plan subtitles-plan.json --accept-plan REVIEW_HASH
./stack --config /path/to/site.json subtitles verify
```

`--providers gestdown,yifysubtitles` can be supplied to `subtitles plan`. The
provider files must exist in the owned pinned Bazarr container, and the running
service must register the chosen providers. Unsupported providers, including
Whisper and embedded-track extraction, are rejected by this policy.

The internal Python API also supports an optional OpenSubtitles.com username and
password through `save_provider_credentials`. It stores these in a root-private
input file and never accepts them as command-line values. Selecting that provider
requires its existing private input and verifies the saved fields while excluding
AI and machine-translated subtitle results. The default guided setup does not
require an external subtitle account.

## Existing-library processing

This release does **not** perform existing-library subtitle backfill. Bazarr's
single-item download endpoints queue asynchronous jobs. Its episode metadata can
also hide embedded subtitles on media without a language profile, so an empty
subtitle list alone is insufficient evidence that a file needs subtitles.

A future bounded backfill must identify exact Sonarr/Radarr file IDs and paths,
inspect embedded languages, preserve current sidecars, recheck free space, and
verify completion for a small reviewed batch. It must not assign a whole TV
series merely to search one episode. No global backlog command is used here.
