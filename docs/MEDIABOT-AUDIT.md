# MediaBot installation audit

Checked October 23, 2026, against MediaBot commit `8760e429af` in the
existing source checkout. MediaBot remains a separate, optional project:
<https://github.com/SamarieSystems/mediabot>.

## Install a separate instance

The portable installer and its documentation are present. The supported path
is Linux, Docker Engine and Compose 2.20+, Python 3.10+, a new Discord bot
application, and an existing configured Seerr installation. Select a published
stable MediaBot release before running the installer.

```sh
git clone https://github.com/SamarieSystems/mediabot.git
cd mediabot
git checkout vX.Y.Z
python3 scripts/setup.py
python3 scripts/setup.py --check
python3 scripts/setup.py --start
python3 scripts/setup.py --status
```

Replace `vX.Y.Z` with an actual release tag. The wizard collects the new
operator's credentials in hidden local prompts and creates a new private
`.env`. It refuses an existing file. The connection checker uses a disposable
container, without submitting a media request or logging the bot into Discord.
Startup uses a separate named data volume, an unprivileged bot user, and a
runtime healthcheck. Existing customized deployments must use their own
migration procedure instead of this fresh installer.

For independent households, use separate Discord applications, credentials,
checkouts, provider accounts and Compose project names. An allowlisted second
Discord server shares the installation's credentials and account links; it
does not create an isolated tenant.

The README and installation guide's clone/release links were updated to the
current organization remote. No live service, credential or Git history was
changed. An existing status-document modification was preserved.

## Verification performed

Run from the MediaBot checkout:

```sh
python -m unittest tests.test_setup tests.test_setup_check tests.test_fresh_install_gate -v
```

Result: **33 tests run, 32 passed, one skipped**. The skipped case exercises
symlink creation, which varies for unprivileged Windows accounts. Tested
boundaries include refusal to overwrite configuration, restricted file
permissions, hidden-input fallback refusal, rejection of legacy data mounts,
Compose environment isolation, sanitized provider diagnostics, blocked
credential-forwarding redirects, and exact ownership labels for disposable
test cleanup.

The repository also contains `scripts/test_fresh_install.py` and a CI clean
Docker installation job. The Docker harness was inspected but **not run in
this audit**. Unit tests and source inspection do not prove a real Discord
login or a confirmed Seerr request on another person's installation.

## Credential scan scope and limits

A read-only pattern scan examined **185 current tracked text files**, followed
by **all reachable Git objects across the local branches, remote-tracking refs
and tags**: 62 commits, 26 annotated-tag objects and **626 distinct file blobs,
22,489,464 bytes**. The expanded content scan used `git rev-list --objects
--all` and `git cat-file --batch`, with no file-size exclusion. All file blobs
were text. Commit messages and annotated-tag text were also checked.

Rules covered recognizable Discord, GitHub, AWS, OpenAI-style and Slack tokens,
private-key headers, credential URLs and long credential assignments. Findings
were inspected by location without printing credential values. All 126 pattern
matches were synthetic negative-test URLs or documented placeholder/validation
values in scripts and historical README examples. **No real credential was
identified within that scope.**

A separate filename search across available Git history found no committed
`.env`, `.env.local`, PEM/key files or database files. Current tracked
configuration includes a blank `.env.example`. Personal production addresses
remain in explicitly homelab-specific documentation; those addresses are not
authentication secrets and are not copied into this deployment toolkit.

`git ls-remote origin` was compared with the local reachable object set. All
59 advertised entries, including peeled tag targets, were already reachable
locally: two branch heads, 29 tags, the advertised pull-request head and HEAD.
The checkout is neither shallow nor configured as a partial clone. No fetch
or history mutation was required. This checks the advertised remote
state at audit time; it cannot cover deleted/unadvertised or unreachable
objects, GitHub issue/discussion contents, release assets, untracked local
files or unknown secret formats. Pattern matching cannot certify that an
arbitrary password never appeared. A confirmed exposed credential would
require revocation/rotation; removing a line from the latest commit alone
would not make it private again.

## GitHub readback

Read-only GitHub CLI/API queries confirmed the public repository's default
branch is `main`, with commit `8760e429af0b55af37f062227c3f56a529b548ab`,
matching the local checked-out commit. The default tree contains
`scripts/setup.py`, `scripts/test_fresh_install.py`, `docs/INSTALL.md`,
`.env.example`, `compose.yaml`, `LICENSE` and the CI workflow. The latest
published release is [MediaBot v2.12.1](https://github.com/SamarieSystems/mediabot/releases/tag/v2.12.1),
published October 19, 2026. Its tree was checked separately for these files.
The clone/release link corrections in this audit remain local changes until
reviewed and published; remote availability is not a claim they were pushed.

## Toolkit credential-handling regressions

The independent regression suite is:

```sh
python3 -m unittest discover -s tests -p test_security_regressions.py -v
```

On Windows, nine tests passed and the Docker parser test was skipped because
the Docker CLI is absent. In an isolated Linux temporary source tree, **all
10 tests passed** using Docker Compose 2.39.1. No containers were started or
pulled. Tests used only synthetic values and loopback HTTP servers.

The suite verifies blocked cross-origin redirects, ignored environment proxy
settings, sanitized HTTP failures, intact JSON/form/text response handling,
hidden-input warning refusal, and private file imports. A real `docker compose
config --format json` invocation verified dollar signs, quotes, internal and
trailing backslashes survive environment-file parsing. The assertion reverses
Compose's output serialization that doubles dollars in the rendered model,
as implemented by the [upstream Compose renderer](https://github.com/docker/compose/blob/main/cmd/compose/config.go);
it does not implement its own dotenv parser.

## Toolkit release check

This toolkit has an independent standard-library hygiene check:

```sh
python3 scripts/check-release.py
python3 scripts/check-release.py --root /path/to/candidate --json
python3 scripts/check-release.py --root /path/to/candidate --deny-file /private/operator-terms.json --json
```

It checks candidate files, including tests. It never prints matched values.
Known cache/dependency directories and `.git` are excluded and must also be
excluded from a release package. Binary files, oversized files, symlinks,
private configuration filenames and recognizable secrets require review
before publication. Standard credential rules are always enabled. Optional
`--deny-file` adds case-insensitive literal matches from an operator-owned JSON
list; neither the terms nor their contents appear in findings. That private
file must stay outside the candidate, be a regular file without symlinks or
hardlinks, and use owner-only permissions on Unix. Windows operators must
protect its ACL themselves. No operator's private identifiers are built into
the distributed scanner or its tests.

Synthetic credential assignments in tests or fixtures require both a
recognizable `fixture-`, `synthetic-` or `test-` value and an inline comment
such as `# release-audit: synthetic - negative credential fixture`. Explicit
placeholders in example configuration and environment-variable references
are accepted. A marker alone cannot exempt an arbitrary real token or private
key. Prefer building realistic negative-test strings at runtime rather than
storing complete token-shaped literals.

Python source that maps an image environment-variable name to a local secret
filename can use `# release-audit: reference - image variable to local secret
filename` on that mapping line. This exception accepts only lowercase
snake-case reference names ending in `_token`, `_password`, `_key` or
`_username`. It never suppresses recognizable token/private-key rules. Paths
in `_FILE`, `_PATH`, `_DIR` and `_SECRETFILE` settings are references too;
the scanner still examines their full lines for recognizable secret formats.
