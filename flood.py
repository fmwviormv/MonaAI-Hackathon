# Copyright (C) 2024 by Ali Farzanrad <ali_farzanrad@riseup.net>
# All rights reserved

import argparse
import csv
import datetime
import enum
import http.client
import itertools
import json
from math import inf, sqrt
from pathlib import Path
from pyproj import Transformer
from shapely.geometry import Point, LineString
from typing import NamedTuple, get_type_hints

UTM_ZONE = 32  # Adjust this based on your location
PROJECT = Transformer.from_crs("EPSG:4326", f"EPSG:{32600 + UTM_ZONE}", always_xy=True)

class Record(NamedTuple):
    city_name: str
    dt: int
    lat: float
    lon: float
    rain_1h: float | None
    weather_description: str

class Location(NamedTuple):
    city_name: str
    lat: float
    lon: float

    def __str__(self):
        return f"{self.city_name} ({self.lat}, {self.lon})"

class Data(NamedTuple):
    loc: Location
    dt_from: int
    dt_to: int
    rain_24h: list[float]

class SkillLevel(int, enum.Enum):
    NONE = 0
    BASIC_SKILLS = enum.auto()
    INTERMEDIATE_SKILLS = enum.auto()
    ADVANCED_SKILLS = enum.auto()

    def __str__(self):
        return self.name.replace("_", " ").title()

class FloatRange(NamedTuple):
    start: float
    stop: float
    inc: bool

    def check(self, value) -> bool:
        if self.inc:
            return self.start <= value <= self.stop
        return self.start < value < self.stop

class RainToSkill(NamedTuple):
    rain: FloatRange
    weather: str
    skill: SkillLevel

class BusinessStatus(str, enum.Enum):
    OPERATIONAL = "OPERATIONAL"
    CLOSED_PERMANENTLY = "CLOSED_PERMANENTLY"

class FireStation(NamedTuple):
    business_status: BusinessStatus
    lat: float
    lon: float
    south: float
    west: float
    north: float
    east: float

def load_river(file):
    result = []
    element, = json.loads(Path(file).read_text())["elements"]
    for member in element["members"]:
        result.extend(PROJECT.transform(x["lat"], x["lon"]) for x in member.get("geometry", ()))
    return LineString(result)

def read_rain_to_skills(file):
    result = []
    with open(file, "r", encoding='utf-8-sig') as f:
        reader = csv.reader(f, delimiter=";")
        row = next(reader)
        wd = row.index("Weather Description")
        rs = row.index("Required Skill Level")
        ar = row.index("amount of rain")
        for row in reader:
            r = row[ar].strip()
            if r == "-":
                r = FloatRange(-inf, inf, False)
            elif r.startswith(">"):
                r = FloatRange(float(r[1:].strip()), inf, False)
            elif "-" in r:
                r = r.split("-", 1)
                r = FloatRange(float(r[0].strip()), float(r[1].strip()), True)
            else:
                r = float(r)
                r = FloatRange(r, r, True)
            s = getattr(SkillLevel, row[rs].upper().replace(" ", "_"))
            result.append(RainToSkill(rain=r, weather=row[wd], skill=s))
    return result

def read_csv(record_type, file, rename={}):
    if isinstance(file, str):
        file = open(file, "r")
    with file:
        reader = csv.reader(file)
        row = next(reader)
        th = {n: (row.index(rename.get(n, n)), t) for n, t in get_type_hints(record_type).items()}
        records = []
        for row in reader:
            rec = {}
            for (n, (i, t)) in th.items():
                v = row[i]
                if t == str | None:
                    v = v if v else None
                if t == float | None:
                    v = float(v) if v else None
                elif v:
                    v = t(v)
                else:
                    raise Exception
                rec[n] = v
            records.append(record_type(**rec))
    return records

RIVERS = {
    "Saar": load_river("saar.json"),
    "Blies": load_river("blies.json"),
}
RAIN_TO_SKILLS = read_rain_to_skills("data/rainToSkillTabel.csv")
FIRE_STATIONS = read_csv(FireStation, "data/fire_stations_locations/fire_stations/csv of fire stations.csv", {
    "lat": "geometry/location/lat",
    "lon": "geometry/location/lng",
    "south": "geometry/viewport/south",
    "west": "geometry/viewport/west",
    "north": "geometry/viewport/north",
    "east": "geometry/viewport/east",
    })

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=argparse.FileType("r"))
    parser.add_argument("-t", "--rain-threshold", type=float, default=50)
    parser.add_argument("-r", "--river-margin", type=float, default=1000)
    args = parser.parse_args()
    records = read_csv(Record, args.file)
    records.sort(key = lambda x: x._replace(rain_1h = 0))
    data = {}
    rain_flood = set()
    river_flood = {}
    for _, recs in itertools.groupby(records, key = lambda x: x[:-1]):
        recs = tuple(recs)
        rec = recs[0]
        loc = Location(city_name = rec.city_name, lat = rec.lat, lon = rec.lon)
        d = data.get(loc)
        if d is None:
            d = Data(loc = loc, dt_from = rec.dt - 3600, dt_to = rec.dt, rain_24h = [])
        elif loc != d.loc or rec.dt == d.dt_to + 3600:
            d = d._replace(dt_to = rec.dt)
        else:
            raise Exception(f"Expected loc={loc!r}, dt={d.dt_to + 3600}, but found {rec!r}")
        d.rain_24h.append(float(rec.rain_1h or 0))
        if len(d.rain_24h) > 24:
            del d.rain_24h[0]
        rain_24h = sum(d.rain_24h)
        if rain_24h >= args.rain_threshold:
            rain_flood.add(loc)
            p = Point(PROJECT.transform(loc.lat, loc.lon))
            for river in RIVERS:
                if RIVERS[river].distance(p) < args.river_margin:
                    river_flood[river] = min(river_flood.get(river, inf), RIVERS[river].project(p))
        skill = SkillLevel.NONE
        for rec in recs:
            for r2s in RAIN_TO_SKILLS:
                if r2s.weather == rec.weather_description and r2s.rain.check(rain_24h):
                    skill = max(skill, r2s.skill)
        if skill is not SkillLevel.NONE:
            print(f"{loc} needs {skill} at {datetime.datetime.utcfromtimestamp(rec.dt)} UTC")
        data[loc] = d
    print("\nFLOOD BY RAIN")
    for loc in rain_flood:
        print(loc)
    print("\nFLOOD BY RIVER")
    for loc, d in data.items():
        if loc in rain_flood:
            continue
        p = Point(PROJECT.transform(loc.lat, loc.lon))
        for river in RIVERS:
            if RIVERS[river].distance(p) < args.river_margin and RIVERS[river].project(p) >= river_flood.get(river, inf):
                print(f"{loc} near {river}")

if __name__ == "__main__":
    main()
