# OpenWrt

**OpenWrt is community supported.** The agent runs on it, but the installer does
not support it and nobody has tested a full install end to end. These steps are
written from how OpenWrt works, not from a verified run. If you follow them,
please report back on [#51](https://github.com/DAB-LABS/smart-sniffer/issues/51)
so this page can be corrected.

## Why the installer stops

The installer sets up a background service so the agent survives a reboot. It
knows two ways to do that: systemd on Linux, launchd on macOS. OpenWrt uses a
third, procd, and the installer has never heard of it. It also assumes `bash`
and `/usr/local/bin`, neither of which OpenWrt has by default.

Rather than get halfway and fail somewhere confusing, the installer detects
OpenWrt and stops with a link here.

The agent binary itself is fine. It is statically linked with no libc
dependency, so it runs on OpenWrt without anything extra.

## What you need

- OpenWrt on x86-64 or ARM64. The agent is not built for MIPS, so most consumer
  routers are out
- Enough free space for the agent, roughly 8 MB, plus smartmontools
- Drives that actually report SMART data. Most USB enclosures do not

## Step 1: Install smartmontools

The agent is a wrapper around `smartctl` and cannot do anything without it.

```sh
opkg update
opkg install smartmontools
```

Check it can see your drives before going further:

```sh
smartctl --scan
smartctl -a /dev/sda
```

If `--scan` finds nothing, stop here. The agent will not do better than
smartctl does.

## Step 2: Install the agent binary

```sh
wget -O /usr/bin/smartha-agent \
  https://github.com/DAB-LABS/smart-sniffer/releases/latest/download/smartha-agent-linux-amd64
chmod +x /usr/bin/smartha-agent
```

On ARM64, use `smartha-agent-linux-arm64` instead.

`/usr/bin` rather than `/usr/local/bin`, because OpenWrt does not create
`/usr/local`.

## Step 3: Write the config

```sh
mkdir -p /etc/smartha-agent
cat > /etc/smartha-agent/config.yaml <<'EOF'
port: 9099
scan_interval: 60s
EOF
```

Add a token if the machine is reachable from anywhere you do not trust:

```yaml
port: 9099
scan_interval: 60s
token: "choose-something-long"
```

You will enter the same token when adding the integration in Home Assistant.

## Step 4: Create the procd service

Save this as `/etc/init.d/smartha-agent`:

```sh
#!/bin/sh /etc/rc.common

USE_PROCD=1
START=95
STOP=10

start_service() {
    procd_open_instance
    procd_set_param command /usr/bin/smartha-agent \
        --config /etc/smartha-agent/config.yaml
    procd_set_param respawn
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}
```

`respawn` restarts the agent if it exits. `stdout` and `stderr` send its log to
the system log, where `logread` can find it.

Then enable and start it:

```sh
chmod +x /etc/init.d/smartha-agent
/etc/init.d/smartha-agent enable
/etc/init.d/smartha-agent start
```

## Step 5: Check it works

```sh
curl http://localhost:9099/api/health
curl http://localhost:9099/api/drives
```

Health should report a version and a drive count. If `/api/drives` is empty but
`smartctl --scan` found drives, run the agent's own diagnostic:

```sh
smartha-agent --discover
```

That probes every drive the OS exposes and says what the agent will see at
runtime. Paste its output into an issue if you need help.

## Step 6: Add it in Home Assistant

Install the integration through HACS as normal, then add it pointing at the
OpenWrt machine's LAN address on port 9099.

Automatic discovery over mDNS may not work on OpenWrt depending on whether
`umdns` is installed and how your firewall zones are set. Adding the host
manually always works.

## Known rough edges

**No drive picker.** The installer normally offers to exclude drives you do not
want polled. Doing it by hand means writing `exclude_devices` into the config
yourself.

**No disk usage monitoring unless you configure it.** The installer's picker
writes the `filesystems:` block. Add it manually if you want it.

**Updating is manual.** Repeat step 2 with the new release, then
`/etc/init.d/smartha-agent restart`. There is no update command.

**Firewall.** OpenWrt firewalls its LAN interfaces more aggressively than a
typical distro. If Home Assistant cannot reach port 9099, add a traffic rule
allowing it from your LAN zone.

## Uninstalling

```sh
/etc/init.d/smartha-agent stop
/etc/init.d/smartha-agent disable
rm -f /etc/init.d/smartha-agent /usr/bin/smartha-agent
rm -rf /etc/smartha-agent
```

## What would make this supported

A procd init script confirmed working on a real OpenWrt box, and a report of
which steps above were wrong. That is genuinely most of the work. Post on
[#51](https://github.com/DAB-LABS/smart-sniffer/issues/51).
