# Plain-language glossary

Use this as a lookup sheet. You do not have to memorize it before using the server.

| Term | What it means here |
| --- | --- |
| Application / app | One service, such as Jellyfin or Sonarr. Several apps cooperate to deliver one request. |
| Server | The machine providing the services. People sometimes use this word for the physical machine, its VM, or an app; name the layer when troubleshooting. |
| Hypervisor / Proxmox | The physical machine's virtualization system. It runs the application VM. It is not the place to run the media installer. |
| VM / guest | A virtual computer with its own Linux operating system. The media apps run here. |
| Host | Context matters: the physical Proxmox host, or the Linux Docker host inside the VM. Say which one you mean. |
| Container | The running environment for one application. It is replaceable; its persistent mounted data is the part you must preserve. |
| Image | The packaged application used to create a container. Changing an image can also change the application's database format. |
| Pinned image | An exact recorded image version/digest. Retrying setup keeps the pinned image instead of silently choosing the newest release. |
| Compose file | The recipe describing a group of containers, their ports and their mounted folders. Usually named `compose.yaml`. |
| Compose project / stack | A named group of related containers. Dockge displays these groups. It is not a physical directory containing every downloaded file. |
| systemd unit | Linux's controller for a background service. Generated Samurai stage units run the startup guards and supervise whole Compose groups. |
| Guided step / engine stage | Two numbering systems. Use the setup menu's steps 1-6 for the normal installer. Advanced engine IDs are documented separately. |
| Appdata | Application databases, settings, accounts and state. Losing it can lose users and watch history even when the movie files survive. |
| Data root | The chosen parent directory for downloads and libraries on the actual media filesystem. Containers normally see it as `/data`. |
| Mount / mountpoint | Connecting a filesystem at a folder such as `/mnt/media`. An existing empty folder does not prove the intended disk is mounted there. |
| NAS / NFS | A network storage server, and a supported way of exporting its folders to the Linux guest. A local HDD does not require a NAS. |
| Filesystem UUID | A persistent identifier for a filesystem. `/dev/sda` and `/dev/sdb` can change order across reboots; UUIDs identify the intended filesystem. |
| Hardlink | A second filename pointing to the same underlying file on one filesystem. A 5 GiB download and its 5 GiB library hardlink share those 5 GiB instead of using 10 GiB. Both names must be removed before those data blocks are freed. Editing the shared file changes what both names see. |
| Import | Sonarr/Radarr accepting a completed file into its managed library, usually with configured naming. Download completion alone is not import completion. |
| Monitor / monitored title | In Sonarr/Radarr, whether a title or episode is eligible for future acquisition according to its settings. In Kuma, a periodic service check. These meanings are different. |
| Quality profile | Which release qualities are accepted and where upgrades should stop. Resolution alone does not describe source quality, audio or file size. |
| Indexer / search source | A catalog used to find releases. An indexer supplies search results; it is not the Usenet server providing the payload. |
| Usenet / NNTP | A download method using a Usenet provider. SABnzbd needs the provider's NNTP server and service credentials. |
| NZB | A job description SABnzbd uses to retrieve a Usenet release. It is not the actual movie file. |
| Torrent / magnet | Metadata identifying a peer-to-peer download. A magnet may need to retrieve file metadata before it can show a meaningful size. |
| Seeder / peer | Participants in a torrent swarm. A completed torrent uploads only when reachable peers request pieces it has. |
| Tracker / DHT | Ways torrent clients discover other participants. Their status is useful evidence, not a guarantee that a particular swarm has useful peers. |
| VPN / Gluetun | The tunnel and container boundary protecting torrent network traffic. qBittorrent's torrent interface stays on `tun0`. |
| Tailscale / tailnet | Private remote access between allowed devices and users. This is separate from the torrent VPN provider. |
| LAN / WAN | Your local home network, and the wider internet. A LAN address normally works only from that network or a configured private route. |
| URL / port | An app's address, for example `http://server-address:8096`. The port selects the service. A different port is a different listener, not a password. |
| API key | A credential applications use to talk to each other. Keep it private just like a password. |
| Direct Play | The player can use the media as stored. This generally needs much less server processing than video transcoding. |
| Transcoding | Converting media for a player while it runs. Video conversion, especially subtitle burn-in, can overload a small CPU. |
| Embedded / sidecar subtitles | Tracks inside the video file, or separate subtitle files next to it. Bazarr provider access does not guarantee either is available. |
| Free-space reserve | A threshold intended to pause or refuse some work before a disk fills. It is not a hard storage quota. |
| Snapshot / backup | A point-in-time state, or a separate recoverable copy. A VM snapshot, RAID and a copied `site.json` each have limits; none alone proves your media and databases can be restored. |
| SMART | Drive health attributes and self-test results. A passing snapshot does not guarantee a drive will never fail. |
| Health check | A container's own check. It is separate from reaching its web page, signing in, downloading or actually playing a title. |
| IOMMU / GPU passthrough | Hardware isolation and VM configuration needed to assign supported physical devices to a guest. Optional; not needed to run the CPU-only core. |

[Guide home](README.md) | [Start here](START-HERE.md) | [Troubleshooting](TROUBLESHOOTING.md)
