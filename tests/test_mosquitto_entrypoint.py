"""mosquitto/entrypoint.sh against the pinned broker image (Baler#79).

mosquitto 2.1 warns when a security file it opens isn't owned by the user
opening it, and says future versions will refuse it. The entrypoint used to
regenerate the persisted passwd in place: the first start created it as
root, chowned it to mosquitto, and every later start ran mosquitto_passwd
(as root) over that mosquitto-owned file - the warning on nash-srv.

The broker loads the file only after dropping to the mosquitto user, so the
fix can't simply make it root-owned: this runs the entrypoint twice on one
data volume (a restart), then the broker itself, and logs in through it."""

import pathlib
import re
import shutil
import subprocess
import uuid

import pytest

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parent.parent
IMAGE = re.search(r"image: (eclipse-mosquitto\S+)", (ROOT / "docker-compose.yml").read_text()).group(1)

# The entrypoint twice (its final exec stripped), then the broker as the
# entrypoint would start it, then a login with the account it wrote.
SCRIPT = r"""
set -e
sed 's/^exec mosquitto .*/true/' /mosquitto/config/entrypoint.sh > /tmp/prep.sh
sh /tmp/prep.sh
sh /tmp/prep.sh 2> /tmp/second.err
cat /tmp/second.err >&2
stat -c 'passwd %U:%G %a' /mosquitto/data/passwd
mosquitto -c /mosquitto/config/mosquitto.conf > /tmp/broker.log 2>&1 &
for i in $(seq 1 50); do
  mosquitto_pub -h 127.0.0.1 -t t -m x -u probe -P s3cret -q 0 && { echo LOGIN-OK; break; }
  sleep 0.2
done
cat /tmp/broker.log >&2
"""


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
def test_restart_leaves_no_ownership_warnings_and_broker_reads_passwd():
    volume = f"baler-test-mosq-{uuid.uuid4().hex[:8]}"
    try:
        run = subprocess.run(
            ["docker", "run", "--rm",
             "-e", "MOSQUITTO_USERS=probe:s3cret,baler:other",
             "-v", f"{ROOT / 'mosquitto'}:/mosquitto/config:ro",
             "-v", f"{volume}:/mosquitto/data",
             "--entrypoint", "sh", IMAGE, "-c", SCRIPT],
            capture_output=True, text=True, timeout=120, check=False,
        )
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, check=False)
    out, err = run.stdout, run.stderr
    assert run.returncode == 0, err
    assert "passwd mosquitto:mosquitto 600" in out
    assert not re.search(r"(owner|group) is not", err), err
    assert "world readable" not in err, err
    assert "LOGIN-OK" in out, err
