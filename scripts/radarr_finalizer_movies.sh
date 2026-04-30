#!/usr/bin/env sh
set -eu

python3 /scripts/sonarr_post_import_finalizer.py \
  --config /config/sonarr-finalizer.yml \
  --instance movies \
  --url-mode docker
