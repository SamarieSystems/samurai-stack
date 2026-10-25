"""Portable Compose catalog. Rendering never reads secrets or changes a host."""

from __future__ import annotations

from pathlib import PurePosixPath

from . import naming


# Immutable multi-platform indexes validated 2026-10-24. See docs/IMAGES.md.
IMAGES = {
    "sonarr": "lscr.io/linuxserver/sonarr@sha256:48e36b608b461273b9dae857725cd0561a127f2c694b687318f6d0c5ce63e5c9",
    "radarr": "lscr.io/linuxserver/radarr@sha256:fc0ae3c50f8128b97e84381903ec8d6845c3a569641cb7117c124376cccb57c1",
    "prowlarr": "lscr.io/linuxserver/prowlarr@sha256:b3b00047ed20f6ce2b16780145cb6a0acb51c9e28ea6aa6f5dcd33c7034f1797",
    "bazarr": "lscr.io/linuxserver/bazarr@sha256:946dc8c5bb5331418eac60050cf40a63cb8f66ac039841dda493870e3559799c",
    "sabnzbd": "lscr.io/linuxserver/sabnzbd@sha256:3ac1c9ea03bafa2588dd8acdcb809857bae305e71d2d90e11d689c78076a9c01",
    "jellyfin": "jellyfin/jellyfin@sha256:2038fe038ab3876a80f6e7b7e0aef6607b032c7dc0c9b3ad3296d0b2eba3bbb5",
    "seerr": "ghcr.io/seerr-team/seerr@sha256:d8fab675b55ad293ded98fd71efcfd46054cbad9746534ae1cc4794a88c0b3be",
    "gluetun": "qmcgaw/gluetun@sha256:fa19cc76b2af13d57a8d3dc3066f2ada061b1c761b8aecf989b3877c0486e027",
    "qbittorrent": "lscr.io/linuxserver/qbittorrent@sha256:e887804fa9de4c60f5f0fb1f3a7e4febe40a8f69fe736f70c4e8589b1f868dab",
    # Docker Official Image, Python 3.13 on Alpine.
    "port-sync": "python@sha256:bf12ecc0bea3f171d55145dd02c78b5eeb09f30ee2ac9cc2b4c9e4e8f5e30741",
    "dockge": "louislam/dockge@sha256:335c6368b880ecc203236ed89e6e5232e0d6578e8ef5920e4a502390451502bf",
    "uptime-kuma": "louislam/uptime-kuma@sha256:4b95dbe7ac5343b768f8e04e3035ccd59997872b1a35ab9b0f082d2cc279070e",
    "homarr": "ghcr.io/homarr-labs/homarr@sha256:21f5ef759181f74c6ad2bd3dbf2d0eb3b8a502ba04980703a2a82945447f58f5",
    "socket-proxy": "lscr.io/linuxserver/socket-proxy@sha256:e5d819de59b343c01b2fe7adc7fad3c7a60e5dba08e45f1ac317352bc08599b5",
    # Immich v3.3.5, server and ML pinned to the tested release together.
    "immich-server": "ghcr.io/immich-app/immich-server@sha256:485d546a2e31d6a8b4db8423fcba5c109de8c218d93410af115fd1ccfc4352a6",
    "immich-machine-learning": "ghcr.io/immich-app/immich-machine-learning@sha256:5b28c9352c8374b8a1ed96fbdd8d6755fea6e43510ec43fdf6538aa02ea40f56",
    # Immich v3.3.5: PostgreSQL 14 / VectorChord 0.4.3 / pgvectors 0.2.0.
    "database": "ghcr.io/immich-app/postgres@sha256:bcf63357191b76a916ae5eb93464d65c07511da41e3bf7a8416db519b40b1c23",
    # The Valkey 9 manifest selected by that same upstream release.
    "redis": "docker.io/valkey/valkey@sha256:c123e3715db63d06d4ad6964884037aa0d5d4d703939b9929954112889708e1d",
    "navidrome": "deluan/navidrome@sha256:38dc2727bfcfd5ede290f8ada114fc90368146f265ae4701ddddbcbe2a44ee52",
    "slskd": "slskd/slskd@sha256:d0963a3e5b16facd295bf4f2156dad0182f246b1b21a032cae12f5494c694843",
    "music-assistant": "ghcr.io/music-assistant/server@sha256:661e88bea76a9e79f38feca791f6c7373376ab60766ee01f56b66418ca243b68",
}

ROLE = {2: "media", 3: "torrent", 4: "management", 5: "photos", 6: "music"}
PORTS = {
    "sonarr": 8989, "radarr": 7878, "prowlarr": 9696,
    "bazarr": 6767, "sabnzbd": 8080, "jellyfin": 8096,
    "seerr": 5055, "qbittorrent": 8081, "dockge": 5001,
    "uptime-kuma": 3001, "homarr": 7575, "immich-server": 2283,
    "navidrome": 4533, "slskd": 5030, "music-assistant": 8095,
}


def _path(*parts: str) -> str:
    return str(PurePosixPath(*map(str, parts)))


def _root(config: dict) -> str:
    return config["install_root"]


def _data(config: dict) -> str:
    return config["storage"]["data_root"]


def _app(config: dict, name: str) -> str:
    return _path(_root(config), "appdata", name)


def _bind(source: str, target: str, *, readonly: bool = False) -> dict:
    return {"type": "bind", "source": source, "target": target,
            "read_only": readonly, "bind": {"create_host_path": False}}


def role_host(config: dict, stage: int) -> str:
    # Management controls this machine's Docker daemon, regardless of its role.
    if stage == 4:
        return config["bind_address"]
    return config.get("hosts", {}).get(ROLE[stage], config["bind_address"])


def _address(host: str) -> str:
    return "[" + host + "]" if ":" in host and not host.startswith("[") else host


def _port(config: dict, stage: int, port: int, *, internal: int | None = None,
          protocol: str = "tcp") -> str:
    return f"{_address(role_host(config, stage))}:{port}:{internal or port}/{protocol}"


def _env(config: dict) -> dict:
    return {"PUID": str(config["puid"]), "PGID": str(config["pgid"]),
            "TZ": config["timezone"], "UMASK": "002"}


def _base(config: dict, name: str, *, storage: bool = True) -> dict:
    return {"image": IMAGES[name], "restart": "no",
            "security_opt": ["no-new-privileges:true"],
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            "labels": {"io.samurai.deployment": config["deployment_name"]}}


def _standard(config: dict, name: str, port: int, *, data: bool = True) -> dict:
    s = _base(config, name, storage=data)
    s["environment"] = _env(config)
    s["volumes"] = [_bind(_app(config, name), "/config")]
    if data:
        s["volumes"].append(_bind(_data(config), "/data"))
    s["ports"] = [_port(config, 2, port)]
    return s


def _health(url: str, *, tool: str = "curl") -> dict:
    command = (["CMD", "curl", "--fail", "--silent", url] if tool == "curl"
               else ["CMD", "wget", "--quiet", "--tries=1", "--spider", url])
    return {"test": command, "interval": "30s", "timeout": "5s",
            "start_period": "60s", "retries": 5}


def _media(config: dict) -> dict:
    services = {name: _standard(config, name, PORTS[name], data=name != "prowlarr")
                for name in ["sonarr", "radarr", "prowlarr", "bazarr"]}
    if config.get("features", {}).get("usenet", True):
        services["sabnzbd"] = _standard(config, "sabnzbd", 8080)
    jellyfin = _base(config, "jellyfin")
    jellyfin.update({"user": f"{config['puid']}:{config['pgid']}",
                    "environment": {"TZ": config["timezone"]},
                    "ports": [_port(config, 2, 8096)],
                    "volumes": [_bind(_app(config, "jellyfin"), "/config"),
                                _bind(_app(config, "jellyfin-cache"), "/cache"),
                                _bind(_path(_data(config), "library"), "/data/library", readonly=True)]})
    acceleration = config.get("hardware", {}).get("acceleration", "cpu")
    if acceleration == "nvidia":
        jellyfin["deploy"] = {"resources": {"reservations": {"devices": [
            {"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}]}}}
        jellyfin["environment"].update({"NVIDIA_VISIBLE_DEVICES": "all",
                                       "NVIDIA_DRIVER_CAPABILITIES": "compute,video,utility"})
    elif acceleration in {"intel", "amd"}:
        jellyfin["devices"] = ["/dev/dri:/dev/dri"]
        groups = config.get("hardware", {}).get("render_groups", [])
        if groups:
            jellyfin["group_add"] = [str(g) for g in groups]
    seerr = _base(config, "seerr", storage=False)
    seerr.update({"init": True, "user": f"{config['puid']}:{config['pgid']}",
                  "cap_drop": ["ALL"], "environment": {"TZ": config["timezone"], "PORT": "5055"},
                  "ports": [_port(config, 2, 5055)],
                  "volumes": [_bind(_app(config, "seerr"), "/app/config")],
                  "healthcheck": _health("http://127.0.0.1:5055/api/v1/settings/public", tool="wget")})
    services.update({"jellyfin": jellyfin, "seerr": seerr})
    return services


def _torrent(config: dict) -> dict:
    if not config.get("features", {}).get("torrents", True):
        return {}
    vpn = config["vpn"]
    gluetun = _base(config, "gluetun", storage=False)
    env = {"TZ": config["timezone"], "VPN_SERVICE_PROVIDER": vpn["provider"],
           "VPN_TYPE": vpn["type"], "FIREWALL_INPUT_PORTS": "8081",
           "VPN_PORT_FORWARDING": "on" if vpn.get("port_forwarding") else "off"}
    if vpn.get("port_forwarding"):
        env["VPN_PORT_FORWARDING_STATUS_FILE"] = "/gluetun/forwarded_port"
        if vpn["provider"] == "protonvpn":
            env["PORT_FORWARD_ONLY"] = "on"
    gluetun.update({"cap_add": ["NET_ADMIN"], "devices": ["/dev/net/tun:/dev/net/tun"],
                    "environment": env, "volumes": [_bind(_app(config, "gluetun"), "/gluetun")],
                    "ports": [_port(config, 3, 8081)]})
    if vpn["type"] == "wireguard":
        if vpn["provider"] != "custom":
            env["WIREGUARD_PRIVATE_KEY_SECRETFILE"] = "/run/secrets/vpn_wireguard_private_key"
            gluetun["secrets"] = ["vpn_wireguard_private_key"]
            address = vpn.get("wireguard_addresses") or ("10.2.0.2/32" if vpn["provider"] == "protonvpn" else None)
            if vpn["provider"] == "mullvad" and not address:
                raise ValueError("Mullvad WireGuard requires its provider-issued address including prefix")
            if address:
                env["WIREGUARD_ADDRESSES"] = str(address)
        else:
            gluetun["volumes"].append(_bind(_path(_root(config), "secrets", "vpn_custom.conf"),
                                           "/gluetun/wireguard/wg0.conf", readonly=True))
    else:
        env["OPENVPN_USER_SECRETFILE"] = "/run/secrets/vpn_openvpn_username"
        env["OPENVPN_PASSWORD_SECRETFILE"] = "/run/secrets/vpn_openvpn_password"
        gluetun["secrets"] = ["vpn_openvpn_username", "vpn_openvpn_password"]
        if vpn["provider"] == "custom":
            env["OPENVPN_CUSTOM_CONFIG"] = "/gluetun/custom.conf"
            gluetun["volumes"].append(_bind(_path(_root(config), "secrets", "vpn_custom.conf"),
                                           "/gluetun/custom.conf", readonly=True))
    qbit = _base(config, "qbittorrent")
    qbit.update({"network_mode": "service:gluetun", "environment": {**_env(config), "WEBUI_PORT": "8081"},
                 "depends_on": {"gluetun": {"condition": "service_healthy"}},
                 "volumes": [_bind(_app(config, "qbittorrent"), "/config"),
                             _bind(_data(config), "/data")]})
    services = {"gluetun": gluetun, "qbittorrent": qbit}
    if vpn.get("port_forwarding"):
        watcher = _base(config, "port-sync", storage=False)
        watcher.update({"network_mode": "service:gluetun", "user": "0:0", "read_only": True,
                        "cap_drop": ["ALL"], "tmpfs": ["/tmp:rw,noexec,nosuid,size=8m"],
                        "secrets": ["qbit_username", "qbit_password"],
                        "command": ["python", "-u", "/app/port_sync.py"],
                        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
                        "healthcheck": {"test": ["CMD", "python", "/app/port_sync.py", "--healthcheck"],
                                        "interval": "20s", "timeout": "5s", "start_period": "60s", "retries": 3},
                        "depends_on": {"gluetun": {"condition": "service_healthy"},
                                       "qbittorrent": {"condition": "service_started"}},
                        "volumes": [_bind(_path(_root(config), "port_sync.py"), "/app/port_sync.py", readonly=True),
                                    _bind(_app(config, "gluetun"), "/gluetun", readonly=True)]})
        services["port-sync"] = watcher
    return services


def _management(config: dict) -> dict:
    services = {}
    dockge = _base(config, "dockge", storage=False)
    dockge.update({"environment": {"DOCKGE_STACKS_DIR": _path(_root(config), "stages")},
                   "ports": [_port(config, 4, 5001)],
                   "volumes": [_bind(_app(config, "dockge"), "/app/data"),
                               _bind(_path(_root(config), "stages"), _path(_root(config), "stages")),
                               _bind("/var/run/docker.sock", "/var/run/docker.sock")]})
    kuma = _base(config, "uptime-kuma", storage=False)
    kuma.update({"ports": [_port(config, 4, 3001)],
                 "volumes": [_bind(_app(config, "uptime-kuma"), "/app/data")]})
    services.update({"dockge": dockge, "uptime-kuma": kuma})
    if config.get("features", {}).get("dashboard", False):
        proxy = _base(config, "socket-proxy", storage=False)
        proxy.update({"environment": {"CONTAINERS": "1", "INFO": "1", "PING": "1",
                                      "VERSION": "1", "EVENTS": "1", "POST": "0"},
                      "networks": ["socket-proxy"],
                      "volumes": [_bind("/var/run/docker.sock", "/var/run/docker.sock", readonly=True)]})
        homarr = _base(config, "homarr", storage=False)
        homarr.update({"ports": [_port(config, 4, 7575)],
                       "volumes": [_bind(_app(config, "homarr"), "/appdata")],
                       "networks": ["default", "socket-proxy"],
                       "environment": {"TZ": config["timezone"], "DOCKER_HOSTNAMES": "socket-proxy",
                                       "DOCKER_PORTS": "2375"},
                       "depends_on": {"socket-proxy": {"condition": "service_started"}}})
        services.update({"homarr": homarr, "socket-proxy": proxy})
    return services


def _photos(config: dict) -> dict:
    if not config.get("features", {}).get("photos", False):
        return {}
    server = _base(config, "immich-server")
    server.update({"user": f"{config['puid']}:{config['pgid']}", "ports": [_port(config, 5, 2283)],
                   "environment": {"TZ": config["timezone"], "DB_HOSTNAME": "database",
                                   "DB_USERNAME": "postgres", "DB_DATABASE_NAME": "immich",
                                   "DB_PASSWORD_FILE": "/run/secrets/immich_db_password",
                                   "REDIS_HOSTNAME": "redis"},
                   "secrets": ["immich_db_password"],
                   "depends_on": {"database": {"condition": "service_healthy"},
                                  "redis": {"condition": "service_healthy"}},
                   "volumes": [_bind(_path(_data(config), "photos", "upload"), "/data"),
                               _bind(_path(_data(config), "photos", "external"), "/external", readonly=True)]})
    ml = _base(config, "immich-machine-learning", storage=False)
    ml["user"] = f"{config['puid']}:{config['pgid']}"
    ml["volumes"] = [_bind(_app(config, "immich-machine-learning"), "/cache")]
    db = _base(config, "database", storage=False)
    db.update({"environment": {"POSTGRES_USER": "postgres", "POSTGRES_DB": "immich",
                               "POSTGRES_PASSWORD_FILE": "/run/secrets/immich_db_password",
                               "POSTGRES_INITDB_ARGS": "--data-checksums"},
               "secrets": ["immich_db_password"], "shm_size": "128mb",
               "volumes": [_bind(_app(config, "immich-postgres"), "/var/lib/postgresql/data")],
               "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U postgres -d immich"],
                               "interval": "15s", "timeout": "5s", "retries": 10}})
    redis = _base(config, "redis", storage=False)
    redis.update({"volumes": [_bind(_app(config, "immich-redis"), "/data")],
                  "healthcheck": {"test": ["CMD", "redis-cli", "ping"],
                                  "interval": "15s", "timeout": "5s", "retries": 10}})
    return {"immich-server": server, "immich-machine-learning": ml, "database": db, "redis": redis}


def _music(config: dict) -> dict:
    if not config.get("features", {}).get("music", False):
        return {}
    navidrome = _base(config, "navidrome")
    navidrome.update({"user": f"{config['puid']}:{config['pgid']}",
                      "ports": [_port(config, 6, 4533)],
                      "environment": {"TZ": config["timezone"], "ND_SCANNER_SCHEDULE": "@every 5m"},
                      "volumes": [_bind(_app(config, "navidrome"), "/data"),
                                  _bind(_path(_data(config), "library", "music"), "/music", readonly=True)]})
    slskd = _base(config, "slskd")
    slskd.update({"user": f"{config['puid']}:{config['pgid']}",
                 "ports": [_port(config, 6, 5030), _port(config, 6, 50300)],
                 "environment": {"TZ": config["timezone"], "SLSKD_UMASK": "002",
                                 "SLSKD_REMOTE_CONFIGURATION": "false",
                                 "SLSKD_DOWNLOADS_DIR": "/data/downloads/music/complete",
                                 "SLSKD_INCOMPLETE_DIR": "/data/downloads/music/incomplete",
                                 "SLSKD_SHARED_DIR": "/music"},
                 "volumes": [_bind(_app(config, "slskd"), "/app"),
                             _bind(_path(_data(config), "downloads", "music"), "/data/downloads/music"),
                             _bind(_path(_data(config), "library", "music"), "/music", readonly=True)]})
    services = {"navidrome": navidrome, "slskd": slskd}
    if config.get("features", {}).get("music_assistant", False):
        ma = _base(config, "music-assistant")
        ma.update({"network_mode": "host", "environment": {"TZ": config["timezone"]},
                   "volumes": [_bind(_app(config, "music-assistant"), "/data"),
                               _bind(_path(_data(config), "library", "music"), "/media", readonly=True)]})
        services["music-assistant"] = ma
    return services


def environment_secrets(config: dict, stage: int) -> dict[str, dict[str, str]]:
    """Private env-file values required by images without native file options."""
    if stage == 4 and config.get("features", {}).get("dashboard", False):
        return {"homarr": {"SECRET_ENCRYPTION_KEY": "homarr_encryption_key"}}  # release-audit: reference - image variable to local secret filename
    if stage == 6 and config.get("features", {}).get("music", False):
        return {"slskd": {"SLSKD_USERNAME": "slskd_username", "SLSKD_PASSWORD": "slskd_password",  # release-audit: reference - image variable to local secret filename
                          "SLSKD_API_KEY": "slskd_api_key", "SLSKD_SLSK_USERNAME": "soulseek_username",  # release-audit: reference - image variable to local secret filename
                          "SLSKD_SLSK_PASSWORD": "soulseek_password"}}  # release-audit: reference - image variable to local secret filename
    return {}


def render_stage(config: dict, stage: int) -> dict:
    renderers = {2: _media, 3: _torrent, 4: _management, 5: _photos, 6: _music}
    if stage not in renderers:
        raise ValueError("Compose stages are 2 through 6")
    services = renderers[stage](config)
    project = naming.project_name(config, stage)
    result = {"name": project, "services": services}
    for service in environment_secrets(config, stage):
        services[service]["env_file"] = [_path(_root(config), "stages", project, service + ".env")]
    secrets = {name for service in services.values() for name in service.get("secrets", [])}
    if secrets:
        result["secrets"] = {name: {"file": _path(_root(config), "secrets", name)} for name in sorted(secrets)}
    if stage == 4 and "homarr" in services:
        result["networks"] = {"default": {}, "socket-proxy": {"internal": True}}
    return result


def expected_secret_names(config: dict, stage: int) -> list[str]:
    result = set(render_stage(config, stage).get("secrets", {}))
    for fields in environment_secrets(config, stage).values():
        result.update(fields.values())
    if stage == 3 and config.get("features", {}).get("torrents", True):
        result.update({"qbit_username", "qbit_password"})
        if config["vpn"]["provider"] == "custom":
            result.add("vpn_custom.conf")
    return sorted(result)


def stage_directories(config: dict, stage: int) -> list[str]:
    """Only new deployment-owned app directories, never device/socket paths."""
    apps = {2: ["sonarr", "radarr", "prowlarr", "bazarr", "jellyfin", "jellyfin-cache", "seerr"],
            3: ["gluetun", "qbittorrent"], 4: ["dockge", "uptime-kuma"],
            5: ["immich-postgres", "immich-redis", "immich-machine-learning"],
            6: ["navidrome", "slskd"]}
    if stage not in apps:
        raise ValueError("Compose stages are 2 through 6")
    services = render_stage(config, stage)["services"]
    if not services:
        return []
    names = apps[stage][:]
    if stage == 2 and "sabnzbd" in services:
        names.append("sabnzbd")
    if stage == 4 and "homarr" in services:
        names.append("homarr")
    if stage == 6 and "music-assistant" in services:
        names.append("music-assistant")
    paths = [_app(config, name) for name in names]
    if stage == 3 and config["vpn"]["provider"] == "custom" and config["vpn"]["type"] == "wireguard":
        paths.append(_path(_app(config, "gluetun"), "wireguard"))
    return sorted(paths)


def stage_endpoints(config: dict, stage: int) -> dict[str, str]:
    host = _address(role_host(config, stage))
    return {name: f"http://{host}:{PORTS[name]}" for name in render_stage(config, stage)["services"] if name in PORTS}
