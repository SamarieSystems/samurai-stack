# Media selection policy

Run `./setup --quality-profiles`, or choose **p** in the setup menu. Fresh
Sonarr/Radarr enrollment offers the same reviewed policy before Seerr wiring.
This is optional. Declining leaves the current policy in place.

The installer reads the live schemas and creates one named profile per app:
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

## Optional shared size limits

After profile verification, setup offers a second, separate size review.
**Quality definitions are global to each Starr app**, including other profiles
using those qualities. They are never changed by profile creation alone.
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
