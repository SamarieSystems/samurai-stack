# Media selection policy

Run `./setup --quality-profiles`, or choose **p** in the setup menu. Fresh
Sonarr/Radarr enrollment offers the same reviewed policy before Seerr wiring.
This is optional. Declining leaves the current policy in place.

The default policy reads the live schemas and creates one named profile per app:
`1080p Original | <media-project>`, for example
`1080p Original | home-picturehouse`. Its custom formats use the same
project-qualified display convention. Existing profiles, titles, files and
download queues are preserved. An existing resource with that name must match
the policy exactly; a conflict stops setup instead of overwriting it.

A reviewed apply can relabel an exact legacy
`Samurai <installation> 1080p Original` profile and its matching custom formats
through the supported APIs. It keeps their original IDs, scores, policy and
title assignments. Verification reads either exact label without renaming.
Duplicate old/new candidates or a policy mismatch require review; display
cleanup does not authorize changing the policy or reassigning existing titles.

- Allow HDTV, WEB and Blu-ray at 720p or 1080p. Exclude 4K, remux, DVD and CAM.
- Keep original audio. English-origin titles require English; foreign titles
  retain their original language. This is not an English-only policy.
- Reject release names parsed as HDR or Dolby Vision. Avoid automatic quality
  upgrades, which would otherwise replace an already downloaded file.
- Use the new profile for newly created Seerr connections when it exists.
  Existing Seerr profile selections and existing titles are not reassigned.
  Select the named profile when adding future titles directly in Starr.

Audio and HDR decisions depend on metadata and release-name parsing. They do
not inspect an undownloaded file's real streams and cannot defeat a mislabeled
release. SDR and 1080p also do not guarantee direct playback on every client;
an incompatible codec can still require CPU transcoding.

The Sonarr language condition follows its documented
[original-only custom-format recipe](https://github.com/Servarr/Wiki/blob/master/sonarr/faq-v4.md#only-original).
Radarr uses its native profile language selection and
[original-language decision logic](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/DecisionEngine/Specifications/LanguageSpecification.cs).

## Four request choices, explicit opt-in

An existing installation with the exact installer-managed HD policy can opt into
four quality profiles in each app. The plan reuses the existing standard profile
ID, creates three additional profiles, and configures the existing Seerr backend.
This changes the original-audio policy deliberately:

| Quality profile | Allowed video | Audio preference |
| --- | --- | --- |
| `1080p English \| <media-project>` | 720p/1080p SDR | English required |
| `Anime 1080p Dual Audio \| <media-project>` | 720p/1080p SDR | English and dual audio preferred; Japanese fallback |
| `4K English (opt-in) \| <media-project>` | Encoded 2160p WEB/Blu-ray | English required |
| `Anime 4K Dual Audio (opt-in) \| <media-project>` | Encoded 2160p WEB/Blu-ray | English and dual audio preferred; Japanese fallback |

Remuxes, CAMs, DVD and automatic quality upgrades are excluded. Dual-audio
release names receive a strong preference, followed by English subtitle-name
evidence and English audio. English-only dubs remain eligible. A Japanese anime
release can remain eligible without an English dub, so a show is not blocked
until a dub exists. Release metadata cannot prove the actual audio or subtitle
tracks in a file; Bazarr's English language profile handles subtitles separately.

Generate the explicit plan using the private site file for the installation:

```bash
./stack --config /path/to/site.json profiles plan --request-choices --out request-profiles-plan.json
```

Read the displayed policy and private plan. It lists every metadata-confirmed
anime reassignment, unused factory preset retirement, unmonitored collection
profile change and shared UHD size limit. Apply its exact `review_hash`:

```bash
./stack --config /path/to/site.json profiles apply --plan request-profiles-plan.json --accept-plan REVIEW_HASH
./stack --config /path/to/site.json profiles verify
```

The installer backs up the API resources before writing. Existing title IDs,
paths, files, queues and collection monitoring are retained. It may assign
metadata-confirmed anime to the Anime HD profile without searching, downloading
or upgrading anything. It retires only the known factory profiles after checking
title, import-list, collection, Seerr request, server-default and override-rule
references. Unrelated/custom profiles and assigned factory profiles stop the
operation. Review those conflicts rather than deleting appdata.

Seerr's normal default is HD; Sonarr also receives a separate Anime HD default.
For an anime movie, choose the Anime profile explicitly. Standard viewers need
Seerr's **Advanced Requests** permission to use **Advanced Request > Quality
Profile**. That permission does not grant application administration. The four
choices appear in that native dropdown; there is no duplicate 4K server or a
second stored copy. Requesting an already available title does not replace it
with 4K. An owner must deliberately arrange that replacement in Starr.

The review includes these **global** size definitions for the three encoded UHD
qualities. They affect every profile using those qualities:

| App | Minimum / preferred / maximum MB per minute |
| --- | --- |
| Sonarr 2160p WEB/Blu-ray | 15 / 55 / 100 |
| Radarr 2160p WEB/Blu-ray | 20 / 90 / 150 |

A 45-minute episode targets about 2.5 GB and caps at 4.5 GB; a two-hour movie
targets 10.8 GB and caps at 18 GB, in decimal units. HD size definitions stay
unchanged unless the plan also includes `--global-sizes`. UHD permits HDR, but
rejects a Dolby Vision release name without advertised HDR10 fallback. A CPU-only
server still needs a compatible direct-play client; this policy adds no
transcoding capability.

After successful opt-in, ordinary `profiles plan/apply/verify` and setup's **p**
menu preserve this saved four-profile policy. They do not recreate a fifth
Original profile. Applying a changed policy flushes only Seerr's Starr lookup
caches and verifies its request dropdown as well as the underlying apps. After
an interrupted attempt, generate a fresh plan and retry. Exact owned partial
resources are reused; missing pinned profiles or intervening owner edits require
review and are not silently replaced.

## Optional shared size limits

After profile verification, setup offers a second, separate size review.
**Quality definitions are global to each Starr app**, including other profiles
using those qualities. The default Original profile does not change them unless
the separate size review is accepted; the four-choice opt-in includes the UHD
limits shown above in its explicit review.
Review the displayed before/after policy before applying it. Existing files
are not rescanned, moved or redownloaded.

| App | Resolution | Minimum / preferred / maximum MB per minute |
| --- | --- | --- |
| Sonarr | 720p | 5 / 20 / 40 |
| Sonarr | 1080p | 10 / 45 / 75 |
| Radarr | 720p | 8 / 30 / 60 |
| Radarr | 1080p | 15 / 60 / 90 |

The 1080p targets are about 2 GB per 45-minute episode and 7.2 GB per two-hour
movie in decimal units. These are storage-conscious starting points, not a
quality guarantee or a fixed size for every title. Only the eight permitted
HD quality definitions change. Other settings are retained, with a private
pre-change resource snapshot under the installation's `backups` directory.

## Advanced review interface

From the source checkout, use the intended installation's private site file:

```bash
./stack --config /path/to/site.json profiles plan --out profiles-plan.json
```

Inspect that private plan and its `review_hash`, then apply that exact hash:

```bash
./stack --config /path/to/site.json profiles apply --plan profiles-plan.json --accept-plan REVIEW_HASH
./stack --config /path/to/site.json profiles verify
```

To include shared size limits, generate a new plan with `--global-sizes` and
verify with the same flag. Applying a stale plan, conflicting resource or
wrong installation is refused before accepting policy changes. Interrupted
API operations can leave a partial profile setup; review a new plan and retry.
The installer never deletes resources as an automatic rollback.
