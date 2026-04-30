#!/usr/bin/env sh
set -eu

python3 /scripts/sonarr_post_import_finalizer.py \
  --config /scripts/sonarr-finalizer.yml \
  --instance anime \
  --url-mode docker
