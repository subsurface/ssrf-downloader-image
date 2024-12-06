import json
import os
import os.path
import pathlib
import platform
import re
import shlex
from flask.scaffold import F
import requests
import secrets
import shutil
import string
import subprocess
import threading
import time
from uuid import uuid4
from base64 import b64encode
from datetime import datetime
from os import urandom
from time import sleep
from typing import Dict, List
from zlib import compress
from copy import deepcopy
from pwd import getpwnam
from grp import getgrnam

from utils.config import (
    config_lock,
    read_values_from_config_json,
    write_values_to_config_json,
)
from utils.util import create_fake_info, make_int, print_err, mf_get_ip_and_triplet

# nofmt: on
# isort: off
from flask import (
    Flask,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    Response,
    send_file,
    url_for,
)


from utils import (
    Background,
    Data,
    Env,
    RouteManager,
    System,
    check_restart_lock,
    cleanup_str,
    print_err,
    run_shell_captured,
    stack_info,
    generic_get_json,
    is_true,
    verbose,
)

# nofmt: off
# isort: on

from werkzeug.utils import secure_filename

from flask.logging import logging as flask_logging


# don't log static assets
class NoStatic(flask_logging.Filter):
    def filter(record):
        msg = record.getMessage()
        if "GET /static/" in msg:
            return False
        if not (verbose & 8) and "GET /api/" in msg:
            return False

        return True


flask_logging.getLogger("werkzeug").addFilter(NoStatic)


class Downloader:
    def __init__(self):
        print_err("starting Downloader.__init__", level=4)
        self.app = Flask(__name__)
        self.app.secret_key = urandom(16).hex()

        @self.app.context_processor
        def env_functions():
            def get_value(tags):
                e = self._d.env_by_tags(tags)
                return e.value if e else ""

            def list_value_by_tags(tags, idx):
                e = self._d.env_by_tags(tags)
                return e.list_get(idx) if e else ""

            return {
                "is_enabled": lambda tag: self._d.is_enabled(tag),
                "list_is_enabled": lambda tag, idx: self._d.list_is_enabled(tag, idx=idx),
                "env_value_by_tag": lambda tag: get_value([tag]),  # single tag
                "env_value_by_tags": lambda tags: get_value(tags),  # list of tags
                "list_value_by_tag": lambda tag, idx: list_value_by_tags([tag], idx),
                "list_value_by_tags": lambda tag, idx: list_value_by_tags(tag, idx),
                "env_values": self._d.envs,
            }

        self._routemanager = RouteManager(self.app)
        self._d = Data()
        self._system = System(data=self._d)

        # prepare for app use (vs ADS-B Feeder Image use)
        # newer images will include a flag file that indicates that this is indeed
        # a full image - but in case of upgrades from older version, this heuristic
        # should be sufficient to guess if this is an image or an app
        os_flag_file = self._d.data_path / "os.ssrf.downloader.image"

        if not os_flag_file.exists():
            # we are running as an app under DietPi or some other OS
            self._d.is_feeder_image = False
            with open(self._d.data_path / "ssrf-downloader/templates/systemmgmt.html", "r+") as systemmgmt_file:
                systemmgmt_html = systemmgmt_file.read()
                systemmgmt_file.seek(0)
                systemmgmt_file.write(
                    re.sub(
                        "FULL_IMAGE_ONLY_START.*? FULL_IMAGE_ONLY_END",
                        "",
                        systemmgmt_html,
                        flags=re.DOTALL,
                    )
                )
                systemmgmt_file.truncate()

        self.last_dns_check = 0

        self._current_site_name = None
        self._agg_status_instances = dict()
        self._next_url_from_director = ""
        self._last_stage2_contact = ""
        self._last_stage2_contact_time = 0

        self._last_base_info = dict()

        self._multi_outline_bg = None

        # Ensure secure_image is set the new way if before the update it was set only as env variable
        if self._d.is_enabled("secure_image"):
            self.set_secure_image()

        self.app.add_url_rule("/hotspot_test", "hotspot_test", self.hotspot_test)
        self.app.add_url_rule("/restarting", "restarting", self.restarting)
        self.app.add_url_rule("/restart", "restart", self.restart, methods=["GET", "POST"])
        self.app.add_url_rule("/waiting", "waiting", self.waiting)
        self.app.add_url_rule("/stream-log", "stream_log", self.stream_log)
        self.app.add_url_rule("/running", "running", self.running)
        self.app.add_url_rule("/systemmgmt", "systemmgmt", self.systemmgmt, methods=["GET", "POST"])
        self.app.add_url_rule("/", "director", self.director, methods=["GET", "POST"])
        self.app.add_url_rule("/index", "index", self.index, methods=["GET", "POST"])
        self.app.add_url_rule("/info", "info", self.info)
        self.app.add_url_rule("/support", "support", self.support, methods=["GET", "POST"])
        self.app.add_url_rule("/setup", "setup", self.setup, methods=["GET", "POST"])
        self.app.add_url_rule("/download", "download", self.download, methods=["GET", "POST"])
        self.app.add_url_rule("/update", "update", self.update, methods=["POST"])
        self.app.add_url_rule(f"/api/devices", "api_devices", self.api_devices)
        self.app.add_url_rule(f"/api/find_dc", "api_find_dc", self.api_find_dc)

        self.update_boardname()
        self.update_version()
        self.update_meminfo()
        self.update_journal_state()

        # now all the envs are loaded and reconciled with the data on file - which means we should
        # actually write out the potentially updated values (e.g. when plain values were converted
        # to lists)
        with config_lock:
            write_values_to_config_json(self._d.envs, reason="Startup")

    def update_boardname(self):
        board = ""
        if pathlib.Path("/sys/firmware/devicetree/base/model").exists():
            # that's some kind of SBC most likely
            with open("/sys/firmware/devicetree/base/model", "r") as model:
                board = cleanup_str(model.read().strip())
        else:
            # are we virtualized?
            try:
                output = subprocess.run(
                    "systemd-detect-virt",
                    timeout=2.0,
                    shell=True,
                    capture_output=True,
                )
            except subprocess.SubprocessError:
                pass  # whatever
            else:
                virt = output.stdout.decode().strip()
                if virt and virt != "none":
                    board = f"Virtualized {platform.machine()} environment under {virt}"
                else:
                    prod = ""
                    manufacturer = ""
                    try:
                        prod = subprocess.run(
                            "dmidecode -s system-product-name",
                            shell=True,
                            capture_output=True,
                            text=True,
                        )
                        manufacturer = subprocess.run(
                            "dmidecode -s system-manufacturer",
                            shell=True,
                            capture_output=True,
                            text=True,
                        )
                    except:
                        pass
                    if prod or manufacturer:
                        board = f"Native on {manufacturer.stdout.strip()} {prod.stdout.strip()} {platform.machine()} system"
                    else:
                        board = f"Native on {platform.machine()} system"
        if board == "":
            board = f"Unknown {platform.machine()} system"
        if board == "Firefly roc-rk3328-cc":
            board = f"Libre Computer Renegade ({board})"
        elif board == "Libre Computer AML-S905X-CC":
            board = "Libre Computer Le Potato (AML-S905X-CC)"
        self._d.env_by_tags("board_name").value = board

    def update_version(self):
        conf_version = self._d.env_by_tags("base_version").value
        if pathlib.Path(self._d.version_file).exists():
            with open(self._d.version_file, "r") as f:
                file_version = f.read().strip()
        else:
            file_version = ""
        if file_version:
            if file_version != conf_version:
                print_err(
                    f"found version '{conf_version}' in memory, but '{file_version}' on disk, updating to {file_version}"
                )
                self._d.env_by_tags("base_version").value = file_version
        else:
            if conf_version:
                print_err(f"no version found on disk, using {conf_version}")
                with open(self._d.version_file, "w") as f:
                    f.write(conf_version)
            else:
                print_err("no version found on disk or in memory, using v0.0.0")
                self._d.env_by_tags("base_version").value = "v0.0.0"

    def update_meminfo(self):
        self._memtotal = 0
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        self._memtotal = make_int(line.split()[1])
                        break
        except:
            pass

    @property
    def dcs(self):
        print_err("setting up dive computer dict")
        dcs = {}
        try:
            print_err("running subsurface-downloader --list-dc")
            result = subprocess.run(
                ["/opt/ssrf/install-root/bin/subsurface-downloader", "--list-dc"],
                # ["/usr/bin/ls", "--help"],
                user="pi",
                group="pi",
                env={"LOGNAME": "pi", "HOME": "/home/pi", "LANG": "en_GB.UTF-8"},
                shell=False,
                capture_output=True,
                timeout=2.0,
            )
            output = result.stderr.decode("utf-8")
        except:
            print_err("failed to run subsurface-downloader --list-dc")
            output = ["INFO: Supported dive computers:", "INFO: Error: unknown (unknown)"]
        matches = re.findall(r"INFO: ([^:]+): (.*)", output, re.MULTILINE)
        for m in matches:
            if m:
                pl = m[1].split("),")
                prodlist = []
                for p in pl:
                    productname, connections = p.split("(", 1)
                    prodlist.append([productname.strip(), connections.rstrip(")")])
                dcs[m[0]] = prodlist
            else:
                print_err(f"no match for")
        return dcs

    def ls_dev(self, pattern):
        try:
            result = subprocess.run(
                ["find", "/dev", "-maxdepth", "1"],
                user="pi",
                group="pi",
                env={"LOGNAME": "pi", "HOME": "/home/pi", "LANG": "en_GB.UTF-8"},
                shell=False,
                capture_output=True,
                timeout=2.0,
            )
            output = result.stdout.decode("utf-8")
        except:
            print_err("failed to run lsusb")
            output = ""
        matches = re.findall(rf"({pattern})", output, re.MULTILINE)
        return matches

    def api_find_dc(self):
        dcs = []
        # run lsusb to check if we can find a dive computer that's plugged in
        try:
            print_err("running lsusb")
            result = subprocess.run(
                ["lsusb"],
                user="pi",
                group="pi",
                env={"LOGNAME": "pi", "HOME": "/home/pi", "LANG": "en_GB.UTF-8"},
                shell=False,
                capture_output=True,
                timeout=2.0,
            )
            output = result.stdout.decode("utf-8")
        except:
            print_err("failed to run lsusb")
            output = ""
        matches = re.findall(r"Bus [0-9]+ Device [0-9]+: ID ([0-9a-f:]+) ", output, re.MULTILINE)
        print_err(f"found {len(matches)} devices")
        for m in matches:
            pattern = "/dev/ttyUSB[0-9]"
            dc = {"vendor": "", "product": "", "connection": "/dev/ttyUSB0"}
            print_err(f"checking {m}")
            if "ffff:0005" == m:
                print_err("found Mares Icon HD")
                dc.update({"vendor": "Mares", "product": "Icon HD", "connection": "/dev/ttyACM0"})
                pattern = "/dev/ttyACM[0-9]"
            elif "0403:f460" == m:
                print_err("found an FTDI Oceanic device")
                dc.update({"vendor": "Oceanic"})
            elif "0403:f680" == m:
                print_err("found an FTDI Suunto device")
                dc.update({"vendor": "Suunto"})
            elif "0403:87d0" == m:
                print_err("found an FTDI Cressi device")
                dc.update({"vendor": "Cressi"})
            elif m in [f"0403:{p}" for p in ["6001", "6010", "6011", "6014", "6015"]]:
                print_err("found an FTDI device (unknown vendor)")
            else:
                # doesn't look like a dive computer
                continue

            devices = self.ls_dev(pattern)
            print_err(f"found {len(devices)} {pattern} devices")
            if len(devices) > 0:
                dc["connection"] = devices[0]
            dcs.append(dc)

        return json.dumps(dcs)

    def api_devices(self):
        print_err(f"api/devices with args: {request.args}")
        vendor = request.args.get("vendor", "")
        product = request.args.get("product", "")
        candidates = json.loads(self.api_find_dc())
        print_err(f"found {len(candidates)} candidates {candidates} for {vendor} {product}")
        devices = [c["connection"] for c in candidates if c["vendor"] in vendor and c["product"] in product]
        if len(devices) == 0:
            devices = ["/dev/ttyUSB0"]
        if vendor and product:
            # deal with HIDRAW dive computesr
            usb = ""
            if vendor == "Suunto":
                if product == "EON Steel":
                    usb = "1493:0030"
                elif product == "EON Core":
                    usb = "1493:0033"
                elif product == "D5":
                    usb = "1493:0035"
                elif product == "EON Steel Black":
                    usb = "1493:0036"
            elif vendor == "Atomics Aquatics":
                if product == "Cobalt":
                    usb = "0471:0888"
            elif vendor == "Scubapro":
                if product == "G2":
                    usb = "2e6c:3201"
                elif product == "G2 Console":
                    usb = "2e6c:3211"
                elif product == "G2 HUD":
                    usb = "2e6c:4201"
                elif product == "Aladin Square":
                    usb = "c251:2006"

            if usb:
                # check if the device is connected via USB
                try:
                    subprocess.run(["/usr/bin/lsusb", "-d", usb], check=True)
                except subprocess.CalledProcessError:
                    pass
                else:
                    devices.append("USB (automatic)")
        print_err(f"found {len(devices)} devices: {devices}")
        return {"devices": devices}

    def update_journal_state(self):
        # with no config setting or an 'auto' setting, the journal is persistent IFF /var/log/journal exists
        self._persistent_journal = pathlib.Path("/var/log/journal").exists()
        # read journald.conf line by line and check if we override the default
        try:
            result = subprocess.run(
                "systemd-analyze cat-config systemd/journald.conf", shell=True, capture_output=True, timeout=2.0
            )
            config = result.stdout.decode("utf-8")
        except:
            config = "Storage=auto"
        for line in config:
            if line.startswith("Storage=volatile"):
                self._persistent_journal = False
                break

    def check_secure_image(self):
        return self._d.secure_image_path.exists()

    def set_secure_image(self):
        # set legacy env variable as well for webinterface
        self._d.env_by_tags("secure_image").value = True
        if not self.check_secure_image():
            self._d.secure_image_path.touch(exist_ok=True)
            print_err("secure_image has been set")

    def update_dns_state(self):
        def update_dns():
            dns_state = self._system.check_dns()
            self._d.env_by_tags("dns_state").value = dns_state
            if not dns_state:
                print_err("ERROR: we appear to have lost DNS")

        self.last_dns_check = time.time()
        threading.Thread(target=update_dns).start()

    def onlyAlphaNumDash(self, name):
        new_name = "".join(c for c in name if c.isalnum() or c == "-")
        new_name = new_name.strip("-")[:63]
        return new_name

    def set_hostname(self, site_name: str):
        os_flag_file = self._d.data_path / "os.ssrf.downloader.image"
        if not os_flag_file.exists() or not site_name:
            return
        # create a valid hostname from the site name and set it up as mDNS alias
        # initially we only allowed alpha-numeric characters, but after fixing an
        # error in the service file, we now can allow dash (or hyphen) as well.
        host_name = self.onlyAlphaNumDash(site_name)

        def start_mdns():
            subprocess.run(["/usr/bin/bash", "/opt/ssrf/scripts/mdns-alias-setup.sh", f"{host_name}"])
            subprocess.run(["/usr/bin/hostnamectl", "hostname", f"{host_name}"])

        if not host_name or self._current_site_name == site_name:
            return

        self._current_site_name = site_name
        # print_err(f"set_hostname {site_name} {self._current_site_name}")

        thread = threading.Thread(target=start_mdns)
        thread.start()

    def run(self, no_server=False):
        debug = os.environ.get("SSRF_DEBUG") is not None

        # hopefully very temporary hack to deal with a broken container that
        # doesn't run on Raspberry Pi 5 boards
        board = self._d.env_by_tags("board_name").value

        self.handle_implied_settings()

        self._every_minute = Background(60, self.every_minute)
        # every_minute stuff is required to initialize some values, run it synchronously
        self.every_minute()

        self.app.run(
            host="0.0.0.0",
            port=int(self._d.env_by_tags("webport").value),
            debug=debug,
        )

    def set_tz(self, timezone):
        # timezones don't have spaces, only underscores
        # replace spaces with underscores to correct this common error
        timezone = timezone.replace(" ", "_")

        success = self.set_system_tz(timezone)
        if success:
            self._d.env("FEEDER_TZ").list_set(0, timezone)
        else:
            print_err(f"timezone {timezone} probably invalid, defaulting to UTC")
            self._d.env("FEEDER_TZ").list_set(0, "UTC")
            self.set_system_tz("UTC")

    def set_system_tz(self, timezone):
        # timedatectl can fail on dietpi installs (Failed to connect to bus: No such file or directory)
        # thus don't rely on timedatectl and just set environment for containers regardless of timedatectl working
        try:
            print_err(f"calling timedatectl set-timezone {timezone}")
            subprocess.run(["timedatectl", "set-timezone", f"{timezone}"], check=True)
        except subprocess.SubprocessError:
            print_err(f"failed to set up timezone ({timezone}) using timedatectl, try dpkg-reconfigure instead")
            try:
                subprocess.run(["test", "-f", f"/usr/share/zoneinfo/{timezone}"], check=True)
            except:
                print_err(f"setting timezone: /usr/share/zoneinfo/{timezone} doesn't exist")
                return False
            try:
                subprocess.run(["ln", "-sf", f"/usr/share/zoneinfo/{timezone}", "/etc/localtime"])
                subprocess.run("dpkg-reconfigure --frontend noninteractive tzdata", shell=True)
            except:
                pass

        return True

    def hotspot_test(self):
        return render_template("hotspot.html", version="123", comment="comment", ssids=list(range(20)))

    def restarting(self):
        return render_template("restarting.html")

    def restart(self):
        self._system._restart.wait_restart_done(timeout=5)
        return self._system._restart.state

    def running(self):
        return "OK"

    def base_is_configured(self):
        base_config: set[Env] = {env for env in self._d._env if env.is_mandatory}
        for env in base_config:
            if env._value == None or (type(env._value) == list and not env.list_get(0)):
                print_err(f"base_is_configured: {env} isn't set up yet")
                return False
        return True

    def agg_status(self, agg, idx=0):
        # print_err(f'agg_status(agg={agg}, idx={idx})')
        if agg == "im":
            return json.dumps(self._im_status.check())

    def set_channel(self, channel: str):
        with open(self._d.data_path / "update-channel", "w") as update_channel:
            print(channel, file=update_channel)

    def extract_channel(self) -> str:
        channel = self._d.env_by_tags("base_version").value
        if channel:
            match = re.search(r"\((.*?)\)", channel)
            if match:
                channel = match.group(1)
        if channel in ["stable", "beta", "main"]:
            channel = ""
        if not channel.startswith("origin/"):
            channel = f"origin/{channel}"
        return channel

    def set_rpw(self):
        try:
            subprocess.call(f"echo 'root:{self.rpw}' | chpasswd", shell=True)
        except:
            print_err("failed to overwrite root password")
        if os.path.exists("/etc/ssh/sshd_config"):
            try:
                subprocess.call(
                    "sed -i 's/^\(PermitRootLogin.*\)/# \\1/' /etc/ssh/sshd_config &&"
                    "echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config && "
                    "systemctl restart sshd",
                    shell=True,
                )
            except:
                print_err("failed to allow root ssh login")

    def handle_implied_settings(self):
        # finally, check if this has given us enough configuration info to
        # start the containers
        if self.base_is_configured():
            self._d.env_by_tags(["base_config", "is_enabled"]).value = True

            if self._d.is_feeder_image and not self._d.env_by_tags("journal_configured").value:
                try:
                    cmd = "/opt/ssrf/scripts/journal-set-volatile.sh"
                    print_err(cmd)
                    subprocess.run(cmd, shell=True, timeout=5.0)
                    self.update_journal_state()
                    self._d.env_by_tags("journal_configured").value = True
                except:
                    pass

    @check_restart_lock
    def update(self):
        description = """
            This is the one endpoint that handles all the updates coming in from the UI.
            It walks through the form data and figures out what to do about the information provided.
        """
        allow_insecure = not self.check_secure_image()
        # in the HTML, every input field needs to have a name that is concatenated by "--"
        # and that matches the tags of one Env
        form: Dict = request.form
        seen_go = False
        next_url = None
        for key, value in form.items():
            value_string = "''"
            if key == "password":
                value_string = "'********'"
            elif value != "":
                value_string = value
            print_err(f"handling {key} -> {value_string}")
            # this seems like cheating... let's capture all of the submit buttons
            if value == "go" or value.startswith("go-"):
                seen_go = True
            if value == "go" or value.startswith("go-") or value == "wait":
                if allow_insecure and key == "shutdown":
                    # do shutdown
                    def do_halt():
                        sleep(0.5)
                        self._system.halt()

                    threading.Thread(target=do_halt).start()
                    return render_template("/shutdownpage.html")
                if allow_insecure and key == "reboot":
                    # initiate reboot
                    self._system.reboot()
                    return render_template("/restarting.html")
                if key == "log_persistence_toggle":
                    if self._persistent_journal:
                        cmd = "/opt/ssrf/scripts/journal-set-volatile.sh"
                    else:
                        cmd = "/opt/ssrf/scripts/journal-set-persist.sh"
                    try:
                        print_err(cmd)
                        subprocess.run(cmd, shell=True, timeout=5.0)
                        self.update_journal_state()
                    except:
                        pass
                    self._next_url_from_director = request.url
                if key == "secure_image":
                    self.set_secure_image()
                if key.startswith("update_feeder_aps"):
                    channel = key.rsplit("_", 1)[-1]
                    if channel == "branch":
                        channel = self.extract_channel()
                    self.set_channel(channel)
                    print_err(f"updating feeder to {channel} channel")
                    self._system._restart.bg_run(cmdline="systemctl start adsb-feeder-update.service")
                    return render_template("/restarting.html")
                if key == "nightly_update" or key == "zerotier":
                    # this will be handled through the separate key/value pairs
                    pass
                if key == "os_update":
                    self._system._restart.bg_run(func=self._system.os_update)
                    self._next_url_from_director = request.url
                    return render_template("/restarting.html")
                if allow_insecure and key == "tailscale":
                    # grab extra arguments if given
                    ts_args = form.get("tailscale_extras", "")
                    if ts_args:
                        # right now we really only want to allow the login server arg
                        ts_cli_switch, ts_cli_value = ts_args.split("=")
                        if ts_cli_switch != "--login-server":
                            print_err(
                                "at this point we only allow the --login-server argument; "
                                "please let us know at the Zulip support link why you need "
                                f"this to support {ts_cli_switch}"
                            )
                            continue
                        print_err(f"login server arg is {ts_cli_value}")
                        match = re.match(
                            r"^https?://[-a-zA-Z0-9._\+~=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?::[0-9]{1,5})?(?:[-a-zA-Z0-9()_\+.~/=]*)$",
                            ts_cli_value,
                        )
                        if not match:
                            print_err(f"the login server URL didn't make sense {ts_cli_value}")
                            continue
                    print_err(f"starting tailscale (args='{ts_args}')")
                    try:
                        subprocess.run(
                            ["/usr/bin/systemctl", "enable", "--now", "tailscaled"],
                            timeout=20.0,
                        )
                        cmd = ["/usr/bin/tailscale", "up"]

                        cmd += [f"--hostname=subsurface-downloader"]

                        if ts_args:
                            cmd += [f"--login-server={shlex.quote(ts_cli_value)}"]
                        cmd += ["--accept-dns=false"]
                        print_err(f"running {cmd}")
                        proc = subprocess.Popen(
                            cmd,
                            stderr=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            text=True,
                        )
                        os.set_blocking(proc.stderr.fileno(), False)
                    except:
                        # this really needs a user visible error...
                        print_err("exception trying to set up tailscale - giving up")
                        continue
                    else:
                        startTime = time.time()
                        match = None
                        while time.time() - startTime < 30:
                            output = proc.stderr.readline()
                            if not output:
                                if proc.poll() != None:
                                    break
                                time.sleep(0.1)
                                continue
                            print_err(output.rstrip("\n"))
                            # standard tailscale result
                            match = re.search(r"(https://login\.tailscale.*)", output)
                            if match:
                                break
                            # when using a login-server
                            match = re.search(r"(https://.*/register/nodekey.*)", output)
                            if match:
                                break

                        proc.terminate()

                    if match:
                        login_link = match.group(1)
                        print_err(f"found login link {login_link}")
                        self._d.env_by_tags("tailscale_ll").value = login_link
                    else:
                        print_err(f"ERROR: tailscale didn't provide a login link within 30 seconds")
                    return redirect(url_for("systemmgmt"))
                # tailscale handling uses 'continue' to avoid deep nesting - don't add other keys
                # here at the end - instead insert them before tailscale
                continue
            if value == "stay" or value.startswith("stay-"):
                if allow_insecure and key == "rpw":
                    print_err("updating the root password")
                    self.set_rpw()
                    continue
            # now handle other form input
            e = self._d.env_by_tags(key.split("--"))
            if e:
                if allow_insecure and key == "ssh_pub":
                    ssh_dir = pathlib.Path("/root/.ssh")
                    ssh_dir.mkdir(mode=0o700, exist_ok=True)
                    with open(ssh_dir / "authorized_keys", "a+") as authorized_keys:
                        authorized_keys.write(f"{value}\n")
                    self._d.env_by_tags("ssh_configured").value = True
                    continue
                if allow_insecure and key == "zerotierid":
                    try:
                        subprocess.call("/usr/bin/systemctl enable --now zerotier-one", shell=True)
                        sleep(5.0)  # this gives the service enough time to get ready
                        subprocess.call(
                            ["/usr/sbin/zerotier-cli", "join", f"{value}"],
                        )
                    except:
                        print_err("exception trying to set up zerorier - giving up")
                    continue
                e.value = value

        # done handling the input data
        # what implied settings do we have (and could we simplify them?)

        self.handle_implied_settings()

        if request.form.get("download") == "stay":
            # user asked to try to download from the dive computer
            self.do_download()
            return redirect(url_for("director"))

        # if the button simply updated some field, stay on the same page
        if not seen_go:
            print_err("no go button, so stay on the same page", level=2)
            return redirect(request.url)

        # where do we go from here?
        if next_url:  # we figured it out above
            return redirect(next_url)
        if self._d.is_enabled("base_config"):
            print_err("base config is completed", level=2)
            return render_template("/restarting.html")
        print_err("base config not completed", level=2)
        return redirect(url_for("director"))

    def sync_cloud(self, background=False):
        def git_update():
            username = self._d.env_by_tags("username").value
            args = [
                "/usr/bin/git",
                "-C",
                f"/home/pi/cloudstorage/{username}",
                "pull",
            ]
            try:
                result = subprocess.run(
                    args,
                    user="pi",
                    group="dialout",
                    env={"LOGNAME": "pi", "HOME": "/home/pi", "LANG": "en_GB.UTF-8"},
                    shell=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            except subprocess.CalledProcessError as e:
                print_err(f"exception trying to update cloudstorage - giving up: {e}")

        print_err(f"starting sync_cloud in the {'background' if background else 'foreground'}", level=4)
        if background:
            threading.Thread(target=git_update).start()
        else:
            git_update()

    def clone_cloud_if_necessary(self):
        username = self._d.env_by_tags("username").value
        cs_path = f"/home/pi/cloudstorage/{username}"
        if os.path.exists(cs_path):
            return
        # create the target path, owned by pi/pi
        os.makedirs(cs_path)
        uid = getpwnam("pi").pw_uid
        gid = getgrnam("pi").gr_gid
        os.chown("/home/pi/cloudstorage", uid, gid)
        os.chown(cs_path, uid, gid)
        password = self._d.env_by_tags("password").value
        args = [
            "/usr/bin/git",
            "clone",
            "-b",
            f"{username}",
            f"https://{username.replace('@', '%40')}:{password}@cloud.subsurface-divelog.org/git/{username}",
            f"/home/pi/cloudstorage/{username}",
        ]
        print_err(f"attempting to clone cloudstorage: {args}")
        try:
            result = subprocess.run(
                args,
                user="pi",
                group="dialout",
                env={"LOGNAME": "pi", "HOME": "/home/pi", "LANG": "en_GB.UTF-8"},
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except:
            pass

        if result.returncode != 0:
            print_err(f"attempt to clone cloudstorage: {result.stdout.decode('utf-8')}")
            print_err("failed to clone cloudstorage - giving up")
            os.rmdir(cs_path)
            return

        print_err(f"cloned cloudstorage {result.stdout.decode('utf-8')}")

    @check_restart_lock
    def do_download(self):
        print_err("starting do_download", level=4)
        self.sync_cloud(background=False)
        dives = -1
        username = self._d.env_by_tags("username").value
        args = [
            "/opt/ssrf/install-root/bin/subsurface-downloader",
            f"--dc-vendor={self._d.env_by_tags('vendor').value.strip()}",
            f"--dc-product={self._d.env_by_tags('product').value.strip()}",
            f"--device={self._d.env_by_tags('device').value.strip()}",
            f"/home/pi/cloudstorage/{username}/[{username}]",
        ]
        try:
            print_err(f"running {args}")
            result = subprocess.run(
                args,
                user="pi",
                group="dialout",
                env={"LOGNAME": "pi", "HOME": "/home/pi", "LANG": "en_GB.UTF-8"},
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output = result.stdout.decode("utf-8")
            match = re.search(r"Finishing download thread: (\d+) dives downloaded", output)
            if match:
                dives = make_int(match.group(1))
                print_err(f"downloaded {dives} dives")
                flash(f"Downloaded {dives} dives.", "success" if dives > 0 else "info")
                self.sync_cloud(background=True)
            else:
                print_err(f"unknown download results: |{output}|")
                flash(f"Cannot parse result of download.", "warning")

        except Exception as e:
            print_err(f"exception trying to download - giving up: {e}")
            flash(f"Exception trying to download - giving up: {e}.", "error")

        self._d.env_by_tags("last_download_count").value = dives

    @check_restart_lock
    def systemmgmt(self):
        if request.method == "POST":
            return self.update()
        if self._d.is_feeder_image:
            # is tailscale set up?
            try:
                result = subprocess.run(
                    "pgrep tailscaled >/dev/null 2>/dev/null && tailscale status --json 2>/dev/null",
                    shell=True,
                    check=True,
                    capture_output=True,
                )
            except:
                # a non-zero return value means tailscale isn't configured
                self._d.env_by_tags("tailscale_name").value = ""
            else:
                ts_status = json.loads(result.stdout.decode())
                if ts_status.get("BackendState") == "Running" and ts_status.get("Self"):
                    tailscale_name = ts_status.get("Self").get("HostName")
                    print_err(f"configured as {tailscale_name} on tailscale")
                    self._d.env_by_tags("tailscale_name").value = tailscale_name
                    self._d.env_by_tags("tailscale_ll").value = ""
                else:
                    self._d.env_by_tags("tailscale_name").value = ""
        # create a potential new root password in case the user wants to change it
        alphabet = string.ascii_letters + string.digits
        self.rpw = "".join(secrets.choice(alphabet) for i in range(12))
        # if we are on a branch that's neither stable nor beta, pass the value to the template
        # so that a third update button will be shown
        return render_template(
            "systemmgmt.html",
            rpw=self.rpw,
            persistent_journal=self._persistent_journal,
        )

    @check_restart_lock
    def director(self):
        # figure out where to go:
        if request.method == "POST":
            return self.update()
        if not self._d.is_enabled("base_config"):
            print_err(f"director redirecting to setup, base_config not completed")
            return self.setup()
        # if we already figured out where to go next, let's just do that
        if self._next_url_from_director:
            print_err(f"director redirecting to next_url_from_director: {self._next_url_from_director}")
            url = self._next_url_from_director
            self._next_url_from_director = ""
            return redirect(url)
        return self.index()

    def every_minute(self):
        # make sure DNS works, every 5 minutes is sufficient
        if time.time() - self.last_dns_check > 300:
            self.update_dns_state()

        try:
            result = subprocess.run(
                "ip route get 1 | head -1  | cut -d' ' -f7",
                shell=True,
                capture_output=True,
                timeout=2.0,
            ).stdout
        except:
            result = ""
        else:
            result = result.decode().strip()
        if result:
            self.local_address = result
        else:
            self.local_address = ""

        if self._d.env_by_tags("tailscale_name").value:
            try:
                result = subprocess.run(
                    "tailscale ip -4 2>/dev/null",
                    shell=True,
                    capture_output=True,
                    timeout=2.0,
                ).stdout
            except:
                result = ""
            else:
                result = result.decode().strip()
            self.tailscale_address = result
        else:
            self.tailscale_address = ""
        zt_network = self._d.env_by_tags("zerotierid").value
        if zt_network:
            try:
                result = subprocess.run(
                    ["zerotier-cli", "get", f"{zt_network}", "ip4"],
                    shell=True,
                    capture_output=True,
                    timeout=2.0,
                ).stdout
            except:
                result = ""
            else:
                result = result.decode().strip()
            self.zerotier_address = result
        else:
            self.zerotier_address = ""
        # next check if there were under-voltage events (this is likely only relevant on an RPi)
        self._d.env_by_tags("under_voltage").value = False
        board = self._d.env_by_tags("board_name").value
        if board and board.startswith("Raspberry"):
            try:
                # yes, the except / else is a bit unintuitive, but that seemed the easiest way to do this;
                # if we don't find the text (the good case) we get an exception
                # ... on my kernels the message seems to be "Undervoltage", but on the web I find references to "under-voltage"
                subprocess.check_call(
                    "dmesg --since '30min ago' | grep -iE under.?voltage",
                    shell=True,
                    stdout=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError:
                pass
            else:
                self._d.env_by_tags("under_voltage").value = True

        # now let's check for disk space
        self._d.env_by_tags("low_disk").value = shutil.disk_usage("/").free < 1024 * 1024 * 1024

    @check_restart_lock
    def index(self):
        # if we get to show the feeder homepage, the user should have everything figured out
        # and we can remove the pre-installed ssh-keys and password
        if os.path.exists("/opt/ssrf/ssrf.downloader.passwd.and.keys"):
            print_err("removing pre-installed ssh-keys, overwriting root password")
            authkeys = "/root/.ssh/authorized_keys"
            shutil.copyfile(authkeys, authkeys + ".bak")
            with open("/root/.ssh/ssrf.downloader.installkey", "r") as installkey_file:
                installkey = installkey_file.read().strip()
            with open(authkeys + ".bak", "r") as org_authfile:
                with open(authkeys, "w") as new_authfile:
                    for line in org_authfile.readlines():
                        if "ssrf" not in line and installkey not in line:
                            new_authfile.write(line)
            # now overwrite the root password with something random
            alphabet = string.ascii_letters + string.digits + string.punctuation
            self.rpw = "".join(secrets.choice(alphabet) for i in range(12))
            self.set_rpw()
            os.remove("/opt/ssrf/ssrf.downloader.passwd.and.keys")
        board = self._d.env_by_tags("board_name").value
        # there are many other boards I should list here - but Pi 3 and Pi Zero are probably the most common
        stage2_suggestion = board.startswith("Raspberry") and not (
            board.startswith("Raspberry Pi 4") or board.startswith("Raspberry Pi 5")
        )
        if self.local_address:
            local_address = self.local_address
        else:
            local_address = request.host.split(":")[0]

        return render_template(
            "index.html",
            local_address=local_address,
            tailscale_address=self.tailscale_address,
            zerotier_address=self.zerotier_address,
            stage2_suggestion=stage2_suggestion,
        )

    @check_restart_lock
    def setup(self):
        print_err(f"setup request, {request.form}")
        if request.method == "POST" and request.form.get("submit") == "go":
            next = self.update()
            if self._d.env_by_tags("username").value and self._d.env_by_tags("password").value:
                self.clone_cloud_if_necessary()
            return next
        # make sure DNS works
        self.update_dns_state()
        return render_template("setup.html")

    @check_restart_lock
    def download(self):
        print_err(f"download request, {request.form}")
        if request.method == "POST" and request.form.get("download") == "stay":
            return self.update()
        return render_template("download.html", dcs=self.dcs)

    def support(self):
        print_err(f"support request, {request.form}")
        if request.method != "POST":
            return render_template("support.html", url="")

        url = "Internal Error uploading logs"

        target = request.form.get("upload")
        print_err(f'trying to upload the logs with target: "{target}"')

        if not target:
            print_err(f"ERROR: support POST request without target")
            return render_template("support.html", url="Error, unspecified upload target!")

        if target == "0x0.st":
            success, output = run_shell_captured(
                command="bash /opt/ssrf/log-sanitizer.sh | curl -F'expires=168' -F'file=@-'  https://0x0.st",
                timeout=60,
            )
            url = output.strip()
            if success:
                print_err(f"uploaded logs to {url}")
            else:
                print_err(f"failed to upload logs, output: {output}")
            return render_template("support.html", url=url)

        if target == "termbin.com":
            success, output = run_shell_captured(
                command="bash /opt/ssrf/log-sanitizer.sh | nc termbin.com 9999",
                timeout=60,
            )
            # strip extra chars for termbin
            url = output.strip("\0\n").strip()
            if success:
                print_err(f"uploaded logs to {url}")
            else:
                print_err(f"failed to upload logs, output: {output}")
            return render_template("support.html", url=url)

        if target == "local_view" or target == "local_download":

            as_attachment = target == "local_download"

            fdOut, fdIn = os.pipe()
            pipeOut = os.fdopen(fdOut, "rb")
            pipeIn = os.fdopen(fdIn, "wb")

            def get_log(fobj):
                subprocess.run(
                    "bash /opt/ssrf/log-sanitizer.sh",
                    shell=True,
                    stdout=fobj,
                    stderr=subprocess.STDOUT,
                    timeout=30,
                )

            thread = threading.Thread(
                target=get_log,
                kwargs={
                    "fobj": pipeIn,
                },
            )
            thread.start()

            now = datetime.now().replace(microsecond=0).isoformat().replace(":", "-")
            download_name = f"subsurface-downloader-config-{now}.txt"
            return send_file(
                pipeOut,
                as_attachment=as_attachment,
                download_name=download_name,
            )

        return render_template("support.html", url="upload logs: unexpected code path")

    def info(self):
        board = self._d.env_by_tags("board_name").value
        current = self._d.env_by_tags("base_version").value

        def simple_cmd_result(cmd):
            try:
                result = subprocess.run(cmd, shell=True, capture_output=True, timeout=2.0)
                return result.stdout.decode("utf-8")
            except:
                return f"failed to run '{cmd}'"

        storage = simple_cmd_result("df -h | grep -v overlay")
        kernel = simple_cmd_result("uname -a")
        memory = simple_cmd_result("free -h")
        top = simple_cmd_result("top -b -n1 | head -n5")
        journal = "persistent on disk" if self._persistent_journal else "in memory"

        return render_template(
            "info.html",
            board=board,
            memory=memory,
            top=top,
            storage=storage,
            kernel=kernel,
            journal=journal,
            current=current,
        )

    def waiting(self):
        return render_template("waiting.html", title="ADS-B Feeder performing requested actions")

    def stream_log(self):
        logfile = "/run/subsurface-downloader-image.log"

        def tail():
            with open(logfile, "r") as file:
                ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
                tmp = file.read()[-16 * 1024 :]
                # discard anything but the last 16 kB
                while self._system._restart.state == "restarting":
                    tmp += file.read(16 * 1024)
                    if tmp and tmp.find("\n") != -1:
                        block, tmp = tmp.rsplit("\n", 1)
                        block = ansi_escape.sub("", block)
                        lines = block.split("\n")
                        data = "".join(["data: " + line + "\n" for line in lines])
                        yield data + "\n\n"
                    else:
                        time.sleep(0.2)

        return Response(tail(), mimetype="text/event-stream")


if __name__ == "__main__":
    Downloader().run()
