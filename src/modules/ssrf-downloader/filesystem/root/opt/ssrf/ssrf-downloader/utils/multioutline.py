import json
import os
import re
import subprocess
import time
from shapely.geometry import LinearRing, Polygon
from shapely.ops import unary_union
from utils.util import make_int, print_err, get_plain_url

use_is_valid_reason = True
try:
    from shapely.validation import is_valid_reason
except:
    use_is_valid_reason = False
    from shapely.validation import explain_validity


class MultiOutline:
    def _get_outlines(self, num):
        data = []
        for i in range(1, num + 1):
            try:
                outline = json.load(open(f"/run/adsb-feeder-uf_{i}/readsb/outline.json"))
            except:
                pass
            else:
                data.append(outline)
        return data

    def _get_heywhatsthat(self, num):
        data = []
        hwt_feeders = []
        now = time.time()
        tar1090port=8080
        with open("/opt/adsb/config/.env", "r") as env:
            for line in env:
                match = re.search(r"AF_TAR1090_PORT=(\d+)", line)
                if match:
                    tar1090port = match.group(1)
                match = re.search(r"_ADSBIM_HEYWHATSTHAT_ENABLED_(\d+)=True", line)
                if match:
                    hwt_feeders.append(int(match.group(1)))
        for i in hwt_feeders:

            hwt_url = f"http://127.0.0.1:{tar1090port}/{i}/upintheair.json"
            response, status = get_plain_url(hwt_url)
            if status != 200:
                print_err(f"_get_heywhatsthat: http status {status} for {hwt_url}")
                continue
            try:
                hwt = json.loads(response)
            except:
                print_err(f"_get_heywhatsthat: json.loads failed on response: {response}")
            else:
                data.append(hwt)
        return data

    def create_outline(self, num):
        data = self._get_outlines(num)
        return self.create(data)

    def create_heywhatsthat(self, num):
        data = self._get_heywhatsthat(num)
        if len(data) == 0:
            return None
        result = {
            "id": "combined",
            "lat": data[0]["lat"],
            "lon": data[0]["lon"],
            "rings": [],
            "refraction": "0.25",
        }
        for idx in range(len(data[0]["rings"])):
            alt = data[0]["rings"][idx]["alt"]
            multi_range = self.create(data, hwt_alt=alt).get("multiRange")
            for i in range(len(multi_range)):
                result["rings"].append({"points": multi_range[i], "alt": alt})
        return result

    def create(self, data, hwt_alt=0):
        result = {"multiRange": []}
        polygons = []
        for i in range(len(data)):
            d = data[i]
            if hwt_alt == 0:
                if d.get("actualRange"):
                    points = d["actualRange"]["last24h"]["points"]
                else:
                    print(f"can't get points from outline #{i}: {d}")
                    points = []
            else:
                points = [r["points"] for r in d["rings"] if r["alt"] == hwt_alt][0]
            if len(points) > 2:
                try:
                    p = Polygon(shell=LinearRing(points))
                    if p:
                        if use_is_valid_reason:
                            r = is_valid_reason(p)
                            if r == "Valid Geometry":
                                polygons.append(p)
                            else:
                                print(f"can't create polygon from outline #{i} - {r}")
                        else:
                            try:
                                polygons.append(p)
                            except:
                                print(f"can't create polygon from outline #{i}")
                    else:
                        print(f"can't create polygon from outline #{i}")
                except:
                    print(f"can't create linear ring from outline #{i} - maybe there is no data, yet?")
        made_change = True
        look_at = range(1, len(polygons))
        while made_change:
            made_change = False
            to_consider = [0]
            for i in look_at:
                combined = False
                if not polygons[i] or not polygons[i].is_valid:
                    print(f"polygons[{i}]: {explain_validity(polygons[i])}")
                    polygons[i] = polygons[i].buffer(0.0001)
                for j in to_consider:
                    if not polygons[j].is_valid:
                        print(f"polygons[{j}]: {explain_validity(polygons[j])}")
                        polygons[j] = polygons[j].buffer(0.0001)
                    try:
                        if not polygons[j].disjoint(polygons[i]):
                            p = unary_union([polygons[j], polygons[i]])
                            polygons[j] = p
                            made_change = True
                            combined = True
                    except Exception as e:
                        print(f"exception {e} while combining polygons #{j} and #{i} for hwt_alt={hwt_alt}")
                        pass
                if not combined:
                    to_consider.append(i)
            look_at = to_consider[1:]
        for i in to_consider:
            try:
                coords = polygons[i].exterior.coords
                if len(coords[0]) == 3:
                    points = [[x, y] for x, y, a in coords]
                else:
                    points = [[x, y] for x, y in coords]
                result["multiRange"].append(points)
            except Exception as e:
                print_err(f"can't get points from polygon #{i} exterior coords: {e}")
                pass
        return result
