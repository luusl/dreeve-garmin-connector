# dreeve-garmin-connector

Syncs your Garmin Connect activities into [Dreeve](https://github.com/robiningelbrecht/strava-statistics)'s
watch folder: it periodically lists new activities, downloads the original FIT files and delivers them
atomically.

```bash
make build-containers   # build the toolchain image
make test               # pytest
make lint               # ruff check + ruff format --check
make typecheck          # mypy --strict
make fix                # ruff autofix + format
make lock               # regenerate uv.lock after changing dependencies
```
