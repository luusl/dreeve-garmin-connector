compose=docker compose
# Bootstrap helper: locking has to work before the dev image exists, so it runs
# against the plain uv image instead of our own.
uv-image=docker run --rm -v $(CURDIR):/app -w /app ghcr.io/astral-sh/uv:0.11.32-python3.13-trixie-slim

dc:
	@${compose} -f docker-compose.yml $(cmd)

dcr:
	@make dc cmd="run --rm python-cli $(cmd)"

build-containers:
	@make dc cmd="build"

down:
	@make dc cmd="down --remove-orphans"

shell:
	@make dcr cmd="bash"

# Dependency management.
lock:
	@$(uv-image) uv lock

lock-check:
	@make dcr cmd="uv lock --check"

upgrade:
	@$(uv-image) uv lock --upgrade

# Code quality tools.
test:
	@make dcr cmd="pytest $(arg)"

coverage:
	@make test arg="--cov --cov-report=term-missing --cov-report=html:var/coverage"

lint:
	@make dcr cmd="ruff check $(arg)"
	@make dcr cmd="ruff format --check"

typecheck:
	@make dcr cmd="mypy"

fix:
	@make dcr cmd="ruff check --fix"
	@make dcr cmd="ruff format"
