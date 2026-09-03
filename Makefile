VERSION := $(shell grep -m1 '^version = ' pyproject.toml | sed -E 's/version = "(.*)"/\1/')
TAG := v$(VERSION)

# Prefer the pipx editable install's own venv (`pipx install -e .`) -- it
# already has korecord plus every runtime/dev dependency installed.
# Falls back to plain `pytest` for anyone without that venv, which needs
# `pip install -e .[dev]` done first.
PIPX_VENV := $(HOME)/.local/share/pipx/venvs/korecord/bin/pytest
PYTEST := $(shell test -x $(PIPX_VENV) && echo $(PIPX_VENV) || echo pytest)

.PHONY: test release

test:
	$(PYTEST) tests/ -q

# Tags the version currently in pyproject.toml and pushes it, which is what
# .github/workflows/publish.yml watches for to build and publish to PyPI.
release: test
	@if [ -n "$$(git status --porcelain)" ]; then \
		git add -A; \
		git commit -m "Release $(TAG)"; \
	fi
	@if git rev-parse "$(TAG)" >/dev/null 2>&1; then \
		echo "release: tag $(TAG) already exists -- bump the version in pyproject.toml first" >&2; \
		exit 1; \
	fi
	git push origin HEAD
	git tag -a "$(TAG)" -m "Release $(TAG)"
	git push origin "$(TAG)"
	@echo "release: pushed $(TAG) -- https://github.com/r4ven-me/korecord/actions"
