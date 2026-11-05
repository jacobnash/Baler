FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
COPY rules/ ./rules/
COPY openapi.yaml ./
RUN pip install --no-cache-dir -e .

# Deterministic given mock_hospital.generate_site's fixed default seed -
# baked in at build time so mock_device has a --site-spec file to load
# with no extra volume/ordering to manage.
RUN python -m windrow.mock_hospital generate --out /app/hospital_site_spec.json

# No ENTRYPOINT/CMD - deploy/nash-srv/docker-compose.windrow.yml supplies
# each service's own `command:` against this one image.
