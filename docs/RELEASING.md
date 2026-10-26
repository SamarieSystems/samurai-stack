# Building a release

The operator entry point is Bash. Python 3.11+ and Git implement the local
build; no pip packages, Docker daemon or running applications are needed.

1. Finish the scoped tests and update `__version__`, changelog and measured
   validation record. Use `0.2.5` for tag `v0.2.5`; a prerelease such as
   `0.3.0rc1` must match `v0.3.0-rc.1`. Commit the reviewed public files. Mark
   `setup`, `stack` and `scripts/release.sh` executable in Git.
2. Run the builder against that clean committed source, with a new output
   directory outside the checkout:

   ```bash
   bash scripts/release.sh --tag v0.2.5 --output ../samurai-release-v0.2.5
   ```

3. Inspect the result and test the actual generated installer in an isolated
   VM. Publish a candidate tag and release, wait for tag CI and download the
   assets again to verify their bytes and checksums before announcing it.

The builder never creates or changes tags, commits, pushes, publishes or
modifies a running installation. It reads canonical Git blobs, refuses a tag
whose version or existing commit differs, and audits the committed source
without exposing credential values. Optional `--deny-file /private/terms.json`
adds a private operator-owned literal deny list; that file is never packaged.
An existing output directory is refused, including a partially completed build.
Keep partial evidence and select a new output path when retrying.

Each release contains the source ZIP and tar.gz, a source manifest identifying
the exact commit and per-file hash/mode, `samurai-setup.sh`, the compatibility
`samurai-setup.pyz`, the focused `samurai-hardlinks.pyz`, and `SHA256SUMS`.
The source archives include the same manifest. Fixed archive timestamps make
repeated builds from the same commit reproducible within the same build
runtime; exact release checksums remain authoritative across runtime versions.
The embedded payloads are built from the same private Git snapshot as the
archives. Uncommitted checkout changes cannot be substituted into an asset.

Checksums establish byte consistency, not a separate signing identity. Fetch
them from the intended repository's release, inspect the source when needed,
and keep private deployment files outside the public checkout.

## Language boundary

- Operators run `./setup`, `./setup NUMBER` and advanced `./stack` commands.
- Maintainers run `bash scripts/release.sh` for local release builds.
- Python modules own validated configuration, plans, recovery, API wiring and
  deterministic packaging. Internal Python scripts are implementation tools.
- MediaBot is a separate optional project and retains its own documented
  installer. Samurai Stack neither starts it nor reuses a household's bot
  credentials during another installation.
