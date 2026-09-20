# AIT general (miscellaneous) tools

Small, standalone tools that don't warrant their own repo. Each one is
independent -- see its section below for what it does, what it needs, and
how to configure it.

| Name          | Description                                          |
|---------------|-------------------------------------------------------|
| sc-monitor.py | Monitor telemetry, alert/act on out-of-range values  |

## sc-monitor.py

Reads a config file that lists telemetry points to monitor, along with an
optional upper and/or lower limit for each. When a monitored value goes at
or beyond a configured limit, the GUI flashes that row red, an alert is
sent to Slack, and an optional action script can be run.

### Requirements

- Python 3 with `pyyaml`, `requests`, and `tkinter`
- `telem-csv` available on `PATH` (used to fetch telemetry values)
- The `Components.power_supply_control` module (only needed if run with
  `+p`/power-supply monitoring enabled)

### Usage

```
sc-monitor.py -c <config file> [options]
```

Run `sc-monitor.py --help` for the full list of command-line options
(config file path, Slack settings, power-supply monitoring, etc).

### Config file

```yaml
telem:
    payload.pld2.sensor_temperature:
        type        : float
        threshold   : 4.0
        action      : /path/to/script    # optional, see below
    rw_speed:
        type        : float
        upper_limit : 600
        lower_limit : -600
alert:
    webhook         : <webhook URL>
    slack_delay     : 45    # seconds between alerts if condition persists
    reset_seconds   : 32    # seconds below threshold to reset alert condition
general:
    reboot_popup    : true  # show an OK popup when a target reboot is detected
    topmost         : true  # keep the main window and popups in front of others
    detect_reboot   : true  # monitor for target reboots
```

Each `telem` entry accepts `upper_limit` and/or `lower_limit` (both
optional). `threshold` is an older, equivalent name for `upper_limit`,
kept for backward compatibility with existing config files.
