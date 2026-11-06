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


# Unit systems we ask for explicitly (Baler#53) rather than taking Open-Meteo's
# metric defaults silently: request params per system.
UNIT_PARAMS = {
    "metric": {"temperature_unit": "celsius", "wind_speed_unit": "kmh"},
    "imperial": {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph"},
}
# Open-Meteo's unit spellings -> Haystack's (project-haystack.org units).
HAYSTACK_UNITS = {"mp/h": "mph", "km/h": "km/h", "°C": "°C", "°F": "°F", "%": "%", "°": "°", "m/s": "m/s", "kn": "kn"}


class OpenMeteoClient:
    def __init__(self, latitude: float, longitude: float, units: str = "metric"):
        if units not in UNIT_PARAMS:
            raise ValueError(f"units must be one of {', '.join(UNIT_PARAMS)}")
        self._session = requests.Session()
        self.latitude = latitude
        self.longitude = longitude
        self.units = units

    def current_with_units(self, variables: list[str]) -> tuple[dict, dict]:
        """(the `current` block, {variable: Haystack unit}) - Open-Meteo says
        what unit each value is in (`current_units`); keep it."""
        body = self._get(variables)
        units = {k: HAYSTACK_UNITS.get(v, v) for k, v in (body.get("current_units") or {}).items()
                 if k not in ("time", "interval")}
        return body["current"], units

    def _get(self, variables: list[str]) -> dict:
        params: dict[str, str | float] = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "current": ",".join(variables),
            "timezone": "GMT",
            **UNIT_PARAMS[self.units],
        }
        resp = self._session.get(BASE_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    def current(self, variables: list[str]) -> dict:
        """GET .../forecast?current=<vars> - returns the `current` block
        verbatim (each requested variable name -> value, plus a `time`
        ISO string), not the whole response (elevation/current_units/
        hourly/etc. aren't needed downstream)."""
        return self._get(variables)["current"]
