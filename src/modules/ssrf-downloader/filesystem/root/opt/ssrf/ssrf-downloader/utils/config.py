import json
import os
import os.path
import tempfile
import threading
from .util import print_err

CONF_DIR = "/opt/ssrf/config"
JSON_FILE_PATH = CONF_DIR + "/config.json"

config_lock = threading.Lock()


def read_values_from_config_json():
    # print_err("reading .json file")
    if not os.path.exists(JSON_FILE_PATH):
        # this must be either a first run after an install,
        # or the first run after an upgrade from a version that didn't use the config.json
        print_err("WARNING: config.json doesn't exist, populating from .env")
        values = {}
        write_values_to_config_json(values, reason="config.json didn't exist")

    ret = {}
    try:
        ret = json.load(open(JSON_FILE_PATH, "r"))
    except:
        print_err("Failed to read .json file")
    return ret


def write_values_to_config_json(data: dict, reason="no reason provided"):
    try:
        print_err(f"config.json write: {reason}")
        fd, tmp = tempfile.mkstemp(dir=CONF_DIR)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp, JSON_FILE_PATH)
    except:
        print_err(f"Error writing config.json to {JSON_FILE_PATH}")


