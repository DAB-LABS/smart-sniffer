# SMART Sniffer — Examples

Community-contributed automations for SMART Sniffer. Copy, paste, adapt.

Update entity names to match your setup — find yours under **Settings → Devices & Services → SMART Sniffer**.

Have one to share? Open a PR or post it in the [community thread](https://community.home-assistant.io/t/s-m-a-r-t-smartctl-in-haos/869345).

## Automations

### Alert via Telegram when drive health changes

Fires when `sensor.haos_ssd_attention_needed` leaves the "NO" state and sends the attention reasons via Telegram. Swap `telegram_bot.send_message` for your notification service.

```yaml
- id: ssd_alert_telegram
  alias: "SSD Alert"
  mode: single
  max_exceeded: silent
  triggers:
    - trigger: state
      entity_id: sensor.haos_ssd_attention_needed
      not_to:
        - "NO"
  actions:
    - action: telegram_bot.send_message
      data:
        chat_id: !secret telegram_alert_chat_group_id
        title: "⚠️<b>SSD Alert</b>"
        message: >
          The System SSD has encountered a problem:
          {{ states('sensor.haos_ssd_attention_reasons') }}
```

📄 [ssd-alert-telegram.yaml](automations/ssd-alert-telegram.yaml) · by [tom_l](https://community.home-assistant.io/u/tom_l)
