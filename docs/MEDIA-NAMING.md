# Naming for future imports

Guided step 3 offers an optional, reviewed Sonarr/Radarr naming policy after
application wiring. It changes naming settings for future imports and new title
folders. It does not rename existing files, change existing title paths, move
media, start scans, import a library or search for releases.

The folder formats identify titles for Jellyfin:

```text
TV:     {Series CleanTitleWithoutYear} {(Series Year)} [tvdbid-{TvdbId}]
Movies: {Movie CleanTitle} ({Release Year}) [tmdbid-{TmdbId}]
Seasons: Season {season:00}
```

File formats retain quality source, release group, audio format/channels, dynamic
range and video codec when Starr has that metadata. Anime also retains absolute
episode numbering, audio languages and bit depth; movie filenames include
edition tags when available. Missing or incorrect metadata cannot be recovered
by a naming template alone. This does not enable media-file analysis or change
quality, language or download-selection policy.

Existing colon replacement, multi-episode style, specials-folder naming and
unrelated naming fields are preserved. Both applications must produce live
examples for the proposed formats. The terminal shows examples before the
optional apply question; declining keeps the current naming settings.

The installer stores the complete original naming resources privately under
`install_root/backups/media-naming-REVIEW-HASH.json`. It sends only the reviewed
naming resource to each application's naming-settings endpoint, then reads both
resources back exactly. A changed host, site, naming resource or application
ownership invalidates a stale plan. If one application saves successfully and
the other fails, keep that partial result and run the same step for a fresh
review. The already-matching application is verified without another write.

To review it later, run `./setup 3` again, or use the advanced commands with the
actual saved private site path:

```sh
./stack --config /actual/private/site.json naming plan --out naming-plan.json
./stack --config /actual/private/site.json naming apply --plan naming-plan.json --accept-plan REVIEW-HASH
./stack --config /actual/private/site.json naming verify
```

Use the actual `review_hash` from the private plan. Verification does not write
settings and exits `2` if the requested naming policy is not currently applied.
There is no bulk-rename command in this module. Changing the storage tree or
adopting an old library remains a separate, deliberate operation.

The formats follow the [Sonarr naming guide](https://trash-guides.info/Sonarr/Sonarr-recommended-naming-scheme/)
and [Radarr naming guide](https://trash-guides.info/Radarr/Radarr-recommended-naming-scheme/).
The API examples request supplies the proposed resource's ID and settings so
Starr renders that proposal rather than silently substituting its saved policy.
