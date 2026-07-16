# Examples

Ready-to-paste Home Assistant snippets for the Dragonfly integration.

| Folder | Contents |
|---|---|
| [`automations/`](automations/) | YAML automations — copy them into your `automations.yaml` or paste them into the Automation editor in **raw editor** mode. |
| [`dashboards/`](dashboards/) | Lovelace snippets, including [`add_parcel_card.yaml`](dashboards/add_parcel_card.yaml) — track a new parcel straight from a dashboard via the `dragonfly.track_parcel` service. |

All examples assume a single Dragonfly hub. Adjust entity IDs to match yours.

## Services

| Service | Description |
|---|---|
| `dragonfly.track_parcel` | Start tracking a parcel (`tracking_code`). |
| `dragonfly.untrack_parcel` | Stop tracking a parcel (`tracking_code`). |

## Events used in the examples

The coordinator fires these on the HA event bus:

| Event | When | Payload |
|---|---|---|
| `dragonfly_parcel_registered` | A new parcel appears in the active list | The full normalised parcel dict |
| `dragonfly_parcel_status_changed` | A parcel's canonical status changes | Same, plus `old_status` / `new_status` |
| `dragonfly_parcel_delivery_time_changed` | A parcel's expected delivery time changes | Same, plus `old_planned_from` / `new_planned_from` / `old_planned_to` / `new_planned_to` |

Events are suppressed on the first refresh after start-up.
