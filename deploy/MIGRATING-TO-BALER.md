# Migrating an install from Timberdoodle (+ FBF) to Baler (+ Windrow)

The rename keeps your data where it is. Nothing is copied or re-keyed:

| What | How it carries over |
|---|---|
| `TIMBERDOODLE_*` env vars | Every `BALER_*` setting falls back to its old name under docker compose, so an old `.env` keeps working. |
| Postgres role and database (`timberdoodle`) | Kept by setting `BALER_PG_USER` and `BALER_PG_DB`. |
| Docker volumes (`timberdoodle_*`) | Kept by setting `BALER_VOLUME_PREFIX` (plus `WINDROW_STATE_VOLUME=fbf-state` for the nash-srv demo overlay). |
| Graph vocabulary (`urn:timberdoodle:td#`, `haystack#`, `proj#`, the ontology graph) | Rewritten to `urn:baler:...` by the `migrate` compose service on the first `up`. It runs before every daemon, does nothing on later runs, and logs what it changed. |
| Windrow `FBF_*` env vars and `fbf-*.json` state files | `FBF_*` still read. Old state files are moved to their `windrow-*` names on first start. |
| MQTT topics `fbf/...`, point ids `urn:point:fbf/...` | **Unchanged on purpose.** Every point's history is keyed on them. |
| `timberdoodle.nash.engineering`, `/var/www/timberdoodle-docs` | Unchanged. They're public URLs served by Caddy and DNS; move them separately if you want. |

## Any docker-compose install (local, or a server you run by hand)

Add these to the `.env` you already have (or your `--env-file`):

```bash
BALER_PG_USER=timberdoodle
BALER_PG_DB=timberdoodle
BALER_VOLUME_PREFIX=timberdoodle
```

Then stop the stack under its old project name. This keeps the volumes, because there's no `-v`:

```bash
docker compose -p timberdoodle down
```

```bash
docker compose up -d --build --wait
```

```bash
docker compose logs migrate
```

The last command should print `migrate: rewrote N triple(s) ...` on the first run and `rewrote 0` after that.

Bare-metal `python -m baler.*` processes don't go through compose, so rename the variables they read
(`TIMBERDOODLE_PG_DSN` → `BALER_PG_DSN`, and the rest) in your shell or service files.

## nash-srv (self-hosted deploy runner)

Renaming the GitHub repo moved the runner's checkout from `_work/timberdoodle/timberdoodle` to
`_work/baler/baler`. The old root-owned wrapper (`/usr/local/sbin/timberdoodle-deploy.sh`) only knows
the old path, so it would keep redeploying stale code. `deploy.yml` now waits for a runner labelled
`baler` instead, so no deploy runs until you've done the steps below. Run them as your own sudo user
on nash-srv.

**1. Stop the old stack.** This keeps the volumes. It also frees the ports and the `172.30.0.0/24` demo
subnet, which the new project's network needs.

```bash
sudo docker compose -p timberdoodle down
```

**2. Copy the prod env file to the new location** and add the legacy settings:

```bash
sudo install -d -o github-runner -g github-runner -m 700 /home/github-runner/.config/baler
```

```bash
sudo install -o github-runner -g github-runner -m 600 /home/github-runner/.config/timberdoodle/nash-srv.env /home/github-runner/.config/baler/nash-srv.env
```

```bash
printf '%s\n' BALER_PG_USER=timberdoodle BALER_PG_DB=timberdoodle BALER_VOLUME_PREFIX=timberdoodle WINDROW_STATE_VOLUME=fbf-state | sudo tee -a /home/github-runner/.config/baler/nash-srv.env
```

The existing `TIMBERDOODLE_*` secrets in that file keep working. Renaming them to `BALER_*` is
optional.

**3. Remove the old runner, wrapper and sudoers rule.** Get a removal token from
GitHub → jacobnash/baler → Settings → Actions → Runners → `nash-srv-timberdoodle` → Remove.

```bash
cd /home/github-runner/actions-runner-timberdoodle && sudo ./svc.sh stop && sudo ./svc.sh uninstall
```

```bash
cd /home/github-runner/actions-runner-timberdoodle && sudo -u github-runner ./config.sh remove --token <REMOVAL_TOKEN>
```

```bash
sudo rm -f /usr/local/sbin/timberdoodle-deploy.sh /etc/sudoers.d/github-runner-timberdoodle
```

**4. Install the new runner and wrapper.** Get a registration token from
GitHub → jacobnash/baler → Settings → Actions → Runners → New self-hosted runner.

```bash
git clone https://github.com/jacobnash/baler /tmp/baler-setup
```

```bash
sudo bash /tmp/baler-setup/deploy/nash-srv/setup-runner.sh <REGISTRATION_TOKEN>
```

It leaves the env file from step 2 alone, because it already exists.

**5. Deploy.** In GitHub → Actions → Deploy, click **Run workflow**, or push anything to `main`. Then check:

```bash
sudo docker compose -p baler logs migrate
```

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8080/auth/login -H 'Content-Type: application/json' -d '{}'
```

The first should show the namespace rewrite, and the second should return a `4xx`, not `000`. The demo
account, history and provisioned demo devices should all be there as before.

**6. Clean up later**, once you're happy. The volumes are not involved here; they're in use under
their old names.

```bash
sudo rm -rf /home/github-runner/actions-runner-timberdoodle /home/github-runner/.config/timberdoodle /tmp/baler-setup
```
