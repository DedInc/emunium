# tests/docker/Dockerfile
# Linux test image: the unit suite, then the e2e suite (DOM and real X11 input)
# against Debian's Chromium under Xvfb with a window manager. Built by
# tests/docker/run_linux.py from a context holding only the package, the tests
# and the project metadata.
FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# xclip: pyperclip's clipboard on X11, which the interactor pastes non-ASCII
# text through on Linux. Noto CJK and colour emoji render the typed text.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb xauth openbox chromium \
        fonts-dejavu-core fonts-noto-cjk fonts-noto-color-emoji \
        libx11-6 libxtst6 procps xclip \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash tester

WORKDIR /home/tester/emunium

# Dependencies first, against a placeholder package, so a source change does
# not reinstall them; the editable install points at this directory.
COPY pyproject.toml README.md LICENSE ./
RUN mkdir emunium \
    && touch emunium/__init__.py \
    && pip install -e ".[standalone]" "pytest>=8" "websockets>=14,<18" \
    && rm -rf emunium

# Docker Desktop runs containers on a WSL2 kernel ("microsoft" in
# /proc/version), and pyperclip then picks the Windows clipboard (clip.exe)
# before looking at DISPLAY; this container's clipboard is X11 through xclip.
RUN printf '%s\n' \
        '# Pin pyperclip to X11 (see tests/docker/Dockerfile).' \
        'import pyperclip' \
        'pyperclip.set_clipboard("xclip")' \
        > /usr/local/lib/python3.10/site-packages/sitecustomize.py

# Debian's Chromium opens its default search engine's start page
# (start.duckduckgo.com) as the first tab; a blank one keeps startup off the
# network, like Chrome for Testing's local new-tab page.
RUN mkdir -p /etc/chromium/policies/managed \
    && printf '{"NewTabPageLocation": "about:blank"}\n' \
        > /etc/chromium/policies/managed/emunium-tests.json

COPY --chown=tester:tester . .

USER tester

ENV EMUNIUM_CHROME_PATH=/usr/bin/chromium

# Both suites run under Xvfb with openbox: the unit suite, then the e2e suite
# plus the real-X11 backend checks. The second runs even when the first fails
# (full evidence in one log); the status is non-zero when either failed.
CMD ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", "sh", "-c", "openbox >/dev/null 2>&1 & sleep 1; python -m pytest -q -p no:cacheprovider tests; unit=$?; python -m pytest -q -p no:cacheprovider --e2e -m e2e tests/test_native_x11.py tests/e2e; e2e=$?; echo \"unit suite status $unit, e2e suite status $e2e\"; [ $unit -eq 0 ] && [ $e2e -eq 0 ]"]
