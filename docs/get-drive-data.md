# The `smart_sniffer.get_drive_data` action

Returns one agent's cached data as a response, for scripts, automations and
AI assistants. It never asks the agent anything; it reads what the last poll
brought.

```yaml
action: smart_sniffer.get_drive_data
data:
  config_entry_id: 01KRS6Q8KDEN7SQJFHWCZAQ6EM
response_variable: drives
```

`config_entry_id` is the agent's entry (pick it in the action editor). The
action is always registered. When no SMART Sniffer agent is loaded, or the
entry named is not loaded, it fails with a message that says which.

## Response

Keys are only added within 1.x; none is removed or renamed before 2.0.

```text
agent:
  name             entry title, for example "SMART Sniffer (nas)"
  host             the agent's address
  os               linux, darwin, windows, ...
  version          agent version
  uptime_seconds   seconds since the agent started
  uptime           the same number (deprecated name, removed in 2.0)
drives:            one per drive, keyed by drive id
  <drive id>:
    model, serial, protocol, device_path
    attention:
      state        yes, maybe, no or unsupported
      severity     critical, warning or none
      reasons      list of strings, [] when there is nothing to report
      accepted     list of strings: readings over a limit you raised
    smart_data     smartctl -a --json as the agent sent it
    device_statistics, derived   agent 0.8 and later, else null
filesystems:       one per monitored mountpoint
  - mountpoint, total_bytes, used_bytes, use_percent
    (sizes are null when the agent could not read the mountpoint)
```

`smart_data` and `device_statistics` are the agent's own documents; see
[agent-api.md](agent-api.md).
