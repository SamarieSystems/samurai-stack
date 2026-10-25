# Private remote access

Use a private Tailscale connection and ordinary OpenSSH for remote administration.
The SSH connection can forward browser ports to the VM's existing LAN listeners.
This does not require public ports, an exit node or advertising an entire LAN.

## Confirm the correct machine

Install and sign in to Tailscale on the VM and your remote computer, with tailnet
policy allowing the intended connection. In the Linux VM, run:

```sh
hostname
tailscale ip -4
tailscale status
```

Match that address to the VM you intend to administer. Each machine has a
different Tailscale address. A reachable address belonging to another machine
does not reach this VM's applications.

On Windows, substitute the VM's actual Tailscale address below:

```powershell
$VmTailIp = '100.100.100.100'
tailscale ping --c=3 --timeout=5s $VmTailIp
Test-NetConnection $VmTailIp -Port 22
```

A successful Tailscale ping proves the peer path. An open TCP port 22 proves
the SSH listener is reachable. SSH still needs a permitted account and working
password or key authentication. Tailscale's separate SSH feature is optional;
do not enable it to repair an ordinary OpenSSH authentication failure.

If ping is relayed, use `tailscale netcheck` to inspect UDP/NAT behavior. A direct
connection is preferable for throughput. A fast direct ping followed by a web
timeout points toward service bindings, firewall or application state, rather
than internet bandwidth.

## Open a private browser connection

Samurai publishes application ports on `bind_address`, usually the VM's LAN
address. Its separate Tailscale address will not automatically accept those
ports. Keep the current private bindings and forward through SSH.

In PowerShell, replace the account, VM Tailscale address and VM LAN address:

```powershell
$VmTailIp = '100.100.100.100'
$VmLanIp = '192.168.1.50'
$VmAdmin = 'your-admin-user'
ssh -N -o ExitOnForwardFailure=yes -L "127.0.0.1:18096:${VmLanIp}:8096" "${VmAdmin}@${VmTailIp}"
```

Keep that terminal open. Open `http://127.0.0.1:18096` for Jellyfin. Port 18096
on your own computer is forwarded over SSH to port 8096 at the VM's LAN address.
Use your normal Jellyfin account. If your key is not a default SSH key, add
`-i C:\path\to\your-private-key` to the SSH command; keep the private key local.

Other applications work the same way. Add another `-L` argument to the same SSH
command for each port you need:

| Application | Local forwarding argument | Open locally |
| --- | --- | --- |
| Dockge | `-L "127.0.0.1:15001:${VmLanIp}:5001"` | `http://127.0.0.1:15001` |
| Sonarr | `-L "127.0.0.1:18989:${VmLanIp}:8989"` | `http://127.0.0.1:18989` |
| Radarr | `-L "127.0.0.1:17878:${VmLanIp}:7878"` | `http://127.0.0.1:17878` |

Use the actual ports printed by setup if customized. The SSH server must allow
TCP forwarding and reach its own LAN listeners. A successful SSH login with a
refused browser connection still requires checking the application listener:

```sh
ss -lnt
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Inspect the relevant generated systemd unit and its journal if the application
is stopped. Preserve storage and VPN checks while repairing it. Do not change
every application binding to `0.0.0.0` as a connectivity shortcut.

## Which warnings matter here?

`tailscale set --accept-routes` is needed only when this client is meant to
consume another machine's advertised subnet routes. Direct access to a peer's
Tailscale address does not need it. The bare command `tailscale --accept-routes`
is invalid because the option belongs to a subcommand.

Linux IP-forwarding and UDP GRO recommendations concern subnet routers and exit
nodes. They do not explain a LAN-bound Docker application refusing connections
on an ordinary node's Tailscale address. Do not advertise routes or enable an
exit node or Funnel for this workflow.

References: [Tailscale CLI](https://tailscale.com/docs/reference/tailscale-cli),
[connection types](https://tailscale.com/docs/reference/connection-types),
[ordinary SSH](https://tailscale.com/docs/reference/ssh-over-tailscale),
[subnet routers](https://tailscale.com/docs/features/subnet-routers).
