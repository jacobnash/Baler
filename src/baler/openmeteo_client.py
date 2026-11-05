"""
Read-only client for Open-Meteo's forecast API (open-meteo.com/en/docs) -
verified live against the real API: a single GET returns exactly the
`current` block requested, no API key/signup needed for non-commercial
use (the one puller in this repo with zero credential setup). Current
conditions are refreshed roughly every 15 minutes per Open-Meteo's own
docs, not a real-time feed.
"""

import requests

BASE_URL = "https://api.open-meteo.com/v1/forecast"


class OpenMeteoClient:
    def __init__(self, latitude: float, longitude: float):
        self._session = requests.Session()
        self.latitude = latitude
        self.longitude = longitude

    def current(self, variables: list[str]) -> dict:
        """GET .../forecast?current=<vars> - returns the `current` block
        verbatim (each requested variable name -> value, plus a `time`
        ISO string), not the whole response (elevation/current_units/
        hourly/etc. aren't needed downstream)."""
        params: dict[str, str | float] = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "current": ",".join(variables),
        }
        resp = self._session.get(BASE_URL, params=params)
        resp.raise_for_status()
        return resp.json()["current"]
