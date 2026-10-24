# Design references

Reviewed October 23, 2026. These establish vendor behavior and recommended layouts; they do not certify this project's fresh-machine rollout.

- Proxmox cloud-init, imported images, keys and cloud-init drives: https://github.com/proxmox/pve-docs/blob/master/qm-cloud-init.adoc
- Proxmox IOMMU/passthrough and current kernel defaults: https://github.com/proxmox/pve-docs/blob/master/qm-pci-passthrough.adoc
- Debian official cloud images/checksums: https://cloud.debian.org/images/cloud/
- Cloud-init completion/error status: https://docs.cloud-init.io/en/latest/reference/cli.html and https://docs.cloud-init.io/en/latest/explanation/return_codes.html
- Docker installation repositories and firewall behavior: https://docs.docker.com/engine/install/debian/ and https://docs.docker.com/engine/install/ubuntu/
- Docker socket administration boundary: https://docs.docker.com/engine/security/protect-access/
- Compose bind mounts, health/readiness and service configuration: https://docs.docker.com/reference/compose-file/services/
- Compose dotenv quoting/interpolation: https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/
- TRaSH shared data layout/hardlinks: https://trash-guides.info/File-and-Folder-Structure/How-to-set-up/Docker/
- Gluetun port forwarding and the distinction from Docker published ports: https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/vpn-port-forwarding.md
- Immich Compose and local database requirement: https://docs.immich.app/install/docker-compose/
- slskd configuration and Docker volume identity: https://github.com/slskd/slskd/blob/master/docs/config.md and https://github.com/slskd/slskd/blob/master/docs/docker.md

Image versions are defined in `samurai_stack/catalog.py`; each installation records the actual registry digests separately. A tagged vendor release is not a promise of indefinite API compatibility. Application wiring checks refuse unexpected schemas or conflicting existing settings.
