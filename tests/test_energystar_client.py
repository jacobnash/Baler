"""
EnergyStarClient's XML parsing tested against canned response bodies
shaped exactly like the real v27.0 XSDs (account.xsd, common/links.xsd,
property/propertyMetrics.xsd) - no live Portfolio Manager account needed,
mocks requests.Session.get directly. Endpoint paths/auth verified
separately against the open-source Ruby client (see energystar_client.py
module docstring) - this only proves the parsing side.
"""

from unittest.mock import Mock, patch

from baler.energystar_client import EnergyStarClient

ACCOUNT_XML = b"""<account><id>12345</id><username>bob</username></account>"""

PROPERTY_LIST_XML = b"""<links>
  <link id="111" linkDescription="Building A" link="https://x/property/111" httpMethod="GET" hint="property"/>
  <link id="222" linkDescription="Building B" link="https://x/property/222" httpMethod="GET" hint="property"/>
</links>"""

METRICS_XML = b"""<propertyMetrics year="2026" month="7" propertyId="111">
  <metric name="score" id="1" uom="Score"><value>87</value></metric>
  <metric name="siteIntensity" id="2" uom="kBtu/ft2"><value>45.2</value></metric>
  <metric name="generationDate" id="3" dataType="date"><value>2026-08-01</value></metric>
</propertyMetrics>"""


def _mock_response(body: bytes) -> Mock:
    resp = Mock()
    resp.content = body
    resp.raise_for_status = Mock()
    return resp


def test_account_parses_id_and_username():
    client = EnergyStarClient("user", "pass")
    with patch.object(client._session, "get", return_value=_mock_response(ACCOUNT_XML)):
        assert client.account() == {"id": "12345", "username": "bob"}


def test_property_list_parses_id_and_description():
    client = EnergyStarClient("user", "pass")
    with patch.object(client._session, "get", return_value=_mock_response(PROPERTY_LIST_XML)):
        assert client.property_list(12345) == [
            {"id": "111", "name": "Building A"},
            {"id": "222", "name": "Building B"},
        ]


def test_property_metrics_parses_name_to_value():
    client = EnergyStarClient("user", "pass")
    with patch.object(client._session, "get", return_value=_mock_response(METRICS_XML)):
        result = client.property_metrics(111, 2026, 7, ["score", "siteIntensity", "generationDate"])
    assert result == {"score": "87", "siteIntensity": "45.2", "generationDate": "2026-08-01"}


def test_test_environment_is_the_default_url_prefix():
    client = EnergyStarClient("user", "pass")
    with patch.object(client._session, "get", return_value=_mock_response(ACCOUNT_XML)) as mock_get:
        client.account()
    called_url = mock_get.call_args[0][0]
    assert called_url == "https://portfoliomanager.energystar.gov/wstest/account"


def test_live_flag_switches_to_the_live_url_prefix():
    client = EnergyStarClient("user", "pass", live=True)
    with patch.object(client._session, "get", return_value=_mock_response(ACCOUNT_XML)) as mock_get:
        client.account()
    called_url = mock_get.call_args[0][0]
    assert called_url == "https://portfoliomanager.energystar.gov/ws/account"
