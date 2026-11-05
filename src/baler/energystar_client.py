"""
Read-only client for EPA's ENERGY STAR Portfolio Manager web services -
account/property/metrics only. Verified against the real v27.0 XSD bundle
(portfoliomanager-schemas-27.0.zip, EPA's own schema download) plus the
open-source Ruby client (github.com/mejackreed/portfolio_manager), which
implements the actual endpoint paths and auth scheme the XSDs alone don't
specify.

Auth is HTTP Basic against the account's real Portfolio Manager
username/password - that's PM's own web-service auth model, not a
separate revocable API key like Ecobee/Nest use. Test vs. live is a URL
path prefix on the same host (/wstest vs /ws), not a different
environment to configure separately.

The ENERGY STAR score is exactly one named metric, "score", requested via
the PM-Metrics header alongside whichever other metrics you want (e.g.
siteIntensity, sourceIntensity, totalGHGEmissions) - confirmed against
EPA's own "Get Available Metrics List" web service docs.

Meter-consumption write path (submitting energy data so PM can compute a
score) is NOT implemented yet - meter/meterConsumptionData.xsd shows the
payload shape, but the exact submit endpoint hasn't been verified against
a real account, so this stays read-only until that's confirmed live.
"""

import xml.etree.ElementTree as ET

import requests

BASE_URL = "https://portfoliomanager.energystar.gov"


class EnergyStarClient:
    def __init__(self, username: str, password: str, live: bool = False):
        self._session = requests.Session()
        self._session.auth = (username, password)
        self._session.headers["Accept"] = "application/xml"
        self._env_path = "/ws" if live else "/wstest"

    def _get(self, path: str, params: dict | None = None, headers: dict | None = None) -> ET.Element:
        resp = self._session.get(f"{BASE_URL}{self._env_path}{path}", params=params or {}, headers=headers or {})
        resp.raise_for_status()
        return ET.fromstring(resp.content)

    def account(self) -> dict:
        """GET /account - also doubles as the credential-verification call:
        Basic Auth failures surface here as a 401 via raise_for_status."""
        root = self._get("/account")
        return {"id": root.findtext("id"), "username": root.findtext("username")}

    def property_list(self, account_id) -> list[dict]:
        """GET /account/{id}/property/list - returns generic links
        (common/links.xsd), one per property: id + a human description."""
        root = self._get(f"/account/{account_id}/property/list")
        return [{"id": link.get("id"), "name": link.get("linkDescription")} for link in root.findall("link")]

    def property_metrics(self, property_id, year: int, month: int, metrics: list[str], measurement_system: str = "EPA") -> dict[str, str | None]:
        """GET /property/{id}/metrics for the 12 months ending year/month.
        Only handles non-monthly (single-value) metrics - "score" and the
        other whole-period metrics this needs. monthlyMetric (time-series)
        metrics parse as None here; add that shape if a caller needs one."""
        root = self._get(
            f"/property/{property_id}/metrics",
            params={"year": year, "month": month, "measurementSystem": measurement_system},
            headers={"PM-Metrics": ",".join(metrics)},
        )
        return {
            name: metric.findtext("value")
            for metric in root.findall("metric")
            if (name := metric.get("name")) is not None
        }
