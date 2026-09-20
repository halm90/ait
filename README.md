# AIT general (miscellaneous) tools

| Name               | Description                                               |
|--------------------|-----------------------------------------------------------|
| sc-monitor.py      | monitor telemetry, take action on threshold events        |

## sc-monitor.py

Reads a config file that lists telemetry points to monitor, high and low
thresholds, and action to take if a telemetry value goes over high limit
or below low limit.

General format of the config file is as in this sample:
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
