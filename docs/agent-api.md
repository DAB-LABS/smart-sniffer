# The agent's HTTP API (api_version 1)

The SMART Sniffer agent serves a small read-only JSON API, by default on port
9099. This page is the reference for `api_version` 1, the API of agent 1.0.
Home Assistant's integration is its main client, but anything that speaks
HTTP can read it.

## The promise

`/api/health` reports `api_version`, an integer (`1`). It changes only with a
breaking change: a key removed or renamed, a key's type or meaning changed,
an endpoint removed. New keys, new endpoints and new error codes are not
breaking and do not change it, so a client should ignore keys it does not
know.

## Requests

- **GET and HEAD only.** Any other method is answered `405` with
  `Allow: GET, HEAD`. HEAD answers exactly as GET would, without the body.
- **Password.** With a `token` set, every request but `/api/health` needs
  `Authorization: Bearer <token>`. After 5 wrong or missing passwords in a
  minute from one address, the agent answers `429` with `Retry-After` (in
  seconds) for up to a minute at a time, without checking the password. An
  address that only sends the right password is never slowed down.

## Errors

Every answer that is not 2xx is `application/json`:

```json
{"error": "not_found", "message": "No drive with this id on this agent."}
```

`error` is a stable code; `message` is for people and may be reworded.

| Status | `error` | When |
|---|---|---|
| 401 | `unauthorized` | Missing or wrong password. |
| 404 | `not_found` | No such endpoint, or no such drive. An address without the password gets 401 first, so it cannot list what exists. |
| 405 | `method_not_allowed` | A method other than GET or HEAD. |
| 429 | `too_many_attempts` | Too many wrong passwords from this address; see `Retry-After`. |
| 503 | `pools_unavailable` | `/api/pools` before `zpool status` was read successfully, or after a failed read. "Not read" never looks like "no pools". |

Agents before 1.0 answered some errors with other bodies (plain text, or
`{"error": "drive not found"}`); go by the status code first.

## `GET /api/health`

Never needs the password.

| Key | Type | Meaning |
|---|---|---|
| `status` | string | Always `ok`. |
| `version` | string | Agent version, for example `1.0.0`. |
| `api_version` | integer | `1`. See [The promise](#the-promise). |
| `os` | string | `linux`, `darwin`, `windows`, ... |
| `hostname` | string | Short host name. Omitted when it cannot be read. |
| `os_name`, `os_version` | string | Friendly OS name and version. Omitted when unknown. |
| `uptime_seconds` | integer | Seconds since the agent started. |
| `scan_interval` | integer | The scan interval in use, in whole seconds. |
| `endpoints` | list of strings | The endpoints this agent serves. |
| `drives` | integer | Drives in the current list. |
| `filesystems` | integer | Filesystems configured. `/api/filesystems` is served only when above 0. |
| `pools` | integer | ZFS pools read. Only when pool status is on and the last read succeeded. |
| `pools_status` | string | `pending`, `ok` or `failed`. Only when pool status is on. |
| `agent_id` | string | The agent's random ID. Omitted when it could not be saved. |
| `container` | boolean | `true` in a container (Docker, Podman). Omitted otherwise. |

## `GET /api/drives` and `GET /api/drives/{id}`

The list gives each drive's `id`, `device_path`, `model`, `serial`,
`protocol` and `readable` (`false`: smartctl could not read it in the last
cycle and the data is the last good reading), plus `smart_available` when
known. `/api/drives/{id}` adds `smart_data` (the output of `smartctl -a
--json`, as smartctl wrote it), `last_updated` (RFC 3339), `in_standby`
(`true` when the drive was not woken this cycle), and, from agent 0.8,
`device_statistics` and `derived`. An unknown id is `404 not_found`.

## `GET /api/filesystems`

A list, one entry per configured mountpoint, in config order.

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Stable id, see below. |
| `uuid` | string | Filesystem UUID from the config (may be empty). |
| `mountpoint`, `device` | string | As configured. |
| `fstype` | string | The filesystem type as configured (`ext4`, `btrfs`, `zfs`, `NTFS`...). It selects the agent's fallbacks for btrfs and ZFS. |
| `total_bytes`, `used_bytes`, `available_bytes` | integer or null | Sizes in bytes. `available_bytes` is what an ordinary user can still write (reserved blocks excluded). |
| `use_percent` | number or null | `used / (used + available)`, as `df` shows it, one decimal. |
| `status` | string | `ok`, or `unavailable` when the mountpoint could not be read. |
| `physical_devices`, `drive_id` | list, string | macOS only: the physical disks under it, and its drive's id when it is on exactly one. |

With `status: unavailable` the four size fields are `null`, never 0, so an
unknown size never reads as an empty disk. (Agents before 1.0 sent 0.)

**Ids.** `fs-` and the first 8 characters of the UUID, or `fs-` and a slug
of the mountpoint when there is no UUID. Btrfs subvolumes and bind mounts
share their filesystem's UUID, so two entries can start with the same id.
Then the first in config order keeps the plain id (the one Home Assistant
has always followed, so its sensor never changes), and each later one gets
`-` and 6 hex characters of the SHA-1 of its mountpoint, the same on every
run. (Agents before 1.0 sent the same id twice; Home Assistant logs one
warning and shows only the first.)

## `GET /api/pools`

Only when ZFS pool status is on and `zpool` is found (see `endpoints`). A
list of pools:

| Key | Type | Meaning |
|---|---|---|
| `name`, `state` | string | As `zpool status` prints them (`ONLINE`, `DEGRADED`, `FAULTED`...). |
| `status`, `action` | string or null | zpool's own text. |
| `read_errors`, `write_errors`, `checksum_errors` | integer | Totals over the pool's devices. |
| `data_errors` | integer or null | Files with permanent errors; null when unknown. |
| `scan_function` | string or null | `SCRUB`, `RESILVER`, `ERRORSCRUB` or `NONE`; null when no scan ever ran. |
| `scan_state` | string or null | `SCANNING`, `FINISHED` or `CANCELED`. |
| `scrub_in_progress` | boolean | Also true while a scrub is paused. |
| `last_scrub_end` | string or null | RFC 3339 UTC end of the last completed scrub. |
| `last_scrub_repaired` | integer or null | Bytes repaired by that scrub. |
| `last_scrub_errors` | integer or null | Errors that scrub found. |
| `problem_vdevs` | list | Devices not ONLINE or with errors: `name`, `type`, `state`, the three error counts. |
| `vdevs`, `logs`, `cache`, `spares`, `special`, `dedup` | list | The layout. `cache` is the L2ARC devices. Each device: `name`, `type`, `state`, error counts, `path` for a disk, `children` for a group. Empty groups are omitted. |
| `layout_truncated` | boolean | Present and true when a very large layout was cut short. |

Device states stay as zpool prints them (upper case) in this API.

## Configuration

Every config key, flag and environment variable is in the README's
"Agent configuration" table. The "smartha" names (`smartha-agent`,
`SMARTHA_*`, `/etc/smartha-agent`, `_smartha._tcp`) are permanent.
