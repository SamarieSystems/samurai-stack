# Retry and recovery

## Keep installation state separate from source

Alpha 6 saves the verified private site at
`/var/lib/samurai-stack/installations/DEPLOYMENT-UUID/site.json`. It binds that
copy to this host and its application/storage identity. Guided progress and
pre-storage drafts live under `/var/lib/samurai-stack/setup`; appdata,
secrets, receipts and image locks remain beneath the configured `install_root`.
Keep backups of all the required parts. Replacing a source ZIP does not require
recreating accounts, generating a new UUID or deleting application state.

Run a new source checkout's `./setup` or the verified portable
`python3 /path/to/samurai-setup.pyz` to resume trusted saved configuration.
When upgrading an older installation that has no canonical copy yet, explicitly
pass its original private site with `--config /actual/path/to/site.json`.
Storage verification must pass before it is saved to the canonical registry.
An unavailable mount keeps the original configuration usable for diagnosis.

The registry contains configuration, not application databases or passwords.
It cannot restore deleted Jellyfin/Seerr accounts, watch history, provider
credentials or media. Those require their actual files and appropriate backups.

## Inspect a blocked or partially deleted installation

```sh
./setup --recover
```

This is read-only: selected container labels/status, host bind paths and their
existence, managed service references and mount identity. It does not read
application account/configuration contents, adopt containers, repoint media,
stop services or restore deleted files. Keep any report containing private
network/path information local.

A project with another UUID pauses before application or image writes. A
matching UUID does not make a deleted, recreated or unsafe appdata path valid.
If containers still exist, leave them in place while checking the original
configuration and backups. Renaming the project does not recover its databases
or free its occupied ports.

After an intentional clean restart that removed all application containers and
appdata, default discovery still refuses dangling service references without a
trusted config. Supply the chosen new private site explicitly:

```sh
./setup --config /actual/path/to/site.json 3
```

Setup may retire the conflicting old generated unit for that exact project
and stage only when both the project and the old UUID have **zero containers,
including stopped ones**. It validates root ownership, original generated
commands, current loaded configuration and absence of systemd drop-ins. Then
it stops/disables only that unit, rechecks absence and moves the original file
into a private unique directory under `/var/lib/samurai-stack/retired-units`.
Other units, application directories, images, volumes and media are not removed.
Custom units and any remaining container require review instead.

A first-run local storage choice on the VM root filesystem has a separate
[`--choose-storage` action](QUICKSTART.md#correct-an-unused-storage-choice).
It requires an unregistered, empty setup with no application state or project
containers and keeps a private original-config backup. It selects an already
mounted local filesystem and new empty child, without moving existing media,
editing mounts or adopting another installation's marker.

## Retry a stage

`plan` inspects the current host and proposed stage. `apply` creates only that stage's artifacts. `configure` links supported application settings through their APIs. `verify` checks persisted settings and running services. None of these commands destroys containers, volumes or media as rollback.

If an apply fails, inspect `install_root/state/stage-N.json`, the stage Compose file, image lock and the service journal. Vendor logs can contain credentials; do not paste them publicly without checking them. Complete partial enrollment, generate a new plan using a new filename, and retry. Images already locked remain fixed.

A stale apply lock means the earlier process may still exist. Read the recorded PID/host/time and confirm that process is gone on the recorded host. Preserve the lock under a timestamped recovery filename before another apply. Do not clear a lock simply because a command is taking a while.

Generated units are named `samurai-DEPLOYMENT-N.service`. For deployment `homelab`, media is:

```bash
sudo systemctl status samurai-homelab-2.service
sudo journalctl -u samurai-homelab-2.service --since '20 minutes ago'
sudo systemctl restart samurai-homelab-2.service
```

Mount checks run before systemd starts the data-dependent stages. Management has no NAS dependency. Container restart policies are disabled so the Docker daemon cannot bypass the pre-start guard during boot. A failed container can cause the stage wrapper to restart the stage, with a bounded systemd restart rate. An unhealthy but running container still requires diagnosis; health checks do not repair it automatically.

When a reviewed retry is ready to start its generated unit, setup clears that
exact unit's failed/start-limit state. It does not reset failures for every
systemd service or replace failed verification with a forced start.

Dockge can display the generated Compose stacks, logs and terminals. The systemd wrapper is the boot/recovery authority. For deliberate maintenance of a complete stack, stop its systemd unit first; do not simultaneously use two controllers to stop/redeploy the same project. Persistent manual changes in a generated Compose file will invalidate the old apply plan. The next deliberate apply preserves that file in `install_root/backups` before generating its replacement.

There is no automatic upgrade, downgrade or database rollback command. Preserve image locks and obtain application-consistent backups before changing versions. A previous Compose file cannot undo a database migration. Use the application's documented backup/restore procedure and test a separate restore.

Install-state/configuration files and private secret files are sensitive. Back them up securely. A config-file backup alone does not include media, Immich originals, database contents or an independent NAS backup. Recovery is not proven until the required applications and files have been restored elsewhere.

Removing an installation is deliberately manual in this release. There is no clean-all, volume-prune, data-delete or factory-reset command.
