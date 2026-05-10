#!/bin/bash
# ============================================================================
# SMART Sniffer Agent — Unified Installer (Linux + macOS)
#
# One-liner install:
#   curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo bash
#
# Or pin a specific version:
#   VERSION=0.1.0 curl -sSL ... | sudo bash
#
# Uninstall:
#   curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo UNINSTALL=1 bash
#   (or if already downloaded: sudo bash install.sh --uninstall)
#
# What this script does:
#   1. Detects OS (Linux/macOS) and architecture (amd64/arm64)
#   2. Downloads the correct binary from the latest GitHub Release
#   3. Verifies the download against SHA256 checksums
#   4. Installs smartmontools if missing
#   5. Prompts for port, token, and scan interval
#   6. Installs the binary, config, and system service
#   7. Starts the agent and verifies it's running
# ============================================================================
set -e

REPO="DAB-LABS/smart-sniffer"
BINARY_NAME="smartha-agent"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BOLD}  --> $*${NC}"; }
success() { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ⚠ $*${NC}"; }
fail()    { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

# ---------------------------------------------------------------------------
# Disk usage picker — detects real block-device mounts, shows numbered list,
# user enters comma-separated numbers, "all", or "none".
# Sets FS_YAML with the config.yaml entries and FS_DISPLAY with mount list.
# ---------------------------------------------------------------------------
FS_YAML=""
FS_DISPLAY=""

# Unescape kernel-style mountinfo path escapes (\040 \011 \012 \134).
# Per fs/proc_namespace.c the kernel escapes space, tab, newline, and
# backslash in path fields. Decoder must process \134 (backslash)
# LAST so that literal backslashes do not re-trigger other rules.
# Implementation: route \134 through a sentinel that cannot collide
# with valid path content, decode the other three, then resolve the
# sentinel to a real backslash.
unescape_mountinfo_path() {
  local s="$1"
  s="${s//\\134/__SMARTSNIFFER_BS__}"
  s="${s//\\040/ }"
  s="${s//\\011/	}"
  # Newline replacement uses ANSI-C $'...' for the literal newline char.
  s="${s//\\012/$'\n'}"
  s="${s//__SMARTSNIFFER_BS__/\\}"
  printf '%s' "$s"
}

# Phase 2: btrfs picker display fallback.
#
# `df` returns total=0 for btrfs in some contexts (kernel statvfs
# undercount on multi-device btrfs / specific kernel versions). Shell
# out to `btrfs filesystem usage --raw <mp>` for accurate values.
#
# Output on stdout (success): "<total>\t<used>" as raw bytes.
# Output on failure (any reason): empty stdout, non-zero exit.
#
# Failure causes (all caller-visible): btrfs binary missing, subprocess
# timed out (5s), or output couldn't be parsed. Caller distinguishes
# "btrfs binary missing" via the cached BTRFS_BIN var, so this helper
# stays simple.
btrfs_usage_for() {
  local mp="$1"
  local out total used
  if command -v timeout >/dev/null 2>&1; then
    out=$(timeout 5s btrfs filesystem usage --raw "$mp" 2>/dev/null) || return 1
  else
    out=$(btrfs filesystem usage --raw "$mp" 2>/dev/null) || return 1
  fi
  # Match the leading "Device size:" line (in the Overall: block).
  total=$(echo "$out" | awk '/^[[:space:]]*Device size:/ {print $3; exit}')
  # Match the bare "Used:" line in Overall: -- NOT the per-block-group
  # "Used:" fields that appear inline like "Data,single: Size:N, Used:N".
  used=$(echo "$out" | awk '/^[[:space:]]*Used:[[:space:]]+[0-9]+[[:space:]]*$/ {print $2; exit}')
  [ -z "$total" ] || [ -z "$used" ] && return 1
  [[ ! "$total" =~ ^[0-9]+$ ]] && return 1
  [[ ! "$used" =~ ^[0-9]+$ ]] && return 1
  printf '%s\t%s\n' "$total" "$used"
}

pick_filesystems() {
  FS_YAML=""
  FS_DISPLAY=""

  # Parallel arrays for detected filesystems.
  local -a fs_mps=()
  local -a fs_devs=()
  local -a fs_types=()
  local -a fs_roots=()
  local -a fs_uuids=()
  local -a fs_labels=()

  # ---------------------------------------------------------------
  # Source selection: mountinfo on Linux (Phase 1B-1), mount on macOS.
  #
  # /proc/self/mountinfo gives us the `root` field, which lets us
  # dedup bind mounts via the strict (source, fstype, root) key.
  # /proc/mounts lacks `root`, so it cannot tell duplicate mounts
  # of the same filesystem apart from bind mounts of subdirs.
  #
  # Fallback chain on Linux:
  #   1. /proc/self/mountinfo  (preferred -- enables dedup)
  #   2. /proc/mounts          (fallback -- duplicates may slip through)
  #   3. mount                 (last resort, parses mount-output format)
  # ---------------------------------------------------------------
  local mounts mount_source=""
  if [[ "$OSTYPE" == darwin* ]]; then
    mounts=$(mount)
    mount_source="mount"
  elif [ -r /proc/self/mountinfo ]; then
    mounts=$(cat /proc/self/mountinfo)
    mount_source="mountinfo"
  elif [ -f /proc/mounts ]; then
    mounts=$(cat /proc/mounts)
    mount_source="proc_mounts"
  else
    mounts=$(mount)
    mount_source="mount"
  fi

  # Phase 2: cache btrfs binary availability once. Used by the per-mount
  # fallback when df returns zero on btrfs filesystems. We track whether
  # any btrfs mount was encountered so we can warn the user once if the
  # binary is missing -- one warning per picker run, not per entry.
  local btrfs_bin=""
  command -v btrfs >/dev/null 2>&1 && btrfs_bin=$(command -v btrfs)
  local btrfs_seen_missing_progs=0

  while IFS= read -r line; do
    local dev mp fstype root="/"

    # macOS mount output: /dev/disk3s1s1 on / (apfs, sealed, local, ...)
    # Linux /proc/self/mountinfo:
    #   36 35 98:0 /mnt1 /mnt2 rw,noatime master:1 - ext3 /dev/root rw,errors=continue
    #   ^id par maj  ^root ^mp  ^opts     opt-fields  - ^fstype ^source super-opts
    # Linux /proc/mounts: /dev/sda1 / ext4 rw,relatime 0 0
    if [[ "$OSTYPE" == darwin* ]]; then
      dev=$(echo "$line" | awk '{print $1}')
      mp=$(echo "$line" | sed 's/.* on \(.*\) (.*/\1/' | sed 's/ *$//')
      fstype=$(echo "$line" | sed 's/.*(\([^,)]*\).*/\1/' | sed 's/ *$//')
    elif [ "$mount_source" = "mountinfo" ]; then
      # mountinfo: fields before `-` separator vary in count (optional
      # fields). Split on " - " first, then extract.
      # Pre-`-`: 1=id 2=parent 3=major:minor 4=root 5=mp 6=opts ...
      # Post-`-`: fstype source super_opts
      local pre post
      pre="${line% - *}"
      post="${line#* - }"
      # Skip if separator not found (malformed line).
      [ "$pre" = "$line" ] && continue
      root=$(echo "$pre" | awk '{print $4}')
      mp=$(echo "$pre" | awk '{print $5}')
      fstype=$(echo "$post" | awk '{print $1}')
      dev=$(echo "$post" | awk '{print $2}')
      # Phase 1B-2: unescape kernel path escapes. Apply to mp, root,
      # and dev. fstype never contains escapes per kernel, but the
      # path fields routinely do (any whitespace in mount paths or
      # device names becomes \040, etc).
      mp="$(unescape_mountinfo_path "$mp")"
      root="$(unescape_mountinfo_path "$root")"
      dev="$(unescape_mountinfo_path "$dev")"
    else
      dev=$(echo "$line" | awk '{print $1}')
      mp=$(echo "$line" | awk '{print $2}')
      fstype=$(echo "$line" | awk '{print $3}')
      # /proc/mounts lacks `root`. Default to "/" so dedup degrades
      # to (dev, fstype) -- correct for partition mounts, may
      # collapse subdir bind mounts incorrectly. Best effort fallback.
      root="/"
      # /proc/mounts uses the same kernel escape rules as mountinfo,
      # so unescape paths and source here too.
      mp="$(unescape_mountinfo_path "$mp")"
      dev="$(unescape_mountinfo_path "$dev")"
    fi

    # Filter to real block devices and common filesystems.
    case "$dev" in
      /dev/sd*|/dev/nvme*|/dev/md*|/dev/mapper/*|/dev/vd*|/dev/xvd*|/dev/hd*|/dev/disk*) ;;
      *) case "$fstype" in zfs) ;; *) continue ;; esac ;;
    esac

    # Skip virtual/special filesystems.
    case "$fstype" in
      tmpfs|overlay|squashfs|proc|sysfs|devtmpfs|devpts|cgroup*|autofs|fusectl|securityfs|debugfs|configfs|pstore|binfmt_misc)
        continue ;;
    esac

    # Skip macOS system/virtual volumes and pseudo-filesystems.
    if [[ "$OSTYPE" == darwin* ]]; then
      case "$mp" in
        /System/Volumes/Preboot|/System/Volumes/Recovery|/System/Volumes/VM)
          continue ;;
        /System/Volumes/xarts|/System/Volumes/iSCPreboot|/System/Volumes/Hardware)
          continue ;;
      esac
      case "$fstype" in devfs|autofs|synthfs) continue ;; esac
    fi

    # Skip snap and docker mounts.
    case "$mp" in /snap/*|/var/lib/docker/*) continue ;; esac

    # Get usage info from df.
    # macOS df doesn't support -B1 (GNU coreutils). Use -k for 1K blocks
    # on macOS and -B1 for byte-accurate values on Linux.
    local df_line total pct hr_total used
    if [[ "$OSTYPE" == darwin* ]]; then
      df_line=$(df -k "$mp" 2>/dev/null | tail -1)
      total=$(echo "$df_line" | awk '{print $2}')
      # df -k returns 1K blocks; convert to bytes.
      total=$((total * 1024))
      pct=$(echo "$df_line" | awk '{print $5}' | tr -d '%')
    else
      df_line=$(df -B1 "$mp" 2>/dev/null | tail -1)
      total=$(echo "$df_line" | awk '{print $2}')
      pct=$(echo "$df_line" | awk '{print $5}' | tr -d '%')
    fi

    # Phase 2: btrfs display fallback. df returns zero for btrfs in some
    # picker contexts (statvfs vs multi-device). Re-fetch via
    # `btrfs filesystem usage --raw`. Two failure modes both fall through
    # to "(unknown size)": btrfs-progs missing, or parse failure.
    if [ "$fstype" = "btrfs" ] && { [ -z "$total" ] || [ "$total" = "0" ]; }; then
      if [ -n "$btrfs_bin" ]; then
        local btrfs_out
        if btrfs_out=$(btrfs_usage_for "$mp"); then
          total=$(echo "$btrfs_out" | awk '{print $1}')
          used=$(echo "$btrfs_out" | awk '{print $2}')
          if [ "$total" -gt 0 ] 2>/dev/null; then
            pct=$(awk -v u="$used" -v t="$total" 'BEGIN { printf "%d", (u*100)/t }')
          fi
        fi
      else
        # No btrfs binary available. Fall through to (unknown size).
        btrfs_seen_missing_progs=1
      fi
    fi

    if [ -z "$total" ] || [ "$total" = "0" ]; then
      hr_total="?"
      pct="?"
    elif [ "$total" -gt 1099511627776 ] 2>/dev/null; then
      hr_total="$(echo "$total" | awk '{printf "%.1fT", $1/1099511627776}')";
    elif [ "$total" -gt 1073741824 ] 2>/dev/null; then
      hr_total="$(echo "$total" | awk '{printf "%.0fG", $1/1073741824}')";
    elif [ "$total" -gt 1048576 ] 2>/dev/null; then
      hr_total="$(echo "$total" | awk '{printf "%.0fM", $1/1048576}')";
    else
      hr_total="${total}B"
    fi

    # When size is unknown, swap the formatted label for an explicit
    # "(unknown size)" so the user doesn't see "?  (?% used)".
    local fs_label
    if [ "$hr_total" = "?" ]; then
      fs_label="$(printf '%-16s %-6s %s' "$mp" "$fstype" "(unknown size)")"
    else
      fs_label="$(printf '%-16s %-6s %6s  (%s%% used)' "$mp" "$fstype" "$hr_total" "$pct")"
    fi

    # Get UUID.
    local uuid=""
    if command -v blkid &>/dev/null && [ -b "$dev" ]; then
      uuid=$(blkid -s UUID -o value "$dev" 2>/dev/null || true)
    fi
    if [ -z "$uuid" ] && command -v diskutil &>/dev/null; then
      uuid=$(diskutil info "$dev" 2>/dev/null | grep "Volume UUID" | awk '{print $NF}' || true)
    fi

    fs_mps+=("$mp")
    fs_devs+=("$dev")
    fs_types+=("$fstype")
    fs_roots+=("$root")
    fs_uuids+=("$uuid")
    fs_labels+=("$fs_label")

  done <<< "$mounts"

  # Phase 2: warn once if we encountered btrfs mounts and the binary is
  # missing. This makes the (unknown size) labels self-explanatory --
  # the user knows what to install if they want real numbers.
  if [ "$btrfs_seen_missing_progs" = "1" ]; then
    warn "btrfs-progs not installed -- btrfs entries will show (unknown size). Install btrfs-progs to enable size detection."
  fi

  # ---------------------------------------------------------------
  # Phase 1B-1: Strict (source, fstype, root) dedup.
  #
  # Two mounts sharing this composite key are guaranteed to point at
  # the same filesystem subtree (per kernel mountinfo semantics).
  # Tiebreak: keep the entry with the shortest mount_point.
  #
  # NOTE on filter scope: the dev filter above only admits /dev/* and
  # zfs entries. fuse.rclone, fuse.sshfs, and similar FUSE backends
  # are dropped before reaching dedup. Widening the filter to surface
  # those mounts is a separate scope decision -- see follow-up note
  # in docs/internal/research/filesystem-reporting-edge-cases.md.
  # ---------------------------------------------------------------
  if [ "$mount_source" = "mountinfo" ] && [ "${BASH_VERSINFO[0]:-0}" -ge 4 ] && [ "${#fs_mps[@]}" -gt 0 ]; then
    # Associative arrays (local -A) require bash 4+. On older bash (QNAP QTS
    # ships 3.2), skip dedup -- the user may see duplicate bind mounts in the
    # picker but everything still works.
    local -a dd_mps=() dd_devs=() dd_types=() dd_roots=() dd_uuids=() dd_labels=()
    local -A seen_key=()  # key -> index into dd_* arrays
    local i key existing_idx existing_mp
    for ((i=0; i<${#fs_mps[@]}; i++)); do
      key="${fs_devs[$i]}|${fs_types[$i]}|${fs_roots[$i]}"
      if [ -z "${seen_key[$key]:-}" ]; then
        # First occurrence -- append.
        dd_mps+=("${fs_mps[$i]}")
        dd_devs+=("${fs_devs[$i]}")
        dd_types+=("${fs_types[$i]}")
        dd_roots+=("${fs_roots[$i]}")
        dd_uuids+=("${fs_uuids[$i]}")
        dd_labels+=("${fs_labels[$i]}")
        seen_key[$key]=$((${#dd_mps[@]} - 1))
      else
        # Duplicate -- replace existing if this mount_point is shorter.
        existing_idx="${seen_key[$key]}"
        existing_mp="${dd_mps[$existing_idx]}"
        if [ "${#fs_mps[$i]}" -lt "${#existing_mp}" ]; then
          dd_mps[$existing_idx]="${fs_mps[$i]}"
          # Other fields are equal by construction (same dedup key);
          # only the label needs refreshing because it embeds mp.
          dd_labels[$existing_idx]="${fs_labels[$i]}"
        fi
      fi
    done
    fs_mps=("${dd_mps[@]}")
    fs_devs=("${dd_devs[@]}")
    fs_types=("${dd_types[@]}")
    fs_roots=("${dd_roots[@]}")
    fs_uuids=("${dd_uuids[@]}")
    fs_labels=("${dd_labels[@]}")
  fi

  local count=${#fs_mps[@]}
  if [ "$count" -eq 0 ]; then
    info "No block-device filesystems detected — skipping disk usage monitoring."
    return
  fi

  # ---------------------------------------------------------------
  # Phase 1B-3: canonical entry + bind-mount hiding.
  #
  # Bind mounts are a Linux kernel feature exposed via mountinfo.
  # On non-Linux platforms (macOS, BSD) there are no bind mounts to
  # group, so every entry is canonical. The grouping logic uses bash
  # 4+ associative arrays (local -A) which are not available on
  # macOS's bash 3.2, so we gate the entire block on mount_source.
  #
  # Group dedup'd entries by (source, fstype). Within each group:
  #   - If any entry has root="/", canonical = shortest mp among
  #     those root="/" entries. All other entries (root="/" non-shortest
  #     plus any root != "/") are hidden by default.
  #   - If no entry has root="/" (typical for btrfs subvolumes), no
  #     hiding -- every entry is its own real subtree.
  #
  # Single-entry groups: no hiding, no tag, no count.
  # ---------------------------------------------------------------
  local -a is_canonical=() is_hidden=() hidden_count_for=()
  local total_hidden=0
  local groups_with_hidden=0
  local i grp

  if [ "$mount_source" = "mountinfo" ] && [ "${BASH_VERSINFO[0]:-0}" -ge 4 ]; then
    # Linux: full bind-mount grouping with associative arrays (bash 4+).
    # Skipped on bash 3.2 (QNAP QTS) -- falls through to the else branch
    # where every entry is treated as canonical (no hiding).
    local -A group_canonical=()  # group_key -> canonical idx
    local -A group_has_root_slash=()  # group_key -> "1" if any root="/" exists
    local -A group_hidden_count=()  # group_key -> N

    # Initialize flag arrays.
    for ((i=0; i<count; i++)); do
      is_canonical+=("0")
      is_hidden+=("0")
      hidden_count_for+=("0")
    done

    # Pass 1: detect which (source, fstype) groups have a root="/" entry.
    for ((i=0; i<count; i++)); do
      grp="${fs_devs[$i]}|${fs_types[$i]}"
      if [ "${fs_roots[$i]}" = "/" ]; then
        group_has_root_slash[$grp]="1"
      fi
    done

    # Pass 2: pick canonical per group.
    #   With root="/": shortest mp among entries where root="/".
    #   Without root="/": every entry is its own canonical (subvolume case).
    for ((i=0; i<count; i++)); do
      grp="${fs_devs[$i]}|${fs_types[$i]}"
      if [ "${group_has_root_slash[$grp]:-}" = "1" ]; then
        # Group has a root="/" entry. Only consider root="/" candidates.
        [ "${fs_roots[$i]}" != "/" ] && continue
        if [ -z "${group_canonical[$grp]:-}" ]; then
          group_canonical[$grp]="$i"
        else
          local cur="${group_canonical[$grp]}"
          if [ "${#fs_mps[$i]}" -lt "${#fs_mps[$cur]}" ]; then
            group_canonical[$grp]="$i"
          fi
        fi
      else
        # No root="/" in group -- every entry is canonical.
        is_canonical[$i]="1"
      fi
    done

    # Pass 3: mark canonicals from group_canonical map; mark non-canonicals
    # in root="/"-bearing groups as hidden.
    for grp in "${!group_canonical[@]}"; do
      local cidx="${group_canonical[$grp]}"
      is_canonical[$cidx]="1"
    done
    for ((i=0; i<count; i++)); do
      grp="${fs_devs[$i]}|${fs_types[$i]}"
      if [ "${group_has_root_slash[$grp]:-}" = "1" ] && [ "${is_canonical[$i]}" = "0" ]; then
        is_hidden[$i]="1"
        group_hidden_count[$grp]=$((${group_hidden_count[$grp]:-0} + 1))
      fi
    done

    # Annotate canonicals with their group's hidden count for display.
    for grp in "${!group_hidden_count[@]}"; do
      local n="${group_hidden_count[$grp]}"
      [ "$n" -ge 1 ] && groups_with_hidden=$((groups_with_hidden + 1))
      total_hidden=$((total_hidden + n))
      local cidx="${group_canonical[$grp]}"
      hidden_count_for[$cidx]="$n"
    done
  else
    # Non-Linux (macOS, BSD): no bind mounts. Every entry is canonical.
    for ((i=0; i<count; i++)); do
      is_canonical+=("1")
      is_hidden+=("0")
      hidden_count_for+=("0")
    done
  fi

  # ---------------------------------------------------------------
  # Display: default (collapsed) view shows only canonicals, with a
  # [+N bind mounts hidden] tag when applicable. If the user types y
  # to expand, reprint with all entries; non-canonical ones get a
  # [bind mount] tag.
  # ---------------------------------------------------------------
  local expanded=0
  local -a visible_indices=()

  build_visible_indices() {
    visible_indices=()
    for ((i=0; i<count; i++)); do
      if [ "$expanded" = "1" ] || [ "${is_hidden[$i]}" = "0" ]; then
        visible_indices+=("$i")
      fi
    done
  }

  print_visible() {
    echo ""
    echo -e "  ${BOLD}Disk Usage Monitoring${NC}"
    echo "  Select mountpoints to report to Home Assistant."
    echo ""
    local pos idx label suffix n plural
    for ((pos=0; pos<${#visible_indices[@]}; pos++)); do
      idx="${visible_indices[$pos]}"
      label="${fs_labels[$idx]}"
      suffix=""
      if [ "$expanded" = "1" ]; then
        # Expanded view: tag non-canonical entries.
        [ "${is_canonical[$idx]}" = "0" ] && suffix="  [bind mount]"
      else
        # Collapsed view: tag canonicals that have hidden siblings.
        n="${hidden_count_for[$idx]}"
        if [ "$n" -ge 1 ]; then
          plural="s"
          [ "$n" -eq 1 ] && plural=""
          suffix="  [+${n} bind mount${plural} hidden]"
        fi
      fi
      echo "    $((pos + 1))) ${label}${suffix}"
    done
    echo ""
  }

  build_visible_indices
  print_visible

  # Show the y/N expansion prompt only if there are hidden mounts
  # AND we are still in collapsed view.
  if [ "$expanded" = "0" ] && [ "$total_hidden" -ge 1 ]; then
    local entry_word="entries"
    [ "$total_hidden" -eq 1 ] && entry_word="entry"
    local part_word="partitions"
    [ "$groups_with_hidden" -eq 1 ] && part_word="partition"
    local hide_msg="${total_hidden} bind-mount ${entry_word} hidden across ${groups_with_hidden} ${part_word}."
    echo "  ${hide_msg}"
    read -rp "  Show all entries? (y/N) [N]: " EXPAND_CHOICE < "$TTY_IN"
    case "${EXPAND_CHOICE:-N}" in
      y|Y|yes|YES)
        expanded=1
        build_visible_indices
        print_visible
        ;;
    esac
  fi

  local visible_count=${#visible_indices[@]}
  local range_hint="1"
  [ "$visible_count" -gt 1 ] && range_hint="1,2..${visible_count}"
  read -rp "  Monitor ($range_hint / all / none) [all]: " FS_CHOICE < "$TTY_IN"
  FS_CHOICE="${FS_CHOICE:-all}"

  # Parse selection. `num` refers to position within visible_indices.
  local -a selected_indices=()
  case "$FS_CHOICE" in
    all|ALL|a|A)
      for ((pos=0; pos<visible_count; pos++)); do
        selected_indices+=("${visible_indices[$pos]}")
      done
      ;;
    none|NONE|n|N)
      info "Disk usage monitoring disabled."
      return
      ;;
    *)
      # Comma-separated numbers.
      IFS=',' read -ra nums <<< "$FS_CHOICE"
      for num in "${nums[@]}"; do
        num=$(echo "$num" | tr -d ' ')
        if [[ "$num" =~ ^[0-9]+$ ]] && [ "$num" -ge 1 ] && [ "$num" -le "$visible_count" ]; then
          selected_indices+=("${visible_indices[$((num - 1))]}")
        else
          warn "Skipping invalid choice: $num"
        fi
      done
      ;;
  esac

  if [ ${#selected_indices[@]} -eq 0 ]; then
    info "No valid mountpoints selected — disk usage monitoring disabled."
    return
  fi

  # Build YAML and display string.
  FS_YAML="filesystems:"
  local -a display_mps=()
  for idx in "${selected_indices[@]}"; do
    FS_YAML="${FS_YAML}
  - path: \"${fs_mps[$idx]}\"
    uuid: \"${fs_uuids[$idx]}\"
    device: \"${fs_devs[$idx]}\"
    fstype: \"${fs_types[$idx]}\""
    display_mps+=("${fs_mps[$idx]}")
  done

  FS_DISPLAY=$(IFS=', '; echo "${display_mps[*]}")
  success "Monitoring ${#selected_indices[@]} mountpoint(s): $FS_DISPLAY"
}

# ---------------------------------------------------------------------------
# Network interface picker — shows numbered list, user enters a number
# or "all". Sets ADV_IFACE to the chosen interface or "" for auto-filter.
# ---------------------------------------------------------------------------
# Cosmetic labels for the interface picker UI. This list does NOT need to
# mirror every entry in agent/config.go's defaultSkipPrefixes (51 entries).
# It only tags common virtual interfaces so users can identify them during
# install. The actual runtime filtering is handled by the Go agent.
# Keep loosely in sync -- add entries when users report confusion.
VIRTUAL_PREFIXES="docker|docker_gwbridge|br-|lxcbr|lxdbr|veth|podman|hassio|zt|tailscale|ts|wg|tun|tap|utun|virbr|vmbr|fwbr|fwpr|fwln|vbox|vmnet|lo"

pick_interface() {
  local -a iface_names=()
  local -a iface_labels=()
  IFACE_COUNT=0
  NON_VIRTUAL_COUNT=0

  for iface in $(ls /sys/class/net 2>/dev/null || ifconfig -l 2>/dev/null | tr ' ' '\n'); do
    local ip4=""
    if command -v ip &>/dev/null; then
      ip4=$(ip -4 addr show "$iface" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
    else
      ip4=$(ifconfig "$iface" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
    fi
    [ -z "$ip4" ] && continue

    IFACE_COUNT=$((IFACE_COUNT + 1))
    local tag_label=""
    if echo "$iface" | grep -qiE "^($VIRTUAL_PREFIXES)"; then
      case "$iface" in
        docker*|br-*) tag_label="${YELLOW}(Docker)${NC}" ;;
        veth*|podman*) tag_label="${YELLOW}(container)${NC}" ;;
        lxcbr*|lxdbr*) tag_label="${YELLOW}(LXC/LXD)${NC}" ;;
        hassio*)      tag_label="${YELLOW}(HA OS)${NC}" ;;
        zt*)          tag_label="${YELLOW}(ZeroTier)${NC}" ;;
        tailscale*|ts*) tag_label="${YELLOW}(Tailscale)${NC}" ;;
        wg*)          tag_label="${YELLOW}(WireGuard)${NC}" ;;
        tun*|tap*|utun*) tag_label="${YELLOW}(VPN tunnel)${NC}" ;;
        virbr*)       tag_label="${YELLOW}(libvirt)${NC}" ;;
        vmbr*|fwbr*|fwpr*|fwln*) tag_label="${YELLOW}(Proxmox)${NC}" ;;
        vbox*)        tag_label="${YELLOW}(VirtualBox)${NC}" ;;
        vmnet*)       tag_label="${YELLOW}(VMware)${NC}" ;;
        lo*)          tag_label="${YELLOW}(loopback)${NC}" ;;
        *)            tag_label="${YELLOW}(virtual)${NC}" ;;
      esac
    else
      NON_VIRTUAL_COUNT=$((NON_VIRTUAL_COUNT + 1))
    fi

    iface_names+=("$iface")
    iface_labels+=("$(printf '%-16s %s  %s' "$iface" "$ip4" "$tag_label")")
  done

  ADV_IFACE=""
  if [ "$IFACE_COUNT" -le 1 ]; then
    info "Single interface detected — using auto-filter."
    return
  fi

  echo ""
  echo -e "  ${BOLD}Network Interface (mDNS)${NC}"
  echo "  Home Assistant uses this to auto-discover the agent."
  echo ""
  for ((i=0; i<${#iface_names[@]}; i++)); do
    echo -e "    $((i + 1))) ${iface_labels[$i]}"
  done
  echo ""

  read -rp "  Advertise on (1-${#iface_names[@]} / all) [all]: " IFACE_CHOICE < "$TTY_IN"
  IFACE_CHOICE="${IFACE_CHOICE:-all}"

  case "$IFACE_CHOICE" in
    all|ALL|a|A|"")
      info "mDNS: auto-filter mode (all physical interfaces)."
      ADV_IFACE=""
      ;;
    *)
      if [[ "$IFACE_CHOICE" =~ ^[0-9]+$ ]] && [ "$IFACE_CHOICE" -ge 1 ] && [ "$IFACE_CHOICE" -le "${#iface_names[@]}" ]; then
        ADV_IFACE="${iface_names[$((IFACE_CHOICE - 1))]}"
        success "mDNS will advertise on: $ADV_IFACE"
      else
        warn "Invalid choice — using auto-filter."
        ADV_IFACE=""
      fi
      ;;
  esac
}

# Returns the count of non-virtual interfaces (call after pick_interface or
# after running the same detection loop). Used to decide whether to prompt
# during upgrades from pre-interface-picker configs.
count_non_virtual_interfaces() {
  NON_VIRTUAL_COUNT=0
  for iface in $(ls /sys/class/net 2>/dev/null || ifconfig -l 2>/dev/null | tr ' ' '\n'); do
    if command -v ip &>/dev/null; then
      ip4=$(ip -4 addr show "$iface" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
    else
      ip4=$(ifconfig "$iface" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
    fi
    [ -z "$ip4" ] && continue
    if ! echo "$iface" | grep -qiE "^($VIRTUAL_PREFIXES)"; then
      NON_VIRTUAL_COUNT=$((NON_VIRTUAL_COUNT + 1))
    fi
  done
}

# ---------------------------------------------------------------------------
# Install-path defaults (may be overridden by resolve_install_paths below)
# ---------------------------------------------------------------------------
INSTALL_BIN="/usr/local/bin/$BINARY_NAME"
INSTALL_CFG="/etc/smartha-agent"

# ---------------------------------------------------------------------------
# Resolve writable install paths
#
# Standard Linux/macOS:  /usr/local/bin  +  /etc/smartha-agent
# Immutable-rootfs (ZimaOS, etc.):  /DATA/smartha-agent  (bin + config)
# Generic fallback:  /opt/smartha-agent  (bin + config)
#
# The probe runs as root (installer requires sudo), so a writability failure
# genuinely means the filesystem is read-only, not a permissions issue.
# ---------------------------------------------------------------------------
resolve_install_paths() {
  # Candidate 1: standard paths (works on most Linux, macOS, Proxmox, etc.)
  if mkdir -p /usr/local/bin 2>/dev/null && [ -w /usr/local/bin ]; then
    INSTALL_BIN="/usr/local/bin/$BINARY_NAME"
    INSTALL_CFG="/etc/smartha-agent"
    return
  fi

  # Candidate 2: /DATA (ZimaOS, CasaOS, and similar NAS distros)
  if [ -d /DATA ] && mkdir -p /DATA/smartha-agent 2>/dev/null && [ -w /DATA/smartha-agent ]; then
    INSTALL_BIN="/DATA/smartha-agent/$BINARY_NAME"
    INSTALL_CFG="/DATA/smartha-agent"
    warn "Immutable root filesystem detected — installing to /DATA/smartha-agent/"
    return
  fi

  # Candidate 3: /opt (generic fallback)
  if mkdir -p /opt/smartha-agent 2>/dev/null && [ -w /opt/smartha-agent ]; then
    INSTALL_BIN="/opt/smartha-agent/$BINARY_NAME"
    INSTALL_CFG="/opt/smartha-agent"
    warn "Standard paths not writable — installing to /opt/smartha-agent/"
    return
  fi

  fail "No writable install location found. Tried /usr/local/bin, /DATA/smartha-agent, /opt/smartha-agent."
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
do_uninstall() {
  echo ""
  echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
  echo -e "${BOLD}║   SMART Sniffer Agent — Uninstaller      ║${NC}"
  echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
  echo ""

  OS=$(uname -s | tr '[:upper:]' '[:lower:]')

  # Stop and remove service
  if [ "$OS" = "linux" ]; then
    # systemd service
    if systemctl is-active --quiet smartha-agent 2>/dev/null; then
      info "Stopping systemd service..."
      systemctl stop smartha-agent
    fi
    if [ -f /etc/systemd/system/smartha-agent.service ]; then
      info "Removing systemd service..."
      systemctl disable smartha-agent 2>/dev/null || true
      rm -f /etc/systemd/system/smartha-agent.service
      systemctl daemon-reload
      success "systemd service removed."
    fi
    # init.d service (QNAP, non-systemd Linux)
    if [ -f /etc/init.d/smartha-agent ]; then
      info "Stopping init.d service..."
      /etc/init.d/smartha-agent stop 2>/dev/null || true
      if command -v update-rc.d >/dev/null 2>&1; then
        update-rc.d -f smartha-agent remove 2>/dev/null || true
      elif command -v rc-update >/dev/null 2>&1; then
        rc-update del smartha-agent 2>/dev/null || true
      fi
      rm -f /etc/init.d/smartha-agent
      success "init.d service removed."
    fi
    # Clean up PID file and log
    rm -f /var/run/smartha-agent.pid
    rm -f /var/log/smartha-agent.log /var/log/smartha-agent.log.1
  elif [ "$OS" = "darwin" ]; then
    PLIST="/Library/LaunchDaemons/com.dablabs.smartha-agent.plist"
    if launchctl list | grep -q com.dablabs.smartha-agent 2>/dev/null; then
      info "Unloading launchd service..."
      launchctl unload "$PLIST" 2>/dev/null || true
    fi
    if [ -f "$PLIST" ]; then
      info "Removing plist..."
      rm -f "$PLIST"
      success "Service removed."
    fi
  fi

  # Remove binary and config from all candidate locations
  FOUND=false
  for BIN_PATH in \
    "/usr/local/bin/$BINARY_NAME" \
    "/DATA/smartha-agent/$BINARY_NAME" \
    "/opt/smartha-agent/$BINARY_NAME"; do
    if [ -f "$BIN_PATH" ]; then
      info "Removing binary ($BIN_PATH)..."
      rm -f "$BIN_PATH"
      success "Binary removed."
      FOUND=true
    fi
  done

  for CFG_PATH in \
    "/etc/smartha-agent" \
    "/DATA/smartha-agent" \
    "/opt/smartha-agent"; do
    if [ -d "$CFG_PATH" ]; then
      info "Removing config directory ($CFG_PATH)..."
      rm -rf "$CFG_PATH"
      success "Config removed."
      FOUND=true
    fi
  done

  if [ "$FOUND" = "false" ]; then
    warn "No installed files found in any known location."
  fi

  # macOS log files
  if [ "$OS" = "darwin" ]; then
    rm -f /var/log/smartha-agent.log /var/log/smartha-agent.error.log 2>/dev/null
  fi

  echo ""
  echo -e "${GREEN}  ✓ SMART Sniffer Agent has been completely removed.${NC}"
  echo ""
  exit 0
}

# Check for --uninstall flag (via args or environment variable)
# IMPORTANT: Save env var BEFORE overwriting, since UNINSTALL=1 may come
# from the caller's environment (e.g. curl ... | sudo UNINSTALL=1 bash)
_UNINSTALL_ENV="${UNINSTALL:-}"
UNINSTALL_REQUESTED=false
for arg in "$@"; do
  case "$arg" in
    --uninstall|-u|uninstall) UNINSTALL_REQUESTED=true ;;
  esac
done
if [ "$_UNINSTALL_ENV" = "1" ] || [ "$_UNINSTALL_ENV" = "true" ] || [ "$UNINSTALL_REQUESTED" = "true" ]; then
  if [ "$EUID" -ne 0 ]; then
    fail "Please run as root: sudo bash $0 --uninstall"
  fi
  do_uninstall
fi

# ---------------------------------------------------------------------------
# Service install functions (defined before use)
# ---------------------------------------------------------------------------

# ===== LINUX: systemd service =====
install_systemd_service() {
  SERVICE_NAME="smartha-agent"
  SERVICE_DEST="/etc/systemd/system/${SERVICE_NAME}.service"

  info "Installing systemd service..."

  # Stop existing service if running.
  if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    warn "Stopping existing service..."
    systemctl stop "$SERVICE_NAME"
  fi

  cat > "$SERVICE_DEST" <<SVCEOF
[Unit]
Description=SMART Sniffer Agent — disk health REST API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$INSTALL_BIN
WorkingDirectory=$INSTALL_CFG
User=root
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl start "$SERVICE_NAME"
  success "systemd service installed, enabled, and started."

  echo ""
  echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}${BOLD}║   SMART Sniffer Agent installed successfully  ║${NC}"
  echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
  echo ""
  echo "  Config   : $INSTALL_CFG/config.yaml"
  echo ""
  echo "  Commands:"
  echo "    Status:    systemctl status $SERVICE_NAME"
  echo "    Logs:      journalctl -u $SERVICE_NAME -f"
  echo "    Restart:   systemctl restart $SERVICE_NAME"
  echo "    Uninstall: curl -sSL https://raw.githubusercontent.com/$REPO/main/install.sh | sudo UNINSTALL=1 bash"
  echo ""
}

# ===== LINUX: init.d service (non-systemd fallback) =====
# Used on QNAP QTS, older NAS firmware, and minimal Linux without systemd.
install_initd_service() {
  SERVICE_NAME="smartha-agent"
  INITD_DEST="/etc/init.d/$SERVICE_NAME"
  PIDFILE="/var/run/${SERVICE_NAME}.pid"
  LOGFILE="/var/log/${SERVICE_NAME}.log"
  MAX_LOG_BYTES=10485760  # 10 MB

  info "No systemd detected -- installing init.d service..."

  # Stop existing init.d service if running.
  if [ -f "$INITD_DEST" ] && [ -x "$INITD_DEST" ]; then
    warn "Stopping existing init.d service..."
    "$INITD_DEST" stop 2>/dev/null || true
  fi

  cat > "$INITD_DEST" <<INITEOF
#!/bin/sh
# SMART Sniffer Agent -- init.d service script
# Installed by install.sh. Re-run the installer to update.

DAEMON=$INSTALL_BIN
WORKDIR=$INSTALL_CFG
PIDFILE=/var/run/smartha-agent.pid
LOGFILE=/var/log/smartha-agent.log
NAME=smartha-agent
MAX_LOG_BYTES=10485760

# Rotate log if it exceeds MAX_LOG_BYTES.
rotate_log() {
  if [ -f "\$LOGFILE" ]; then
    log_size=\$(wc -c < "\$LOGFILE" 2>/dev/null || echo 0)
    if [ "\$log_size" -gt "\$MAX_LOG_BYTES" ]; then
      mv "\$LOGFILE" "\$LOGFILE.1"
    fi
  fi
}

case "\$1" in
  start)
    echo "Starting \$NAME..."
    rotate_log
    if command -v start-stop-daemon >/dev/null 2>&1; then
      echo "  (via start-stop-daemon)" >> "\$LOGFILE"
      start-stop-daemon -S -b -m -p "\$PIDFILE" -x "\$DAEMON" -d "\$WORKDIR"
    else
      echo "  (via POSIX backgrounding)" >> "\$LOGFILE"
      cd "\$WORKDIR"
      ( trap '' HUP; exec "\$DAEMON" >> "\$LOGFILE" 2>&1 ) &
      echo \$! > "\$PIDFILE"
    fi
    ;;
  stop)
    echo "Stopping \$NAME..."
    if command -v start-stop-daemon >/dev/null 2>&1; then
      start-stop-daemon -K -p "\$PIDFILE" 2>/dev/null
    else
      if [ -f "\$PIDFILE" ]; then
        _pid=\$(cat "\$PIDFILE")
        # Verify PID belongs to our daemon before killing
        if kill -0 "\$_pid" 2>/dev/null; then
          _cmd=\$(cat /proc/\$_pid/comm 2>/dev/null || ps -p \$_pid -o comm= 2>/dev/null)
          if [ "\$_cmd" = "\$NAME" ]; then
            kill "\$_pid" 2>/dev/null
          fi
        fi
      fi
    fi
    rm -f "\$PIDFILE"
    ;;
  restart)
    \$0 stop
    sleep 1
    \$0 start
    ;;
  status)
    if [ -f "\$PIDFILE" ] && kill -0 "\$(cat "\$PIDFILE")" 2>/dev/null; then
      echo "\$NAME is running (PID \$(cat "\$PIDFILE"))"
    else
      echo "\$NAME is not running"
      exit 1
    fi
    ;;
  *)
    echo "Usage: \$0 {start|stop|restart|status}"
    exit 1
    ;;
esac
INITEOF

  chmod +x "$INITD_DEST"

  # Register for boot where possible.
  if command -v update-rc.d >/dev/null 2>&1; then
    update-rc.d "$SERVICE_NAME" defaults 2>/dev/null || true
  elif command -v rc-update >/dev/null 2>&1; then
    rc-update add "$SERVICE_NAME" default 2>/dev/null || true
  fi

  # Start the service now.
  "$INITD_DEST" start
  success "init.d service installed and started."

  echo ""
  echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}${BOLD}║   SMART Sniffer Agent installed successfully  ║${NC}"
  echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
  echo ""
  echo "  Config   : $INSTALL_CFG/config.yaml"
  echo "  Log      : $LOGFILE"
  echo ""
  echo "  Commands:"
  echo "    Status:    $INITD_DEST status"
  echo "    Logs:      tail -f $LOGFILE"
  echo "    Restart:   $INITD_DEST restart"
  echo "    Uninstall: curl -sSL https://raw.githubusercontent.com/$REPO/main/install.sh | sudo UNINSTALL=1 bash"
  echo ""
  warn "Note: On some NAS platforms (QNAP), firmware updates may remove"
  warn "the init.d script. If the agent stops working after a firmware"
  warn "update, re-run this installer to restore it."
  echo ""
}

# ===== LINUX: service install router =====
install_linux_service() {
  if [ -d /run/systemd/system ]; then
    install_systemd_service
  else
    install_initd_service
  fi
}

# ===== MACOS: launchd plist =====
install_macos_service() {
  PLIST_NAME="com.dablabs.smartha-agent"
  PLIST_DEST="/Library/LaunchDaemons/${PLIST_NAME}.plist"

  info "Installing launchd service..."

  # Unload existing service if present.
  if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
    warn "Unloading existing service..."
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
  fi

  cat > "$PLIST_DEST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${INSTALL_BIN}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_CFG}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>StandardOutPath</key>
    <string>/var/log/smartha-agent.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/smartha-agent.error.log</string>
</dict>
</plist>
PLISTEOF

  chown root:wheel "$PLIST_DEST"
  chmod 644 "$PLIST_DEST"
  launchctl load -w "$PLIST_DEST"
  success "launchd service installed and started."

  echo ""
  echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}${BOLD}║   SMART Sniffer Agent installed successfully  ║${NC}"
  echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
  echo ""
  echo "  Config   : $INSTALL_CFG/config.yaml"
  echo "  Logs     : /var/log/smartha-agent.log"
  echo ""
  echo "  Commands:"
  echo "    Stop:      sudo launchctl unload $PLIST_DEST"
  echo "    Start:     sudo launchctl load -w $PLIST_DEST"
  echo "    Restart:   sudo launchctl kickstart -k system/$PLIST_NAME"
  echo "    Logs:      tail -f /var/log/smartha-agent.log"
  echo "    Uninstall: curl -sSL https://raw.githubusercontent.com/$REPO/main/install.sh | sudo UNINSTALL=1 bash"
  echo ""
}

# ---------------------------------------------------------------------------
# Must run as root
# ---------------------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  fail "Please run as root: sudo bash $0"
fi

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   SMART Sniffer Agent — Installer        ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""

# ---------------------------------------------------------------------------
# Detect platform
# ---------------------------------------------------------------------------
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

case "$OS" in
  linux)  PLATFORM="linux" ;;
  darwin) PLATFORM="darwin" ;;
  *)      fail "Unsupported OS: $OS. This installer supports Linux and macOS." ;;
esac

case "$ARCH" in
  x86_64)  GOARCH="amd64" ;;
  aarch64) GOARCH="arm64" ;;
  arm64)   GOARCH="arm64" ;;
  *)       fail "Unsupported architecture: $ARCH" ;;
esac

BINARY_FILE="${BINARY_NAME}-${PLATFORM}-${GOARCH}"
info "Detected platform: ${PLATFORM}/${GOARCH}"

# Probe for writable install paths (must run after root check)
resolve_install_paths
info "Install location: $INSTALL_BIN"

# ---------------------------------------------------------------------------
# Resolve version and download URL
# ---------------------------------------------------------------------------
if [ -z "${VERSION:-}" ]; then
  info "Fetching latest release version..."
  VERSION=$(curl -sSf "https://api.github.com/repos/$REPO/releases/latest" \
    | grep '"tag_name"' | head -1 | sed 's/.*"v\([^"]*\)".*/\1/')
  if [ -z "$VERSION" ]; then
    fail "Could not determine latest version. Set VERSION=x.y.z manually."
  fi
fi
success "Version: v${VERSION}"

RELEASE_URL="https://github.com/$REPO/releases/download/v${VERSION}"
BINARY_URL="${RELEASE_URL}/${BINARY_FILE}"
CHECKSUMS_URL="${RELEASE_URL}/checksums.txt"

# ---------------------------------------------------------------------------
# Download binary and verify checksum
# ---------------------------------------------------------------------------
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

info "Downloading ${BINARY_FILE}..."
curl -sSfL -o "$TMPDIR/$BINARY_FILE" "$BINARY_URL" \
  || fail "Download failed. Check that version v${VERSION} exists at:\n  ${BINARY_URL}"

info "Verifying checksum..."
curl -sSfL -o "$TMPDIR/checksums.txt" "$CHECKSUMS_URL" \
  || warn "Could not download checksums — skipping verification."

if [ -f "$TMPDIR/checksums.txt" ]; then
  EXPECTED=$(grep "$BINARY_FILE" "$TMPDIR/checksums.txt" | awk '{print $1}')
  if [ -n "$EXPECTED" ]; then
    if command -v sha256sum &>/dev/null; then
      ACTUAL=$(sha256sum "$TMPDIR/$BINARY_FILE" | awk '{print $1}')
    else
      ACTUAL=$(shasum -a 256 "$TMPDIR/$BINARY_FILE" | awk '{print $1}')
    fi
    if [ "$EXPECTED" = "$ACTUAL" ]; then
      success "Checksum verified."
    else
      fail "Checksum mismatch!\n  Expected: $EXPECTED\n  Got:      $ACTUAL"
    fi
  else
    warn "Binary not found in checksums file — skipping verification."
  fi
fi

# ---------------------------------------------------------------------------
# Install smartmontools
# ---------------------------------------------------------------------------
info "Checking for smartmontools..."
if ! command -v smartctl &>/dev/null; then
  if [ "$PLATFORM" = "darwin" ]; then
    warn "smartctl not found."
    if command -v brew &>/dev/null; then
      info "Installing via Homebrew..."
      sudo -u "${SUDO_USER:-$USER}" brew install smartmontools
    else
      fail "smartctl is required. Install it with: brew install smartmontools"
    fi
  else
    warn "smartctl not found. Installing..."
    if command -v apt-get &>/dev/null; then
      apt-get update -qq && apt-get install -y smartmontools
    elif command -v dnf &>/dev/null; then
      dnf install -y smartmontools
    elif command -v yum &>/dev/null; then
      yum install -y smartmontools
    elif command -v opkg &>/dev/null; then
      opkg update && opkg install smartmontools
    else
      fail "Could not detect package manager. Install smartmontools manually."
    fi
  fi
fi
success "smartctl found: $(smartctl --version | head -1)"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# When piped through bash (curl | bash), stdin is the script itself, not the
# terminal. Redirect reads from /dev/tty so interactive prompts still work.
# If /dev/tty isn't available (e.g. CI), fall back to defaults silently.
if [ -t 0 ]; then
  TTY_IN="/dev/stdin"
elif [ -e /dev/tty ]; then
  TTY_IN="/dev/tty"
else
  TTY_IN=""
fi

# ---------------------------------------------------------------------------
# Check for existing configuration (upgrade detection)
# ---------------------------------------------------------------------------
EXISTING_CONFIG="$INSTALL_CFG/config.yaml"
KEEP_CONFIG=false

if [ -f "$EXISTING_CONFIG" ]; then
  # Parse existing values from config.yaml
  _EXISTING_PORT=$(grep -E '^port:' "$EXISTING_CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)
  _EXISTING_TOKEN=$(grep -E '^token:' "$EXISTING_CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)
  _EXISTING_INTERVAL=$(grep -E '^scan_interval:' "$EXISTING_CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)
  _EXISTING_IFACE=$(grep -E '^advertise_interface:' "$EXISTING_CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)
  _EXISTING_FS=$(grep -E '^\s+- path:' "$EXISTING_CONFIG" 2>/dev/null | sed 's/.*path:\s*//' | tr -d '"' || true)

  # Only treat as valid if we got at least a port
  if [ -n "$_EXISTING_PORT" ]; then
    echo ""
    echo -e "${GREEN}${BOLD}  Existing configuration found:${NC}"
    echo "    Port:       $_EXISTING_PORT"
    if [ -n "$_EXISTING_TOKEN" ]; then
      # Mask the token for display
      _TOKEN_LEN=${#_EXISTING_TOKEN}
      if [ "$_TOKEN_LEN" -gt 4 ]; then
        _TOKEN_DISPLAY="${_EXISTING_TOKEN:0:2}$(printf '%*s' $((_TOKEN_LEN - 4)) '' | tr ' ' '•')${_EXISTING_TOKEN:$((_TOKEN_LEN - 2))}"
      else
        _TOKEN_DISPLAY="••••"
      fi
      echo "    Token:      $_TOKEN_DISPLAY"
    else
      echo "    Token:      (none)"
    fi
    echo "    Interval:   ${_EXISTING_INTERVAL:-60s}"
    if [ -n "$_EXISTING_IFACE" ]; then
      echo "    Interface:  $_EXISTING_IFACE"
    else
      echo "    Interface:  auto-filter"
    fi
    if [ -n "$_EXISTING_FS" ]; then
      _FS_LIST=$(echo "$_EXISTING_FS" | awk '{printf "%s%s", sep, $0; sep=", "} END{print ""}')
      echo "    Disk usage: $_FS_LIST"
    else
      echo "    Disk usage: (not configured)"
    fi
    echo ""

    if [ -n "$TTY_IN" ]; then
      read -rp "  Keep current settings? [Y/n]: " KEEP_CHOICE < "$TTY_IN"
      case "$KEEP_CHOICE" in
        [nN]|[nN][oO]) KEEP_CONFIG=false ;;
        *)             KEEP_CONFIG=true ;;
      esac
    else
      # Non-interactive upgrade: always keep existing config
      info "Non-interactive mode — keeping existing configuration."
      KEEP_CONFIG=true
    fi

    if [ "$KEEP_CONFIG" = "true" ]; then
      PORT="$_EXISTING_PORT"
      TOKEN="$_EXISTING_TOKEN"
      SCAN_INTERVAL="${_EXISTING_INTERVAL:-60s}"
      ADV_IFACE="$_EXISTING_IFACE"
      # Carry forward filesystem config for the Agent Summary display.
      if [ -n "$_EXISTING_FS" ]; then
        FS_YAML="existing"   # non-empty sentinel — config already has the block
        FS_DISPLAY=$(echo "$_EXISTING_FS" | awk '{printf "%s%s", sep, $0; sep=", "} END{print ""}')
      fi
      success "Keeping existing configuration."

      # --- Upgrade path: offer interface picker if config predates v0.4.25 ---
      # Old configs won't have advertise_interface. On multi-homed hosts this
      # can cause mDNS to advertise on a VPN or Docker IP. Prompt once.
      if [ -z "$ADV_IFACE" ] && [ -n "$TTY_IN" ]; then
        count_non_virtual_interfaces
        if [ "$NON_VIRTUAL_COUNT" -gt 1 ]; then
          echo ""
          warn "Your config doesn't specify a network interface for mDNS."
          echo "  Machines with multiple interfaces may advertise on the wrong IP."
          echo ""

          pick_interface "A"

          if [ -n "$ADV_IFACE" ]; then
            # Append to existing config (don't rewrite it)
            echo "advertise_interface: $ADV_IFACE" >> "$EXISTING_CONFIG"
            success "Interface saved to existing config."
          fi
        fi
      fi

      # --- Upgrade path: offer filesystem picker if config has no filesystems ---
      if ! grep -q '^filesystems:' "$EXISTING_CONFIG" 2>/dev/null && [ -n "$TTY_IN" ]; then
        echo ""
        info "Disk usage monitoring is now available."
        echo ""
        pick_filesystems
        if [ -n "$FS_YAML" ]; then
          echo "" >> "$EXISTING_CONFIG"
          echo "$FS_YAML" >> "$EXISTING_CONFIG"
          success "Filesystem monitoring added to existing config."
        fi
      fi
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Configuration prompts (skipped if keeping existing config)
# ---------------------------------------------------------------------------
if [ "$KEEP_CONFIG" = "false" ] && [ -n "$TTY_IN" ]; then
  echo ""
  echo -e "${BOLD}  Configuration${NC}"
  echo "  (Press Enter to accept defaults)"
  echo ""

  read -rp "  Port [9099]: " PORT < "$TTY_IN"
  read -rp "  Bearer token for API auth (leave blank to disable): " TOKEN < "$TTY_IN"
  read -rp "  Scan interval (e.g. 60s, 30m, 24h) [60s]: " SCAN_INTERVAL < "$TTY_IN"

  # --- Disk usage picker ---
  echo ""
  pick_filesystems

  # --- Network interface picker ---
  echo ""
  pick_interface

  # --- Standby-aware polling (HDD detection) ---
  STANDBY_MODE="never"
  _HAS_HDD="false"
  if command -v smartctl &>/dev/null; then
    _SCAN_JSON=$(smartctl --json --scan 2>/dev/null || true)
    if [ -n "$_SCAN_JSON" ]; then
      # Check each drive's rotation_rate. >0 means spinning disk (HDD).
      while IFS= read -r _DEV_PATH; do
        _DEV_INFO=$(smartctl --json -i "$_DEV_PATH" 2>/dev/null || true)
        _ROT=$(echo "$_DEV_INFO" | grep -o '"rotation_rate"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$' || true)
        if [ -n "$_ROT" ] && [ "$_ROT" -gt 0 ] 2>/dev/null; then
          _HAS_HDD="true"
          break
        fi
      done < <(echo "$_SCAN_JSON" | grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)"/\1/')
    fi
  fi
  if [ "$_HAS_HDD" = "true" ]; then
    echo ""
    echo "  Spinning drives (HDDs) detected. The agent can skip drives in"
    echo "  standby to avoid waking them up. Recommended for NAS setups."
    echo ""
    read -rp "  Enable standby-aware polling? [Y/n]: " _STANDBY_ANS < "$TTY_IN"
    case "$_STANDBY_ANS" in
      [nN]*) STANDBY_MODE="never" ;;
      *) STANDBY_MODE="standby" ;;
    esac
  fi

elif [ "$KEEP_CONFIG" = "false" ]; then
  info "Non-interactive mode detected -- using defaults."
  PORT=""
  TOKEN=""
  SCAN_INTERVAL=""
  ADV_IFACE=""
  FS_YAML=""
  FS_DISPLAY=""
  STANDBY_MODE="never"
fi

PORT="${PORT:-9099}"
SCAN_INTERVAL="${SCAN_INTERVAL:-60s}"

# If the user entered a bare number (e.g. "30"), append "s" for Go duration.
if echo "$SCAN_INTERVAL" | grep -qE '^[0-9]+$'; then
  SCAN_INTERVAL="${SCAN_INTERVAL}s"
fi

# ---------------------------------------------------------------------------
# Install binary (stop service first to avoid "Text file busy")
# ---------------------------------------------------------------------------
echo ""
if [ "$PLATFORM" = "linux" ]; then
  if systemctl is-active --quiet smartha-agent 2>/dev/null; then
    info "Stopping running agent before upgrade..."
    systemctl stop smartha-agent
  elif [ -f /etc/init.d/smartha-agent ] && [ -f /var/run/smartha-agent.pid ]; then
    info "Stopping running agent before upgrade..."
    /etc/init.d/smartha-agent stop 2>/dev/null || true
  fi
fi
if [ "$PLATFORM" = "darwin" ] && launchctl list | grep -q com.dablabs.smartha-agent 2>/dev/null; then
  info "Stopping running agent before upgrade..."
  launchctl unload /Library/LaunchDaemons/com.dablabs.smartha-agent.plist 2>/dev/null || true
fi
info "Installing binary to $INSTALL_BIN..."
cp "$TMPDIR/$BINARY_FILE" "$INSTALL_BIN"
chmod +x "$INSTALL_BIN"

# macOS: remove Gatekeeper quarantine flag so the binary can run without
# an "unidentified developer" dialog. Files downloaded via curl get the
# com.apple.quarantine xattr automatically. This attribute is NOT
# SIP-protected and can be removed with sudo. The || true ensures this
# is a no-op on Linux (where xattr may not exist).
# Verified working through macOS Tahoe (16) as of April 2026.
if [ "$PLATFORM" = "darwin" ]; then
  xattr -d com.apple.quarantine "$INSTALL_BIN" 2>/dev/null || true
fi
success "Binary installed."

# ---------------------------------------------------------------------------
# Write config (skip if keeping existing config)
# ---------------------------------------------------------------------------
if [ "$KEEP_CONFIG" = "true" ]; then
  info "Keeping existing config at $INSTALL_CFG/config.yaml"
else
  info "Writing config to $INSTALL_CFG/config.yaml..."
  mkdir -p "$INSTALL_CFG"

  cat > "$INSTALL_CFG/config.yaml" <<CONFEOF
port: $PORT
scan_interval: $SCAN_INTERVAL
CONFEOF

  if [ -n "$TOKEN" ]; then
    echo "token: \"$TOKEN\"" >> "$INSTALL_CFG/config.yaml"
  fi
  if [ -n "$ADV_IFACE" ]; then
    echo "advertise_interface: $ADV_IFACE" >> "$INSTALL_CFG/config.yaml"
  fi
  if [ -n "$STANDBY_MODE" ] && [ "$STANDBY_MODE" != "never" ]; then
    echo "standby_mode: $STANDBY_MODE" >> "$INSTALL_CFG/config.yaml"
  fi
  if [ -n "$FS_YAML" ]; then
    echo "" >> "$INSTALL_CFG/config.yaml"
    echo "$FS_YAML" >> "$INSTALL_CFG/config.yaml"
  fi
  success "Config written."
fi

# ---------------------------------------------------------------------------
# Platform-specific service installation
# ---------------------------------------------------------------------------
if [ "$PLATFORM" = "linux" ]; then
  install_linux_service
elif [ "$PLATFORM" = "darwin" ]; then
  install_macos_service
fi

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
info "Waiting for agent to start..."
HEALTH_OK=false
HEALTH_CURL="curl -sf http://localhost:$PORT/api/health"
if [ -n "$TOKEN" ]; then
  HEALTH_CURL="curl -sf -H \"Authorization: Bearer $TOKEN\" http://localhost:$PORT/api/health"
fi
for i in 1 2 3 4 5; do
  sleep 2
  if eval "$HEALTH_CURL" &>/dev/null; then
    HEALTH_OK=true
    success "Health check passed — agent is running!"
    break
  fi
  if [ "$i" -eq 5 ]; then
    warn "Health check didn't respond after 10s."
    if [ "$PLATFORM" = "linux" ] && [ -d /run/systemd/system ]; then
      warn "Check logs: journalctl -u smartha-agent -f"
    else
      warn "Check logs: tail -f /var/log/smartha-agent.log"
    fi
  fi
done

# ---------------------------------------------------------------------------
# Post-install summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}  ── Agent Summary ──────────────────────────────${NC}"
echo ""

# Detect IP for display — prefer the mDNS-advertised interface so
# the summary matches what Home Assistant will actually connect to.
_AGENT_IP=""
if [ -n "$ADV_IFACE" ] && [ "$ADV_IFACE" != "all" ]; then
  # macOS/BSD: ifconfig is reliable and avoids grep -P (Perl regex, Linux-only).
  _AGENT_IP=$(ifconfig "$ADV_IFACE" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
  # Linux fallback: ip command with portable grep.
  [ -z "$_AGENT_IP" ] && _AGENT_IP=$(ip -4 addr show "$ADV_IFACE" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
fi
[ -z "$_AGENT_IP" ] && _AGENT_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$_AGENT_IP" ] && _AGENT_IP=$(ifconfig 2>/dev/null | grep -oE 'inet [0-9.]+' | grep -v '127.0.0.1' | head -1 | awk '{print $2}')
[ -z "$_AGENT_IP" ] && _AGENT_IP="localhost"

if [ "$HEALTH_OK" = "true" ]; then
  echo -e "  ${GREEN}✓${NC} Status:         ${GREEN}running${NC}"
else
  echo -e "  ${RED}✗${NC} Status:         ${RED}not responding${NC}"
fi
echo -e "  ${GREEN}✓${NC} Port:           ${PORT}"
echo -e "  ${GREEN}✓${NC} IP:             ${_AGENT_IP}"
echo -e "  ${GREEN}✓${NC} Endpoints:      http://${_AGENT_IP}:${PORT}"
echo "                    /api/health"
echo "                    /api/drives"
echo "                    /api/drives/{id}"
if [ -n "$FS_YAML" ] && [ -n "$FS_DISPLAY" ]; then
  echo "                    /api/filesystems"
fi
if [ -n "$TOKEN" ]; then
  echo -e "  ${GREEN}✓${NC} Auth:           enabled"
else
  echo -e "  ${YELLOW}○${NC} Auth:           disabled"
fi
echo -e "  ${GREEN}✓${NC} Scan interval:  ${SCAN_INTERVAL}"
# mDNS — show the actual instance name the agent will advertise.
_MDNS_HOSTNAME=$(hostname 2>/dev/null)
_MDNS_HOSTNAME="${_MDNS_HOSTNAME%%.*}"  # strip domain suffix
_MDNS_INSTANCE="smartha-${_MDNS_HOSTNAME}"
if [ -n "$ADV_IFACE" ]; then
  echo -e "  ${GREEN}✓${NC} mDNS:           ${_MDNS_INSTANCE}._smartha._tcp.local. (${ADV_IFACE})"
else
  echo -e "  ${GREEN}✓${NC} mDNS:           ${_MDNS_INSTANCE}._smartha._tcp.local. (all physical)"
fi

# SMART drives — query the running agent if possible.
_DRIVE_CURL="curl -sf http://localhost:$PORT/api/drives"
if [ -n "$TOKEN" ]; then
  _DRIVE_CURL="curl -sf -H \"Authorization: Bearer $TOKEN\" http://localhost:$PORT/api/drives"
fi
_DRIVE_JSON=$(eval "$_DRIVE_CURL" 2>/dev/null || true)
if [ -n "$_DRIVE_JSON" ]; then
  _DRIVE_COUNT=$(echo "$_DRIVE_JSON" | grep -o '"id"' | wc -l)
  _DRIVE_NAMES=$(echo "$_DRIVE_JSON" | sed -n 's/.*"model" *: *"\([^"]*\)".*/\1/p' | awk '{printf "%s%s", sep, $0; sep=", "} END{print ""}')
  if [ "$_DRIVE_COUNT" -gt 0 ]; then
    echo -e "  ${GREEN}✓${NC} SMART drives:   ${_DRIVE_COUNT} detected"
    if [ -n "$_DRIVE_NAMES" ]; then
      echo "                    ${_DRIVE_NAMES}"
    fi
  else
    echo -e "  ${YELLOW}○${NC} SMART drives:   none detected"
  fi
else
  echo -e "  ${YELLOW}○${NC} SMART drives:   (could not query)"
fi

# Disk usage monitoring.
if [ -n "$FS_YAML" ] && [ -n "$FS_DISPLAY" ]; then
  echo -e "  ${GREEN}✓${NC} Disk usage:     ${FS_DISPLAY}"
else
  echo -e "  ${YELLOW}○${NC} Disk usage:     disabled"
fi

echo -e "  ${GREEN}✓${NC} Config:         ${INSTALL_CFG}/config.yaml"
echo ""

