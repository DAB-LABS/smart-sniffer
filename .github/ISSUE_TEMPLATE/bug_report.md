---
name: Bug Report
about: Something isn't working right
title: ""
labels: bug
assignees: ""
---

**Describe the bug**
A clear description of what's happening.

**Expected behavior**
What should happen instead.

**Environment**
- **Home Assistant version:**
- **SMART Sniffer integration version:**
- **Agent version:** (check with `curl http://<host>:9099/api/health`)
- **Agent OS:** (e.g., Proxmox, Ubuntu, macOS, Windows)
- **Drive type:** (ATA/SATA SSD, ATA/SATA HDD, NVMe, USB)

**Drive info (if relevant)**
Paste the output of `smartctl -a /dev/sdX --json` or `smartctl -a /dev/sdX` for the affected drive. This helps us catch manufacturer-specific quirks.

**Logs**
Relevant logs from Home Assistant (`Settings → System → Logs`, filter for `smart_sniffer`) and/or the agent (`journalctl -u smartha-agent`).

**Screenshots**
If applicable, screenshots of the HA device/entity page showing the issue.
