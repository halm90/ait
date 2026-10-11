#!/usr/bin/python3
"""
    Telemetry (and power supply) monitoring

    This was originally intended to monitor battery voltage during charge testing, and
    later expanded to monitor any telemetry. If the monitored value exceeds threshold
    then the GUI flashes that item red and sends a message to a dedicated slack channel.

    Any telemetry item can be monitored by adding an entry for it into a YAML config file.

    The config file looks like this:
    ---
    telem:
        payload.pld1.sensor_temperature:
            type        : float
            threshold   : 4.0           # deprecated synonym for 'upper_limit', see below
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
        slack_delay     : 45        # seconds between alerts if condition persists
        reset_seconds   : 32        # seconds below threshold to reset alert condition
    general:
        reboot_popup    : true      # show an OK popup when a target reboot is detected
        topmost         : true      # keep the main window and popups in front of others
        detect_reboot   : true      # monitor health.service.boot_id for target reboots
    ---
    The webhook currently defaults to the "tvac-test-alerts" slack channel.

    Target reboot detection:
        "health.service.boot_id" is always monitored (unless general.detect_reboot is
        set false), regardless of what's in the 'telem' config section -- it changes
        any time the remote target system reboots. It is never shown as an ordinary
        row; instead a status line above the table reads "Last reboot: <date/time>"
        (or "Last reboot: unknown" until one is observed). When a change is detected,
        all pending alarms/warnings are cleared (they describe conditions on the
        previous boot) and, unless general.reboot_popup is set false, an
        acknowledgement popup is shown. Only a KNOWN boot_id changing to a different
        KNOWN value counts as a detected reboot -- learning the boot_id for the first
        time (e.g. telemetry wasn't flowing yet at startup) is just establishing the
        baseline, not a reboot, and does not pop up an alert. Also, a single physical
        reboot can sometimes cause more than one boot_id change in quick succession
        (e.g. health.service itself bouncing once during the target's own startup) --
        any further change within REBOOT_DEBOUNCE_SEC (default 10s) of an already-
        reported reboot is treated as the same event and does not pop up again.

    Telemetry status indicator:
        A "Telemetry" / "No Telemetry" indicator sits at the top right, alongside
        "Last reboot", turning green/red to match. telem-csv is called with a
        TELEM_CALL_TIMEOUT_SEC (default 2s) timeout, since it can otherwise hang
        indefinitely if the upstream host process is alive but the target has gone
        quiet (e.g. mid-reboot) -- without this, the whole GUI would freeze for the
        duration rather than showing a "No Telemetry" state. Once telemetry has been
        received at least once, a brief gap (up to TELEM_LOSS_GRACE_SEC, default 10s)
        is tolerated without flipping the display -- only a gap that persists longer
        counts as an actual loss for display purposes (values show "unk" rather than
        a frozen stale reading). Alarm/threshold evaluation is stricter: it always
        uses the raw per-cycle result, not the grace-windowed one.

    Optional per-telem-point 'action' field:
        Path to an executable script/command to run (via subprocess.Popen,
        non-blocking) each time that telem point ENTERS an alarm episode
        (transitions from normal/warning into over-threshold). It is invoked
        as:
            <action> <telem_name> <value> <limit>
        <limit> is the configured upper_limit if one is set, otherwise the
        lower_limit. Only fires once per alarm episode, same as the Slack alert -- not on
        every refresh cycle while still over threshold. A small popup shows
        while the script runs and closes automatically once it exits.

    Operator "Clear Warnings" button:
        Clears every row currently in the warning (yellow) state -- i.e.
        was over threshold and has since returned to normal, but hasn't
        aged past reset_seconds yet. Rows still actively alarming (red,
        flashing) are left untouched; an active alarm can only be cleared
        once it drops back below threshold and ages into the warning state.

    All fields are optional except for:
        - the 'telem' block start and the telem name(s)

    'threshold' / 'upper_limit' / 'lower_limit':
        'threshold' only ever triggered an alarm when a value rose too high. Some
        telemetry (e.g. a signed rate or speed) can also go too far negative, so
        'upper_limit' and 'lower_limit' are the preferred names now: an alarm
        triggers when the value is >= upper_limit or <= lower_limit. 'threshold' is
        kept as a synonym for 'upper_limit' so existing config files keep working
        unchanged -- if both 'threshold' and 'upper_limit' are given for the same
        point, 'upper_limit' wins. All three remain optional.
"""
#pylint: disable=invalid-name, broad-except

import  argparse
import  json
import  os
import  subprocess
import  sys
import  time
from    tkinter                         import  ttk
import  tkinter                         as      tk
from    typing                          import  Union
import  yaml
import  requests

from    Components.power_supply_control import DCSupply, PS_Object

DEFAULT_SLACK_MESSAGE_PAUSE = 45
DEFAULT_OVERLIMIT_RESET     = 30


class InvalidArgumentError(Exception):
    pass


class Defaults:
    """ Assorted command line option default values """
    default_configs = {'verbose'        : False,
                       'power'          : False,
                       'slack'          : False,
                       'slack_url'      : 'https://hooks.slack.com/services/T026D9E55/' + \
                                          'B0A6Q8TTJNB/MXeQa3O9F50Y3KaZM9NMrqSm',
                       'slack_delay'    : DEFAULT_SLACK_MESSAGE_PAUSE,
                       'reset_seconds'  : DEFAULT_OVERLIMIT_RESET,
                       'vlimit'         : 32.05,
                       'ilimit'         : None,
                       'reboot_popup'   : True,
                       'topmost'        : True,
                       'detect_reboot'  : True,
                      }

# # #
# Constants
DISPLAY_PERIOD  = 500          # display refresh milliseconds

# Always monitored, regardless of what the config file specifies: changes any time the
# remote target system reboots, used to detect a reboot and clear stale alarm/warning state.
BOOT_ID_KEY = "health.service.boot_id"

# A single physical reboot can sometimes cause boot_id to change more than once in quick
# succession (e.g. health.service itself bouncing once during the target's own startup
# sequence). Any further boot_id change within this many seconds of an already-reported
# reboot is treated as part of the SAME event, not reported again.
REBOOT_DEBOUNCE_SEC = 10

# telem-csv can hang indefinitely waiting for the next sample if the upstream host
# process is alive but the target itself has gone quiet (e.g. mid-reboot). Since the
# call happens synchronously on the tkinter main thread, an unbounded wait would freeze
# the entire GUI for the duration. Cap it -- a timeout is treated the same as "nothing
# received" this cycle.
TELEM_CALL_TIMEOUT_SEC = 2

# Once telemetry has been received at least once, a brief gap (a single missed cycle,
# a moment of jitter) shouldn't flip the display to "No Telemetry"/unk -- only a gap
# that persists this long is treated as an actual loss for DISPLAY purposes. Alarm/
# threshold evaluation is unaffected by this and still uses the raw per-cycle result.
TELEM_LOSS_GRACE_SEC = 5


POWER = {"v_out"      : {"type": float, "threshold": None},
         "i_out"      : {"type": float, "threshold": None},
         "v_prot"     : {"type": float, "threshold": None},
        }


def get_upper_limit(entry):
    """ 'upper_limit' is the preferred key; 'threshold' is kept as a synonym for it
    so existing config files keep working unchanged. If both are present,
    'upper_limit' wins. """
    return entry.get('upper_limit', entry.get('threshold'))


def get_lower_limit(entry):
    """ New, optional key -- no legacy synonym. """
    return entry.get('lower_limit')


def telem_val(names: list) -> Union[float, str, int, bool]:
    """ Use telem-csv to retrieve most recent values of selected telemetry list """
    csvargs = ["telem-csv", "--count", "1", "--no-heading", "--fields"] + names
    try:
        output = subprocess.run(csvargs, check=True, capture_output=True, text=True,
                                timeout=TELEM_CALL_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return []
    except subprocess.CalledProcessError as exn:
        print(f"{time.time()}: telem-csv exited with an error: {exn}")
        return []
    # This was as follows to get values for those in the names list, but that didn't handle
    # list types.
    #    vals = [val.strip(',') for val in output.stdout.strip().split()[-len(names):]]
    # We know that the response has timestamp, index and the names of the fields
    # so we do a little math instead to find where in the output to get the values.
    # We start with index 1 to skip over the timestamp
    #
    # 'check=True' allows this to succeed even if nothing was received. We don't
    # check for stderr, simply return an empty list and handle it in the caller.
    vals    = [val.strip(',') for val in output.stdout.strip().split()][1:]
    return vals

# # #
# Objects
#
class SlackAlert():
    """ Temporary object attached to a telem value if there's an active or pending alarm
    """
    def __init__(self, cfg, name, upper_limit, lower_limit):
        self.config = cfg

        # Parameter name and limit(s) for this alert
        self.name               = name
        self.upper_limit        = upper_limit
        self.lower_limit        = lower_limit

        # last time we slacked about this
        self.last_alert_ts      = None

        # next time we'll slack about this
        self.next_alert_ts      = None

        # last time ok after first alert
        self.val_ok_ts          = None


    def send_alert(self, value):
        if self.config['slack_url']:
            now = time.time()

            if not self.last_alert_ts or now >= self.next_alert_ts:
                # time to (re)send the alert
                limits = []
                if self.upper_limit is not None:
                    limits.append(f"upper limit {self.upper_limit}")
                if self.lower_limit is not None:
                    limits.append(f"lower limit {self.lower_limit}")
                slack_msg = {'text': f"{self.name} value {value} is outside its " + \
                                     " / ".join(limits)}
                resp      = requests.post(self.config['slack_url'], data = json.dumps(slack_msg),
                                          headers = {'Content-Type': 'application/json'})
                if resp.status_code != 200:
                    raise ValueError(f"Slack request returned error {resp.status_code}: " + \
                                     f"{resp.text}")
                self.last_alert_ts  = now
                self.next_alert_ts  = now + self.config['slack_delay']
            else:
                # to soon to (re)send the alert
                pass


class DisplayInfo(dict):
    """
    Dictionary to maintain current values read from the PS or telemetry (or other source)
    """
    def __init__(self, args):
        super().__init__()
        self.args   = args

        # To add a non-telemetry item for monitoring, add it to POWER above and then add any
        # special handling here (ie: threshold) and populate the value(s) in 'info_update' below
        if self.args['power']:
            self.supply = DCSupply()
            self.ps_obj = PS_Object()
            for k, v in POWER.items():
                self[k] = v

        extra             = {"value": None, "tag": None}
        self.telem_points = []
        for k, v in self.args["telem_points"].items():
            self[k] = v
            self[k] = {**self[k], **extra}
            self.telem_points.append(k)

        # Precompute how many raw values we expect back from telem-csv, so info_update()
        # can detect a PARTIAL response (some fields present, fewer than expected) and
        # skip the update entirely, the same as a fully empty response. Without this, a
        # partial response still passes the "non-empty" check but can walk off the end of
        # telem_vals with an uncaught IndexError -- which would kill the periodic update
        # loop for good, since it happens before update_display() reaches the line that
        # reschedules itself.
        self.expected_val_count = 0
        for k in self.telem_points:
            if self[k].get("type") == "list":
                self.expected_val_count += self[k].get("list_len", 1)
            else:
                self.expected_val_count += 1

        self.info_update()

        if hasattr(self, "v_out"):
            self['v_out']['threshold']      = args['vlimit']
            self['i_out']['threshold']      = args['ilimit']


    def info_update(self):
        """ This is the key function: it updates values by retrieving from external sources """
        if self.args['power']:
            self['v_out']["value"], self['i_out']["value"] = self.supply.get_power_supply_values()
            self['v_prot'] = self.ps_obj.get_voltage_protection()

        telem_vals = telem_val(self.telem_points)
        has_telem  = bool(telem_vals) and len(telem_vals) >= self.expected_val_count
        if has_telem:
            val_ndx = 0
            for k in self.telem_points:
                tel_type = self[k].get("type", "float")
                if tel_type == 'list':
                    list_len = self[k].get("list_len")
                    assert list_len, f"{k} list type must specify 'list_len'"
                    lst_type = self[k].get("list_type", "float")
                    rng_nd   = val_ndx + list_len
                    self[k]['value'] = [eval(lst_type)(telem_vals[i]) for i in range(val_ndx, rng_nd)]
                    val_ndx = rng_nd
                else:
                    self[k]['value'] = eval(tel_type)(telem_vals[val_ndx]) # pylint: disable=eval-used
                    val_ndx = val_ndx + 1
        return has_telem



class InfoDisplay:
    """
    tkinter main display object
    """
    columns = {"Parameter"  : "Parameter Name",
               "Value"      : "Parameter Value"}


    def __init__(self, root, args):
        """
        Set up the display. Rows, columns, headings
        """
        self.args          = args
        self.disp_info     = DisplayInfo(args)
        self.root          = root
        self.vlim_key      = None
        self.active_action = None

        # 'general' config settings, all default True
        self.topmost       = self.args.get('topmost', True)
        self.detect_reboot = self.args.get('detect_reboot', True) and (BOOT_ID_KEY in self.disp_info)

        self.root.title(self.args['title'])
        if self.topmost:
            self.root.attributes('-topmost', True)     # keep main window in front

        # --- Main layout: status bar, center = scrollable tree, bottom = buttons ---
        center = ttk.Frame(root)
        center.pack(fill="both", expand=True)

        # Status bar: last detected target reboot (stays "unknown" until one is observed,
        # or forever if detect_reboot is disabled), plus a telemetry-received indicator.
        status_frame = ttk.Frame(center)
        status_frame.pack(fill="x", padx=10, pady=(10, 0))
        self.reboot_var = tk.StringVar(value="Last reboot: unknown")
        ttk.Label(status_frame, textvariable=self.reboot_var).pack(side=tk.LEFT)

        self.telem_status_var = tk.StringVar(value="No Telemetry")
        self.telem_status_label = tk.Label(status_frame, textvariable=self.telem_status_var,
                                           background="red", foreground="white",
                                           padx=6)
        self.telem_status_label.pack(side=tk.RIGHT)

        # Scrollable Treeview
        tree_frame = ttk.Frame(center)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.tree = ttk.Treeview(tree_frame,
                                 columns=list(self.columns.keys()),
                                 show="headings")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal",
                            command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set,
                            xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        # Configure headings/columns
        for k, v in self.columns.items():
            self.tree.heading(k, text=v)

        # Estimate column widths based on longest parameter name (boot_id is never
        # displayed as a row -- see below -- so it's excluded here too)
        display_keys   = [k for k in self.disp_info.keys() if k != BOOT_ID_KEY]
        longest_name = max((len(k) for k in display_keys), default=10)
        # rough char -> pixel estimate; adjust factor as needed
        name_col_width  = max(120, longest_name * 8)

        #value_col_width = 150
        longest_val     = max([self.disp_info[k].get('list_len', 1) for k in display_keys],
                              default = 1)
        value_col_width = max(150, (longest_val * 8 * 8))

        self.tree.column("Parameter", anchor=tk.W, width=name_col_width, stretch=True)
        self.tree.column("Value", anchor=tk.CENTER, width=value_col_width, stretch=True)

        # Tags for flashing
        self.tree.tag_configure("red",    background="red",    foreground="white")
        self.tree.tag_configure("yellow", background="yellow", foreground="black")
        self.tree.tag_configure("green",  background="green",  foreground="white")
        self.tree.tag_configure("normal", background="",       foreground="")

        # Initialize data rows. BOOT_ID_KEY is fetched every cycle (see DisplayInfo) but is
        # never shown as an ordinary row -- its value feeds the "Last reboot" status bar
        # above instead.
        for k, v in self.disp_info.items():
            if k == BOOT_ID_KEY:
                continue
            row_id = self.tree.insert("", tk.END,
                                      values=(k, v['value']),
                                      tags=("normal",))
            self.disp_info[k]['row_id'] = row_id

        # Bottom button bar (always visible)
        btn_bar = ttk.Frame(root)
        btn_bar.pack(fill="x")
        clear_button = ttk.Button(btn_bar, text="Clear Warnings", command=self.clear_warnings)
        clear_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5, pady=5)
        quit_button = ttk.Button(btn_bar, text="Quit", command=self.quit_app)
        quit_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5, pady=5)

        # Let Tk compute natural size, then choose a reasonable initial geometry
        self.root.update_idletasks()
        req_w = tree_frame.winfo_reqwidth() + 40
        req_h = (status_frame.winfo_reqheight() + tree_frame.winfo_reqheight() +
                btn_bar.winfo_reqheight() + 40)

        # Cap height so scrollbars are used for large tables
        width = max(400, req_w)
        height = min(600, max(300, req_h))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(400, 300)

        # Cache the target's current boot_id at startup -- this is just the baseline, not
        # a detected reboot, so no popup/clear happens for this initial value. Only tracked
        # at all if the 'general.detect_reboot' config setting allows it (default True).
        self.last_boot_id  = self.disp_info[BOOT_ID_KEY]['value'] if self.detect_reboot else None
        self.last_reboot_ts = None     # wall-clock time of the last REPORTED reboot, for debounce
        self.last_telem_ts  = None     # wall-clock time telemetry was last actually received

        # Start periodic update
        self.update_display()


    def update_display(self):
        """
        Periodic update of the displayed table
        """
        now       = time.time()
        has_telem = self.disp_info.info_update()

        if has_telem:
            self.last_telem_ts = now

        # For DISPLAY purposes only (status indicator, "unk" placeholders), tolerate a
        # brief gap since the last actual receipt rather than flickering on every missed
        # cycle -- only a gap that persists past TELEM_LOSS_GRACE_SEC counts as a real
        # loss. Alarm/threshold evaluation below is unaffected and still uses the raw,
        # strict per-cycle has_telem.
        telemetry_ok = (self.last_telem_ts is not None
                        and (now - self.last_telem_ts) < TELEM_LOSS_GRACE_SEC)

        if telemetry_ok:
            self.telem_status_var.set("Telemetry")
            self.telem_status_label.configure(background="green")
        else:
            self.telem_status_var.set("No Telemetry")
            self.telem_status_label.configure(background="red")

        if self.detect_reboot:
            current_boot_id = self.disp_info[BOOT_ID_KEY]['value']
            # Only a KNOWN boot_id changing to a different KNOWN value counts as a
            # detected reboot. Going from None (never yet observed -- e.g. telemetry
            # hadn't started flowing yet at startup) to a real value is just learning
            # the baseline for the first time, not a reboot, and must not pop up an
            # alert. This was the cause of the spurious/duplicate reboot popups seen
            # at startup: the old check compared against a None baseline.
            if (current_boot_id is not None and self.last_boot_id is not None
                   and current_boot_id != self.last_boot_id):
                if (self.last_reboot_ts is None
                       or (now - self.last_reboot_ts) >= REBOOT_DEBOUNCE_SEC):
                    self.handle_reboot(self.last_boot_id, current_boot_id)
                    self.last_reboot_ts = now
                else:
                    # A single physical reboot can cause more than one boot_id change in
                    # quick succession (e.g. health.service itself bouncing once during
                    # the target's own startup). Treat this as the SAME event -- update
                    # the cached value and move on quietly, no second popup.
                    print(f"{now}: boot_id changed again ({self.last_boot_id} -> " +
                          f"{current_boot_id}) within {REBOOT_DEBOUNCE_SEC}s of the last " +
                          "reported reboot -- treating as the same event, not popping up again")
            if current_boot_id is not None:
                self.last_boot_id = current_boot_id

        for row_key, row_info in self.disp_info.items():
            if row_key == BOOT_ID_KEY:
                continue        # never displayed as an ordinary row
            row_val = row_info['value']

            upper_limit   = get_upper_limit(row_info)
            lower_limit   = get_lower_limit(row_info)
            is_alarming   = False
            if has_telem and (upper_limit is not None or lower_limit is not None):
                if row_info.get('type') == "list":
                    is_alarming = any((upper_limit is not None and v > upper_limit) or
                                      (lower_limit is not None and v <= lower_limit)
                                      for v in row_info['value'])
                else:
                    is_alarming = ((upper_limit is not None and row_val >= upper_limit) or
                                   (lower_limit is not None and row_val <= lower_limit))

            if is_alarming:
                if row_info.get("alert", None):
                    # Already an alert for this item
                    row_info['alert'].val_ok_ts = None
                else:
                    # This item/alert hasn't been slacked yet.  Add alert object and slack now
                    row_info['alert'] = SlackAlert(self.args, row_key, upper_limit, lower_limit)

                    # New alarm episode starting -- fire the configured action script, if
                    # any, via a helper that keeps the GUI responsive and shows a status
                    # popup while it runs.
                    action = row_info.get("action")
                    if action:
                        action       = os.path.expandvars(action)
                        action_limit = upper_limit if upper_limit is not None else lower_limit
                        self.launch_action(action, row_key, row_val, action_limit)

                # New or existing alert, send it if it's time or a new alert
                row_info['alert'].send_alert(row_val)

            elif row_info.get("alert", None):
                # Not alarming. If this item has an alert then it WAS alarming in
                # the past.  If it's been ok for long enough then remove the alert.
                now = time.time()
                ok_ts = row_info['alert'].val_ok_ts
                if ok_ts and now >= (ok_ts + self.args['reset_seconds']):
                    print(f"{now}: {row_key} value {row_val} back within limits " + \
                          f"(upper={upper_limit}, lower={lower_limit}) for " + \
                          f"{self.args['reset_seconds']} seconds")
                    del row_info['alert']
                    row_info['tag'] = 'yellow'
                elif not ok_ts:
                    # Value is now (first time) ok
                    row_info['alert'].val_ok_ts = now

            # Show parameter name and value in separate columns. When telemetry isn't
            # currently being received (or hasn't been received yet at all), show a
            # placeholder rather than silently displaying a frozen/stale value.
            if row_info.get("alert"):       # flash red if alert in progress
                row_info['tag'] = 'red' if row_info['tag'] == 'normal' else 'normal'

            if not telemetry_ok or row_info['value'] is None:
                row_val = "unk"
            elif isinstance(row_info['value'], list):
                if isinstance(row_info['value'][0], float):
                    row_val = [round(v, 4) for v in row_info['value']]
                row_val = "[" + ", ".join([str(v) for v in row_val]) + "]"
            else:
                row_val = row_info['value']
            self.tree.item(row_info['row_id'],
                           values=(row_key, row_val),
                           tags=(row_info['tag'],))

        self.root.after(DISPLAY_PERIOD, self.update_display)


    def handle_reboot(self, old_id, new_id):
        """
        Detected that the target's health.service.boot_id changed, meaning the remote
        target system rebooted. Any alarm/warning state accumulated before the reboot
        describes conditions on the PREVIOUS boot and is no longer meaningful, so it's
        cleared here (both active alarms and pending warnings -- unlike Clear Warnings,
        this clears everything, since a reboot invalidates all of it, not just resolved
        conditions).

        Optionally shows an acknowledgement popup, controlled by the 'reboot_popup'
        general config setting (default True):
            general:
                reboot_popup: false
        """
        print(f"{time.time()}: target reboot detected (boot_id {old_id} -> {new_id}), " +
              f"clearing pending warnings/alarms")

        for row_key, row_info in self.disp_info.items():
            if row_key == BOOT_ID_KEY:
                continue        # never displayed as an ordinary row, nothing to clear
            if row_info.get('alert') is not None:
                del row_info['alert']
            if row_info.get('tag') in ('red', 'yellow'):
                row_info['tag'] = 'normal'
                self.tree.item(row_info['row_id'], tags=('normal',))

        reboot_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        self.reboot_var.set(f"Last reboot: {reboot_time}")

        if self.args.get('reboot_popup', True):
            popup = tk.Toplevel(self.root)
            popup.title("Target Reboot Detected")
            popup.transient(self.root)
            if self.topmost:
                popup.attributes('-topmost', True)
            ttk.Label(popup, text="Target system reboot detected.\n" +
                                  f"boot_id changed: {old_id} -> {new_id}",
                     padding=20).pack()
            ttk.Button(popup, text="OK", command=popup.destroy).pack(pady=(0, 10))


    def launch_action(self, action, row_key, row_val, threshold):
        """
        Launches the configured action script for a newly-started alarm
        episode. subprocess.Popen() is already non-blocking -- it starts
        the process and returns immediately, so the tkinter main loop
        (flashing, update_display() re-checking every other value) keeps
        running the whole time the action script executes.

        Shows a small non-modal popup noting that the action is running,
        and automatically closes it once the script exits (polled via
        root.after so this doesn't block either). The operator can also
        dismiss it manually before the script finishes.
        """
        if self.active_action == action:
            print(f"action script {action} already in progress")
            return
        self.active_action = action

        try:
            print(f"starting action script {action}")
            proc = subprocess.Popen([action, row_key, str(row_val), str(threshold)])
        except Exception as exc:                              #pylint: disable=broad-except
            print(f"Failed to launch action '{action}' for {row_key}: {exc}")
            return

        popup = tk.Toplevel(self.root)
        popup.title("Action Running")
        popup.transient(self.root)     # associated with main window, but non-modal
        if self.topmost:
            popup.attributes('-topmost', True)
        ttk.Label(popup, text=f"Action script '{action}' is running for {row_key}...",
                 padding=20).pack()
        ttk.Button(popup, text="Dismiss", command=popup.destroy).pack(pady=(0, 10))

        def _check_done():
            if not popup.winfo_exists():
                self.active_action = None
                return          # operator already dismissed it
            if proc.poll() is None:
                self.root.after(300, _check_done)      # still running, check again shortly
            else:
                self.active_action = None
                popup.destroy()

        self.root.after(300, _check_done)


    def clear_warnings(self):
        """
        Clears every row currently in the warning (yellow) state --
        i.e. rows that WERE over threshold and have since dropped back
        below it, but haven't yet aged past reset_seconds. Rows that
        are actively alarming (red, flashing) are left untouched:
        an active alarm can't be silenced this way, it can only be
        cleared once the value itself drops back below threshold and
        ages into the warning state.

        A single global button (rather than per-row) is used here
        because ttk.Treeview doesn't support embedding real widgets
        per row. Per-row selection-based clearing (select a row, then
        clear just that one) is a reasonable future enhancement if
        needed later.
        """
        for row_key, row_info in self.disp_info.items():          #pylint: disable=unused-variable
            if row_info.get('tag') == 'yellow':
                row_info['tag'] = 'normal'
                self.tree.item(row_info['row_id'], tags=('normal',))


    def quit_app(self):
        """Closes the main application window."""
        self.root.destroy()



def main(args):
    """
    Main function, start the tkinter main loop
    """
    root = tk.Tk()
    app  = InfoDisplay(root, args)              #pylint: disable=unused-variable
    root.mainloop()


if __name__ == "__main__":
    defaults        = Defaults.default_configs

    script_nm       = os.path.basename(sys.argv[0])     # invocation name of script
    script_base     = os.path.basename(script_nm)       # name of invoked script (ie: runopts.py)
    script_title    = script_base.rsplit('.')[0] if script_base.endswith(".py") else script_base

    hpath           = os.path.expandvars("${HOME}/.ait/" + script_title)
    def_config      = hpath if os.path.isfile(hpath) else None

    parser = argparse.ArgumentParser(description='Battery charge/discharge helper',
                                     prefix_chars='-+',
                                     add_help=False)

    reqarg = parser.add_argument_group("required arguments")


    optarg = parser.add_argument_group("optional arguments")
    optarg.add_argument( '--help', '-h',
                         action     = 'help',
                         help       = 'show this help message and exit'
                       )
    optarg.add_argument( '-c', '--config',
                         dest       = 'config',
                         default    = def_config,
                         required   = False,
                         type       = str,
                         help       = f"Configuration file (default {def_config}",
                       )
    optarg.add_argument( '-e', '--vlimit',
                         dest       = 'vlimit',
                         default    = defaults['vlimit'],
                         required   = False,
                         type       = float,
                         help       = "PS Voltage threshold",
                       )
    optarg.add_argument( '-i', '--ilimit',
                         dest       = 'ilimit',
                         default    = defaults['ilimit'],
                         required   = False,
                         type       = float,
                         help       = "PS Current threshold",
                       )
    optarg.add_argument( '-s',  '--slack',
                         dest       = 'do_slack',
                         default    = defaults['slack'],
                         required   = False,
                         action     = 'store_true',
                         help       = f"Send a slack message when threshold exceeded " + \
                                      f"(default {defaults['slack']})",
                       )
    optarg.add_argument( '-p', '--power-off',
                         dest       = 'power',
                         default    = defaults['power'],
                         required   = False,
                         action     = 'store_false',
                         help       = "No real access to power suppply",
                       )
    optarg.add_argument( '+p', '--power-on',
                         dest       = 'power',
                         default    = defaults['power'],
                         required   = False,
                         action     = 'store_true',
                         help       = "Real access to power suppply",
                       )
    optarg.add_argument( '-r', '--reset-sec',
                         dest       = 'reset_seconds',
                         default    = defaults['reset_seconds'],
                         required   = False,
                         type       = float,
                         help       = "Seconds under threshold to reset fault condition",
                       )
    optarg.add_argument( '-su', '--slack-url',
                         dest       = 'slack_url',
                         default    = None,
                         required   = False,
                         type       = str,
                         help       = "Slack channel URL (implies -s)",
                       )
    optarg.add_argument( '-v', '--verbose',                 # currently unused / reserved
                         dest       = 'verbose',
                         default    = defaults['verbose'],
                         required   = False,
                         action     = 'store_true',
                         help       = "Increase verbosity",
                       )

    arguments       = parser.parse_args()
    argdict         = vars(arguments)
    argdict['title']= script_title

    if not arguments.do_slack:
        if arguments.slack_url:
            raise InvalidArgumentError("Slack URL meaningless if not sending slack messages")
        argdict['slack_url'] = None
    else:
        argdict['slack_url'] = argdict.get('slack_url') or defaults['slack_url']

    # look for config file
    config = os.path.expandvars(arguments.config) if arguments.config else None
    if config:
        if os.path.isfile(config):
            try:
                with open(config, 'r', encoding='utf-8') as fp:
                    yaml_conf   = yaml.safe_load(fp)
                argdict['telem_points'] = yaml_conf.get("telem")
                if yaml_conf.get("alert"):
                    argdict = {**argdict, **yaml_conf['alert']}
                if yaml_conf.get("general"):
                    argdict = {**argdict, **yaml_conf['general']}
            except Exception as exn:
                print(f"Config file '''{arguments.config}''' is malformed or missing: {exn}")
                sys.exit()
        else:
            print("Config file '" + f"{config}' not found")
            sys.exit()

    if not argdict.get("telem_points"):
        # Dummy telemetry in case no config is available, primarily for debugging
        print("No telemetry points specified, using default dummy points")
        argdict['telem_points'] = {"eps.stats.up_seconds":      {"type": "int", "threshold": None},
                                   "eps.stats.session_seconds": {"type": "int", "threshold": None}}

    # Always monitor the target's boot_id, regardless of what the config file specifies,
    # so a target reboot can be detected even if the operator's config doesn't mention it.
    # (general.detect_reboot only gates whether the app ACTS on it -- see InfoDisplay --
    # not whether it's fetched; the fetch itself is cheap.)
    if BOOT_ID_KEY not in argdict['telem_points']:
        argdict['telem_points'][BOOT_ID_KEY] = {"type": "str", "threshold": None}

    argdict = {**defaults, **argdict}

    try:
        main(argdict)
    except Exception as exn:
        print(f"Error running {script_nm}: {exn}")
