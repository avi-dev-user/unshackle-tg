# unshackle-tg: the unshackle engine + a Telegram frontend in one image (see README).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 HOME=/root DEBIAN_FRONTEND=noninteractive WVD_DIR=/data/wvd
WORKDIR /app

# System deps: ffmpeg (probe/mux/split), mkvtoolnix/aria2, git, curl/certs, supervisor,
# build-essential (compiles tgcrypto's C extension, no wheel on slim), unzip.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg mkvtoolnix aria2 git curl ca-certificates supervisor build-essential unzip \
    && rm -rf /var/lib/apt/lists/*

# DRM/download binaries unshackle needs that aren't in apt:
#   mp4decrypt (Bento4)  - Widevine/PlayReady CENC decryption (required for DRM services)
#   shaka-packager       - the default decrypter for modern CENC
RUN curl -fsSL -o /tmp/bento4.zip \
        "https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip" \
    && unzip -j /tmp/bento4.zip "*/bin/mp4decrypt" -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/mp4decrypt && rm -f /tmp/bento4.zip \
    && (curl -fsSL -o /usr/local/bin/packager \
        "https://github.com/shaka-project/shaka-packager/releases/download/v3.4.2/packager-linux-x64" \
        && chmod +x /usr/local/bin/packager \
        && ln -sf /usr/local/bin/packager /usr/local/bin/shaka-packager || echo "shaka-packager skipped") \
    && mp4decrypt 2>/dev/null | head -1 || true

# The unshackle engine + the bot's direct deps.
# NOTE: pinned to a fork while the REST API fixes (PRs upstream) are in review; switch the
# source to git+https://github.com/unshackle-dl/unshackle.git@<release> once they land.
# Cache-bust: this JSON changes when the engine's main HEAD moves, so the pip layer re-runs.
ADD https://api.github.com/repos/avi-dev-user/unshackle/commits/main /tmp/unshackle-main.json
COPY requirements.txt .
RUN pip install --no-cache-dir \
        "unshackle @ git+https://github.com/avi-dev-user/unshackle.git@main" \
        playwright yt-dlp requests bgutil-ytdlp-pot-provider \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir srt ftfy tinycss xmltodict \
    # unshackle pins subby via a uv-only git source pip ignores, so force the correct git rev.
    && pip install --no-cache-dir --force-reinstall --no-deps \
        "subby @ git+https://github.com/vevv/subby.git@1ea6a52028c5bea8177c8abc91716d74e4d097e1" \
    # Keep the engine's pinned cryptography (<46, for pyplayready) + curl-cffi (<0.14) so DRM works.
    && pip install --no-cache-dir "cryptography<46" "curl-cffi<0.14" \
    && python -c "import unshackle, yt_dlp, cryptography, curl_cffi, subby, xmltodict; print('engine deps OK')" \
    && playwright install --with-deps chromium

# Deno: the JS runtime yt-dlp needs for YouTube player extraction (nsig).
RUN curl -fsSL -o /tmp/deno.zip \
        https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin/ && chmod +x /usr/local/bin/deno && rm -f /tmp/deno.zip \
    && deno --version | head -1

# Example services. Add your own by mounting a directory and listing it in unshackle.yaml.
COPY services/ /app/services/

# Engine config into the installed package dir (unshackle finds it HOME-independently).
# unshackle.__file__ is None (PEP 420 namespace package), so resolve the dir via sysconfig.
COPY deploy/unshackle.yaml /app/unshackle.yaml
RUN cp /app/unshackle.yaml "$(python -c 'import sysconfig, os; print(os.path.join(sysconfig.get_paths()["purelib"], "unshackle"))')/unshackle.yaml" \
    && mkdir -p /root/.config/unshackle && cp /app/unshackle.yaml /root/.config/unshackle/unshackle.yaml

# Bot source + UI translations + process manager.
COPY src/ /app/src/
COPY locales/ /app/locales/
COPY deploy/supervisord.conf /etc/supervisor/conf.d/unshackle-tg.conf

# Data dir (override with a volume in production; the CDM lands in /data/wvd).
RUN mkdir -p /data/state /data/cookies /data/downloads /data/temp /data/logs /data/wvd

CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf", "-n"]
