# Retry and recovery

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

Dockge can display the generated Compose stacks, logs and terminals. The systemd wrapper is the boot/recovery authority. For deliberate maintenance of a complete stack, stop its systemd unit first; do not simultaneously use two controllers to stop/redeploy the same project. Persistent manual changes in a generated Compose file will invalidate the old apply plan. The next deliberate apply preserves that file in `install_root/backups` before generating its replacement.

There is no automatic upgrade, downgrade or database rollback command. Preserve image locks and obtain application-consistent backups before changing versions. A previous Compose file cannot undo a database migration. Use the application's documented backup/restore procedure and test a separate restore.

Install-state/configuration files and private secret files are sensitive. Back them up securely. A config-file backup alone does not include media, Immich originals, database contents or an independent NAS backup. Recovery is not proven until the required applications and files have been restored elsewhere.

Removing an installation is deliberately manual in this release. There is no clean-all, volume-prune, data-delete or factory-reset command.
