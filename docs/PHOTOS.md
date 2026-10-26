# Optional Immich setup

Run `./setup 6`, select Photos, and keep the existing media installation. The
guided path now enrolls a proven-new Immich owner through the native API and
applies bounded background processing. It does not require a browser setup
wizard. Existing owners must supply their current email login and password;
the installer never resets accounts or replaces an existing photo database.

Immich requires an email-shaped login, even if you use a local-only identity.
The setup prompt does not send mail. For a new owner you can reuse the saved
media administrator password; for an existing owner use the current Immich
password. Login input is private to root outside the source checkout. A typo
can be corrected with a private-input backup; that corrects the installer's
saved input, not the application's password.

## Shared-server resource defaults

The guided photos path defaults to CPU mode with machine learning disabled.
Ordinary uploads, albums, thumbnails and original downloads remain available.
AI semantic search, face recognition and OCR need machine learning and are not
available in this lean configuration. More waiting is preferable to exhausting
the RAM used by the media stack, but memory bounds are not a throughput promise.

- Server: 2 GiB memory, one CPU quota, one FFmpeg thread.
- PostgreSQL: 2 GiB memory, half a CPU quota, local application disk.
- Valkey: 128 MiB memory, quarter of a CPU quota.
- All 15 supported native background queues: concurrency one.
- Photo-owner quota: 2 GiB by default, explicitly shown in the reviewed plan.
  Other users are created by the owner in Immich and need their own quotas.

This is about 4.125 GiB of aggregate container memory limits with ML off, in
addition to the OS and other applications. Actual use varies. Do not infer that
a 4 GiB machine has enough space for this whole media stack. Immich documents
6 GiB minimum for its usual installation and a smaller configuration with ML
disabled. Review the [current upstream requirements](https://docs.immich.app/install/requirements/).

`extras.photos` supports `machine_learning`, `user_quota_gib`,
`data_reserve_gib` and `app_reserve_gib`. The default free-space floors are
50 GiB on media storage and 20 GiB on the application disk. The optional-stage
timer stops only photos/music if the backing mount is wrong, cannot be proved,
or reaches its floor. It records a persistent hold and does not delete data or
automatically resume after space recovers. Inspect the owned
`state/extras-capacity-hold.json` and the named optional units before a reviewed
restart. A per-user originals quota does not include every thumbnail, database
or backup byte; the filesystem reserve is still necessary.

## Paths and acceptance

Uploads and generated photo files use `data_root/photos/upload`, mounted as
`/data` in Immich. PostgreSQL stays under
`install_root/appdata/immich-postgres` on local Linux storage. The separate
`photos/external` directory is mounted read-only and is not automatically
registered or scanned. Existing media and family photos are not imported,
moved, renamed or adopted.

Before trusting it with personal originals:

1. Log in as the owner, then as a normal user. Verify the latter cannot access
   administration. Use the LAN address locally and the private overlay address
   remotely, both on port 2283; do not open a router port for setup.
2. Upload a disposable picture, retrieve its original and compare SHA256.
   Confirm its thumbnail loads and its actual bytes are on the expected disk.
3. Restart the owned photo stage and confirm the account and asset survive.
4. Copy **originals plus a consistent database backup** to independent storage
   and test an isolated restore. An installer receipt, a thumbnail cache or an
   untested VM backup is not an independent photo backup.

The installer records native account/resource readback separately from these
tests. It does not label an upload or restore successful just because containers
are healthy. Keep the original copy elsewhere until a restore has passed.

## Advanced maintenance

After guided setup has saved owner input, the Bash entry point exposes a narrow
maintenance path without deploying containers or importing photos:

```sh
sudo ./stack --config /path/to/site.json photos plan --out photos-review.json
sudo python3 -m json.tool photos-review.json
sudo ./stack --config /path/to/site.json photos apply --plan photos-review.json --accept-plan REVIEW_HASH
sudo ./stack --config /path/to/site.json photos verify
```

Replace `REVIEW_HASH` with that private plan's exact hash. A changed owner input,
container identity, datastore, native configuration or quota requires a fresh
review. First-owner creation additionally requires an empty datastore reserved
before first startup; an uninitialized web page alone does not grant authority.
An interrupted claim can be reviewed again only against the same owned database
and saved login. The native API still rejects a second first administrator.
