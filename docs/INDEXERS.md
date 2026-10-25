# Source intake and acquisition readiness

Guided step 3 tests the existing enabled Prowlarr sources and shows which selected
download protocols cover movies, TV and anime. It can add one authorized NZBGeek,
generic Newznab or generic Torznab source per run without a browser wizard:

```sh
./setup 3
```

The portable release uses the same flow with `bash ./samurai-setup.sh 3`.
Current installations reuse their trusted saved configuration. This is a resume,
not a reinstall, and it does not import or move existing media.

## Usenet server versus indexer

The NNTP prompt is for the download provider, such as Frugal Usenet. SAB uses its
server hostname, TLS port and service credentials to retrieve articles. NZBGeek
is an indexer: Prowlarr uses its API key to find NZBs. They are separate accounts
and separate checks. The installer does not supply subscriptions or register
provider accounts.

Torrents-only installations are not asked for NZBGeek or NNTP credentials.
For both download methods, each selected protocol needs at least one working
source. The protocol preference does not remove that readiness requirement.

## What the terminal handles

Healthy existing sources are tested and kept without another credential prompt.
Disabled sources remain disabled. Their names, keys, categories and priorities
are not replaced. Existing Cardigann and HTTP/LAN sources can still be tested;
new intake supports HTTPS Newznab/Torznab endpoints only. Providers requiring a
different schema still need deliberate setup in Prowlarr, then `./setup 3` can
verify them.

When coverage is missing, the wizard offers the applicable source types and
saved private inputs. NZBGeek needs its API key. A generic source also needs a
label, HTTPS base URL and API path, normally `/api`. Key input is hidden; a
public Torznab feed may have no key. If multiple search-enabled Prowlarr
application profiles exist, select the intended existing profile ID. No profile
is silently changed or created.

The installer tests the proposed source before showing a private reviewed plan,
then tests again before an approved additive write. It reads back the saved
source and tests the actual saved ID. A lost response can resume using the saved
private input without creating duplicates. A conflicting name or endpoint
pauses rather than overwriting an existing source.

API keys stay in mode-600 files beneath the installation's private `secrets`
directory. They are not placed in `site.json`, command arguments, reviewed plans
or normal output. Keep the installation directory and any backups private.

## Pending means pending

The readiness matrix reports actual discovered Newznab categories. Movie and TV
coverage are required; anime is reported separately and is true only when the
source advertises TV/Anime category `5070`. A broad TV category alone is not
evidence of anime coverage. This does not assign anime profiles or guarantee a
particular release exists.

Zero sources cannot pass. An enabled source with a failing test leaves readiness
pending, even if another source works. Repair that source, or deliberately
disable it in Prowlarr if it is no longer wanted. Missing coverage can be added
one source at a time; run the same step again for another missing protocol.
Blank optional intake pauses with an actionable result instead of repeatedly
asking for credentials. Application/container health is reported separately.

These checks establish source authentication, search enablement and advertised
category coverage. They do not run searches, grab releases, sync applications,
download files, test VPN forwarding or prove playback. The application's saved
Prowlarr connections and a real authorized download/import/playback test still
need to pass their separate checks.

## Advanced readback and reviewed retries

Use the actual saved site path, not a new example site:

```sh
./stack --config /actual/private/site.json indexers verify
```

Verification tests existing enabled sources without changing their configuration.
A pending readiness result exits with status `2`. The CLI does not accept an API
key flag. Credential intake belongs in the hidden guided prompt.

For a saved private input, `INPUT-UUID` is the opaque ID in the corresponding
`secrets/indexer-input-INPUT-UUID.json` filename, not its contents. A reviewed
retry uses:

```sh
./stack --config /actual/private/site.json indexers plan --input-id INPUT-UUID --out indexers-plan.json
./stack --config /actual/private/site.json indexers apply --plan indexers-plan.json --accept-plan REVIEW-HASH
```

Use the actual `review_hash` from the private plan. Host, configuration, owned
runtime, source inventory, application-profile or intake changes invalidate an
old plan; create and review a fresh one. Do not paste the private input file into
chat or a public issue.

The contracts follow the [Prowlarr API](https://prowlarr.com/docs/api/) and the
[Newznab category specification](https://newznab.readthedocs.io/en/latest/misc/api.html).
