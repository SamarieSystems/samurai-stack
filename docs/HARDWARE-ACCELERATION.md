# Jellyfin hardware acceleration

An integrated GPU can accelerate Jellyfin. It must be available inside the
machine running Jellyfin, then explicitly exposed to its container. A GPU shown
on the Proxmox host is not automatically available inside a VM.

CPU mode remains supported. Hardware acceleration is optional; the installer
does not change firmware, boot arguments, PCI driver ownership or VM hardware.

## Proxmox: prove the prerequisites first

Run these read-only commands in the **Proxmox host shell**, not the Debian VM:

```sh
lspci -nnk
find /sys/kernel/iommu_groups -mindepth 1 -maxdepth 1 -type d
ls /sys/firmware/acpi/tables/IVRS /sys/firmware/acpi/tables/DMAR 2>/dev/null
```

For an AMD host, IVRS describes the firmware's IOMMU topology. For Intel, the
equivalent table is DMAR. An empty group list means there is no usable group
isolation yet. Having AMD-V/SVM or Intel VT-x enabled for virtual machines does
not by itself prove that PCI passthrough is ready.

If the firmware has not exposed the IOMMU, enable its IOMMU/AMD-Vi or VT-d setting
using the exact motherboard manual. This requires a host reboot and interrupts
all its guests. Keep console access and record the original setting. Do not
change storage-controller mode, reset firmware defaults, or install a BIOS
update merely to try this setting.

For example, ASRock's B450 Gaming-ITX/ac manual places IOMMU under Advanced,
AMD CBS, NBIO Common Options, NB Configuration; that manual lists it as disabled
by default. Menus can differ by firmware revision. This is an example for that
board, not a universal AMD menu path.

After the reboot, inspect the actual group containing the GPU and its related
functions before assigning anything. Do not apply an ACS override or pass a
storage/USB controller simply because it shares an APU's device prefix.
Proxmox PCI passthrough gives the selected device to one VM; the host cannot
continue using that same device normally. A host with only an iGPU can lose its
local graphical console when the guest owns it. Preserve remote administration
and a recovery plan before changing ownership.

Current Proxmox documentation says AMD IOMMU support is enabled automatically
by the kernel. Do not add copied kernel parameters until the actual firmware,
kernel and group evidence show they are needed.

## Debian guest and container

After a reviewed passthrough configuration, inspect **inside the Debian VM**:

```sh
lspci -nnk
ls -l /dev/dri
```

For Linux Intel/AMD acceleration, expect a usable render node such as
`/dev/dri/renderD128` belonging to the intended GPU. A virtual display's `card0`
alone is not proof of an accessible video encoder. The VM must have the proper
kernel driver and firmware, and the Jellyfin container user must have access to
the render device.

Use the site's supported `hardware.acceleration` selection (`intel` or `amd`)
and its actual render/video group IDs, then review the changed media-stage plan.
Keep the existing application state and media mounts. In Jellyfin, select the
supported VA-API/QSV method and only codecs the actual device supports. Confirm
a real forced video transcode in Jellyfin's playback information and FFmpeg
log. Direct Play or an audio-only conversion does not exercise GPU video
encoding. A healthy container alone is not this test.

Container/LXC render-device sharing is a different architecture from assigning
the whole GPU to a VM. Do not migrate an existing VM deployment to LXC as a
side effect of trying to enable acceleration.

## References

- [Proxmox PCI passthrough documentation](https://github.com/proxmox/pve-docs/blob/master/qm-pci-passthrough.adoc)
- [Jellyfin AMD acceleration](https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/amd/)
- [ASRock B450 Gaming-ITX/ac manual, printed page 68](https://download.asrock.com/Manual/Fatal1ty%20B450%20Gaming-ITXac.pdf)
