# dataclass
from dataclasses import dataclass
from pathlib import Path

from .environment import Env
from .netconfig import NetConfig
from .util import is_true, print_err


@dataclass
class Data:
    def __new__(cc):
        if not hasattr(cc, "instance"):
            cc.instance = super(Data, cc).__new__(cc)
        return cc.instance

    data_path = Path("/opt/ssrf")
    config_path = data_path / "."
    version_file = data_path / "ssrf.downloader.version"
    secure_image_path = data_path / "ssrf.downloader.secure_image"
    is_feeder_image = True
    settings = None
    _env_by_tags_dict = dict()
    ultrafeeder = []

    _env = {
        # Mandatory site data
        Env("USERNAME", default="", is_mandatory=True, tags=["username"]),
        Env("PASSWORD", default="", is_mandatory=True, tags=["password"]),
        Env("VENDOR", default="", tags=["vendor"]),
        Env("PRODUCT", default="", tags=["product"]),
        Env("DEVICE", default="", tags=["device"]),
        Env("VERSION", default="", tags=["version"]),
        Env("BASE_VERSION", default="", tags=["base_version"]),
        Env("SECURE_IMAGE", default=False, tags=["secure_image", "is_enabled"]),
        Env("DNS_STATE", default=False, tags=["dns_state"]),
        Env("BOARD_NAME", default="", tags=["board_name"]),
        Env("WEBPORT", default=80, tags=["webport"]),
        Env("JOURNAL_CONFIGURED", default=False, tags=["journal_configured", "is_enabled"]),
        Env("TAILSCALE_LL", default="", tags=["tailscale_ll"]),
        Env("TAILSCALE_EXTRAS", default="", tags=["tailscale_extras"]),
        Env("TAILSCALE_NAME", default="", tags=["tailscale_name"]),
        Env("ZEROTIERID", default="", tags=["zerotierid"]),
        Env("SSH_CONFIGURED", default=False, tags=["ssh_configured", "is_enabled"]),
        Env("UNDER_VOLTAGE", default=False, tags=["under_voltage"]),
        Env("LOW_DISK", default=False, tags=["low_disk"]),
        Env("BASE_CONFIG", default=False, tags=["base_config", "is_enabled"]),
        Env("CSS_THEME", default="auto", tags=["css_theme"]),
        Env("LAST_DOWNLOAD_COUNT", default=0, tags=["last_download_count"]),
    }

    @property
    def envs(self):
        return {e.name: e._value for e in self._env}

    # helper function to find env by name
    def env(self, name: str):
        for e in self._env:
            if e.name == name:
                return e
        return None

    # helper function to find env by tags
    # Return only if there is one env with all the tags,
    # Raise error if there are more than one match
    def env_by_tags(self, _tags):
        if type(_tags) == str:
            tags = [_tags]
        elif type(_tags) == list:
            tags = _tags
        else:
            raise Exception(f"env_by_tags called with invalid argument {_tags} of type {type(_tags)}")
        if not tags:
            return None

        # make the list a tuple so it's hashable
        tags = tuple(tags)
        cached = self._env_by_tags_dict.get(tags)
        if cached:
            return cached

        matches = []
        for e in self._env:
            if not e.tags:
                print_err(f"{e} has no tags")
            if all(t in e.tags for t in tags):
                matches.append(e)
        if len(matches) == 0:
            return None
        if len(matches) > 1:
            print_err(f"More than one match for tags {tags}")
            for e in matches:
                print_err(f"  {e}")

        self._env_by_tags_dict[tags] = matches[0]
        return matches[0]

    def _get_enabled_env_by_tags(self, tags):
        # we append is_enabled to tags
        tags.append("is_enabled")
        # stack_info(f"taglist {tags} gets us env {self.env_by_tags(tags)}")
        return self.env_by_tags(tags)

    # helper function to see if something is enabled
    def is_enabled(self, tags):
        if type(tags) != list:
            tags = [tags]
        e = self._get_enabled_env_by_tags(tags)
        if type(e._value) == list:
            ret = e and is_true(e.list_get(0))
            print_err(f"is_enabled called on list: {e}[0] = {ret}")
            return ret
        return e and is_true(e._value)

    # helper function to see if list element is enabled
    def list_is_enabled(self, tags, idx):
        if type(tags) != list:
            tags = [tags]
        e = self._get_enabled_env_by_tags(tags)
        ret = is_true(e.list_get(idx)) if e else False
        print_err(f"list_is_enabled: {e}[{idx}] = {ret}", level=8)
        return ret
