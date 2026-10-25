#!/usr/bin/env bash
# Shared source/portable installer preflight. This file defines functions only.
# The portable builder embeds these exact bytes, not a second implementation.

samurai_setup_help() {
  local command_name="${1:-./setup}"
  printf '%s\n' 'Samurai Stack guided installer' \
    "Run $command_name for the menu, or $command_name NUMBER to resume one step." \
    '1 Prepare Debian   2 Connect storage   3 Media applications' \
    '4 VPN and torrents 5 Management        6 Optional extras' \
    "Maintenance: $command_name --hardlinks checks/enables hardlinks without redeploying." \
    "Media quality: $command_name --quality-profiles reviews optional HD/original-audio and size policy without importing media." \
    "New blank setup after an intentional app wipe: $command_name --fresh (keeps existing media)." \
    "Recovery: $command_name --recover reports existing containers, mounts and saved configuration paths." \
    "Empty setup storage correction: $command_name --config site.json --choose-storage" \
    'Keep the installer file. Progress and private configuration live outside the download folder.'
}

samurai_python_ready() {
  python3 -I -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null
}

samurai_guest_preflight() {
  local launcher="$1" argument read_only=0 setup_python_answer setup_python_plan
  shift
  for argument in "$@"; do
    if [[ "$argument" == --recover ]]; then read_only=1; fi
  done
  if [[ "$read_only" -eq 0 && ( ! -t 0 || ! -t 1 ) ]]; then
    printf '%s\n' 'Open an interactive terminal and run the saved installer there. Do not pipe a downloaded script into a shell.' >&2
    exit 1
  fi
  if [[ "${EUID}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      # Downloads and source ZIPs may not retain an executable bit. Use Bash
      # explicitly, preserving the original arguments and working directory.
      exec sudo -- bash "$launcher" "$@"
    fi
    printf '%s\n' 'Administrator access is needed. Run su -, return to this directory, then run the saved installer.' >&2
    exit 1
  fi
  if [[ -d /etc/pve || "$(uname -s)" != Linux ]]; then
    printf '%s\n' 'Run this installer inside the new Debian/Ubuntu VM, not on Proxmox itself.' >&2
    exit 1
  fi
  if [[ "$(cat /proc/1/comm)" != systemd ]] || \
     ! systemd-detect-virt --vm 2>/dev/null | grep -Eq '^(kvm|qemu)$'; then
    printf '%s\n' 'This installer runs inside a KVM/QEMU Linux VM booted with systemd.' >&2
    exit 1
  fi
  if samurai_python_ready; then return 0; fi
  if [[ "$read_only" -eq 1 ]]; then
    printf '%s\n' 'Read-only recovery needs Python 3.11+. No packages were changed. Run the saved installer interactively to prepare Python first.' >&2
    exit 2
  fi
  if [[ ! -e /dev/virtio-ports/org.qemu.guest_agent.0 ]]; then
    printf '%s\n' 'Preparation is waiting for the QEMU Guest Agent channel. No packages changed.' \
      'In Proxmox: VM > Options > QEMU Guest Agent > Enable. Shut this VM down cleanly, then Start it.' \
      'Run the saved installer again after startup.' >&2
    exit 2
  fi
  # Only these supported distributions supply the required Python directly.
  # shellcheck source=/etc/os-release
  source /etc/os-release
  case "${ID:-}:${VERSION_ID:-}" in
    debian:12|debian:13|ubuntu:24.04|ubuntu:26.04) ;;
    *) printf '%s\n' 'Use Debian 12/13 or Ubuntu 24.04/26.04 for this installer.' >&2; exit 1 ;;
  esac
  printf '%s\n' 'Python 3.11+ is missing. Install Python and CA certificates from this VM OS repository?'
  read -r -p '[y/N] ' setup_python_answer
  case "$setup_python_answer" in
    y|Y|yes|YES)
      if ! apt-get update; then
        printf '%s\n' 'APT could not refresh this VM OS repositories. No Python installation was run.' \
          'Review the APT error above, including any enabled installation CD-ROM source, then retry this installer.' >&2
        exit 1
      fi
      setup_python_plan="$(apt-get --simulate --no-remove --no-upgrade --no-install-recommends install python3 ca-certificates)"
      if printf '%s\n' "$setup_python_plan" | grep -Eq '^(Remv[[:space:]]|Inst[[:space:]]+[^[:space:]]+[[:space:]]+\[)'; then
        printf '%s\n' 'Python prerequisites would upgrade or remove existing packages. No installation was performed; resolve this dependency conflict deliberately.' >&2
        exit 1
      fi
      apt-get --yes --no-remove --no-upgrade --no-install-recommends install python3 ca-certificates
      if ! samurai_python_ready; then
        printf '%s\n' 'Python 3.11+ is still unavailable after preparation. No installation step was started.' >&2
        exit 1
      fi ;;
    *) printf '%s\n' 'No packages changed. Run the saved installer again when ready.'; exit 2 ;;
  esac
}
