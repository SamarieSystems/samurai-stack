# Image pins

Every default in `samurai_stack/catalog.py` is an immutable SHA256 reference, including optional services. A first install does not resolve a moving `latest`, major-version or runtime tag. Per-site image lock files still record what the deployment uses.

## Provenance and architecture

Captured on **2026-10-24**. Twenty pins came from the fresh disposable Debian 13 VM's stage 2-6 image locks and compiled Compose definitions. The optional port-sync runtime was resolved from Docker's Official `python:3.13-alpine` image, pulled into that guest, and ran `Python 3.13.17` in an isolated container. This validates runtime availability, not an actual provider port-forwarding flow.

All 21 references were checked against their official registries and with `docker buildx imagetools inspect --raw`. The exact manifest bytes hash to the recorded digest. **All 21 are multi-platform index digests**, containing at least `linux/amd64` and `linux/arm64`. Additional platforms vary by image. Attestation descriptors marked `unknown/unknown` are not counted as runnable platforms. An index retains upstream architecture selection instead of forcing the amd64 child manifest onto another host. See Docker's [manifest inspection reference](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/).

Actual application acceptance ran on **linux/amd64 only**. ARM64 manifests were inspected, not booted or functionally tested. Version hints below come from the captured source tag, OCI label or isolated runtime; the digest is authoritative.

| Service | Version or source hint | SHA256 index digest |
| --- | --- | --- |
| `sonarr` | 4.0.20.3014-ls1 | `48e36b608b461273b9dae857725cd0561a127f2c694b687318f6d0c5ce63e5c9` |
| `radarr` | 6.4.4.10685-ls323 | `fc0ae3c50f8128b97e84381903ec8d6845c3a569641cb7117c124376cccb57c1` |
| `prowlarr` | 2.6.5.5623-ls165 | `b3b00047ed20f6ce2b16780145cb6a0acb51c9e28ea6aa6f5dcd33c7034f1797` |
| `bazarr` | v1.6.2-ls369 | `946dc8c5bb5331418eac60050cf40a63cb8f66ac039841dda493870e3559799c` |
| `sabnzbd` | 5.1.3-ls278 | `3ac1c9ea03bafa2588dd8acdcb809857bae305e71d2d90e11d689c78076a9c01` |
| `jellyfin` | 12.3; API reported 12.3.0 | `2038fe038ab3876a80f6e7b7e0aef6607b032c7dc0c9b3ad3296d0b2eba3bbb5` |
| `seerr` | v3.5.1 | `d8fab675b55ad293ded98fd71efcfd46054cbad9746534ae1cc4794a88c0b3be` |
| `gluetun` | v3.41.3 | `fa19cc76b2af13d57a8d3dc3066f2ada061b1c761b8aecf989b3877c0486e027` |
| `qbittorrent` | 5.3.0_v2.1.2-ls484 | `e887804fa9de4c60f5f0fb1f3a7e4febe40a8f69fe736f70c4e8589b1f868dab` |
| `port-sync` | Python 3.13.17 / Alpine | `bf12ecc0bea3f171d55145dd02c78b5eeb09f30ee2ac9cc2b4c9e4e8f5e30741` |
| `dockge` | 1 source tag; no OCI version label | `335c6368b880ecc203236ed89e6e5232e0d6578e8ef5920e4a502390451502bf` |
| `uptime-kuma` | 2 source tag; no OCI version label | `4b95dbe7ac5343b768f8e04e3035ccd59997872b1a35ab9b0f082d2cc279070e` |
| `homarr` | latest source tag; OCI version main | `21f5ef759181f74c6ad2bd3dbf2d0eb3b8a502ba04980703a2a82945447f58f5` |
| `socket-proxy` | 3.4.7-r0-ls103 | `e5d819de59b343c01b2fe7adc7fad3c7a60e5dba08e45f1ac317352bc08599b5` |
| `immich-server` | v3.3.5 | `485d546a2e31d6a8b4db8423fcba5c109de8c218d93410af115fd1ccfc4352a6` |
| `immich-machine-learning` | v3.3.5 | `5b28c9352c8374b8a1ed96fbdd8d6755fea6e43510ec43fdf6538aa02ea40f56` |
| `database` | 14-vectorchord0.4.3-pgvector0.8.1-pgvectors0.2.0 | `bcf63357191b76a916ae5eb93464d65c07511da41e3bf7a8416db519b40b1c23` |
| `redis` | Valkey 9, selected by Immich v3.3.5 | `c123e3715db63d06d4ad6964884037aa0d5d4d703939b9929954112889708e1d` |
| `navidrome` | 0.64.2 | `38dc2727bfcfd5ede290f8ada114fc90368146f265ae4701ddddbcbe2a44ee52` |
| `slskd` | 0.26.2.0+e472ecd7 | `d0963a3e5b16facd295bf4f2156dad0182f246b1b21a032cae12f5494c694843` |
| `music-assistant` | 2.10.8 | `661e88bea76a9e79f38feca791f6c7373376ab60766ee01f56b66418ca243b68` |

The server and machine-learning images are Immich **v3.3.5**. Keep that pair and its PostgreSQL/VectorChord/pgvector/pgvectors and Valkey dependencies together when testing upgrades. The PostgreSQL image label is `14-vectorchord0.4.3-pgvector0.8.1-pgvectors0.2.0`.

## Updating deliberately

A digest fixes image identity; it does not automatically receive security updates. To update a service, resolve the intended upstream release to its index digest, review release and migration notes, test the new family in a disposable deployment, and change the catalog pin plus this record together. Then generate and review a fresh deployment plan, apply, and verify. Preserve application/database backups before a version change. Do not rewrite a site's image locks to conceal an untested upgrade.

Registry inspection is read-only. From a checkout on a host with Docker Buildx:

```bash
python3 - <<'PY'
import subprocess
from samurai_stack.catalog import IMAGES
for name, reference in IMAGES.items():
    print(name, reference, flush=True)
    subprocess.run([
        "docker", "buildx", "imagetools", "inspect", "--raw", reference
    ], check=True)
PY
```
