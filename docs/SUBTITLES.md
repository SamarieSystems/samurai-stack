# English subtitles

Run `./setup 3` to review the optional Bazarr policy after the media applications
are connected. New setup uses Gestdown for account-free TV searches and offers
an optional OpenSubtitles.com account for TV and film searches. Skipping that
account leaves movie subtitle searches unconfigured; existing embedded and
sidecar movie subtitles still work. YIFY has demonstrated external connection
failures and is no longer a default. Provider coverage, rate limits and subtitle
availability can change independently of this stack.

The installer adds one `English | <media-project>` language profile and selects it for
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

`--providers gestdown,opensubtitlescom` can be supplied to `subtitles plan` after
the private account input is saved. Omitting `--providers` keeps a receipt's
existing provider choices; on a new installation it selects Gestdown only. The
provider files must exist in the owned pinned Bazarr container, and the running
service must register the chosen providers. Unsupported providers, including
Whisper and embedded-track extraction, are rejected by this policy.

The guided setup asks for an optional OpenSubtitles.com username and password
through hidden terminal prompts. The internal Python API uses the same
`save_provider_credentials` operation. It stores these in a root-private
input file and never accepts them as command-line values. Selecting that provider
requires its existing private input and verifies the saved fields while excluding
AI and machine-translated subtitle results. The default guided setup does not
require an external subtitle account for TV searches.

Use the **OpenSubtitles.com account username**, not its email address. The website
and API have different login requirements; the installer rejects email input
before changing Bazarr. An OpenSubtitles.org login is a separate provider login
and is not substituted automatically. Bazarr supplies its application API key;
this integration does not require a personal developer API key. The account's
provider quota and rate limits still apply.

To add movie searches or replace a legacy YIFY selection after saving private `.com` input,
review a plan with `--providers gestdown,opensubtitlescom`, then apply its exact
hash using the commands above. This retains Gestdown for TV and removes YIFY from
the enabled provider list. It preserves the language profile, existing title
assignments and subtitle files. Provider registration is still not delivery
proof: a successful single-title test must produce a valid new English sidecar
and a matching Bazarr download-history entry. A queued or completed search job
without those artifacts is not a successful subtitle download.

## Existing subtitle input bindings

Current subtitle receipts and private provider input are bound to the Bazarr
stage's deployment, host, media address/project, application path, storage
identity and UID/GID. Enabling a separate photo or music stage does not change
that authority. The running pinned image, provider source hashes and original
Bazarr directory are still verified separately.

Older receipts used the entire site configuration. If a later optional-stage
change makes that old binding fail, keep the receipts and credentials. Use the
**actual captured pre-change site file**, whose full digest must match those
legacy inputs, to review a metadata-only migration:

```bash
./stack --config /path/to/site.json subtitles binding-plan --previous-config /path/to/captured-old-site.json --out subtitle-binding-plan.json
./stack --config /path/to/site.json subtitles binding-apply --previous-config /path/to/captured-old-site.json --plan subtitle-binding-plan.json --accept-plan REVIEW_HASH
./stack --config /path/to/site.json subtitles verify
```

The review refuses changed media endpoints, project names, paths, storage or
UID/GID. It also verifies the saved native policy, selected provider credentials,
image/source hashes and persistent Bazarr directory. Apply checks the exact
receipt/input hashes again under the deployment lock, saves private byte-for-byte
backups, and changes only binding metadata. It does not update native settings,
start jobs, move media or restart services. Backups of provider input contain
credentials and must stay private. After an interrupted metadata update, generate
a fresh plan against the same confirmed predecessor and retry; matching partial
updates are retained. Missing, foreign or edited policy resources are not adopted.

## Existing-library processing

This release does **not** perform existing-library subtitle backfill. Bazarr's
single-item download endpoints queue asynchronous jobs. Its episode metadata can
also hide embedded subtitles on media without a language profile, so an empty
subtitle list alone is insufficient evidence that a file needs subtitles.

A future bounded backfill must identify exact Sonarr/Radarr file IDs and paths,
inspect embedded languages, preserve current sidecars, recheck free space, and
verify completion for a small reviewed batch. It must not assign a whole TV
series merely to search one episode. No global backlog command is used here.
