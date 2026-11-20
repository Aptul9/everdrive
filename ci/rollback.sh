#!/usr/bin/env bash
set -euo pipefail
BASE=/opt/everbright-drive
ENV_FILES=(--env-file "$BASE/.env")
if test -e "$BASE/oidc.env"; then ENV_FILES+=(--env-file "$BASE/oidc.env"); fi
if test "${1:-previous}" = disable; then
  if test -L "$BASE/app"; then CURRENT=$(readlink -f "$BASE/app"); else CURRENT=$(cat "$BASE/candidate-release"); fi
  case "$CURRENT" in "$BASE"/releases/*) ;; *) exit 1 ;; esac
  export RELEASE=${CURRENT##*/}
  # Retention remains enforced even while the public app is disabled.
  docker compose --project-directory "$CURRENT" "${ENV_FILES[@]}" stop web app
  test -s /etc/sslh.cfg.pre-everbright-drive
  cp /etc/sslh.cfg.pre-everbright-drive /etc/sslh.cfg
  systemctl restart sslh
  systemctl is-active --quiet sslh
  echo 'Drive disabled; data preserved and anonymous cleanup still running'
  exit 0
fi
test -s "$BASE/previous-release"
PREVIOUS=$(cat "$BASE/previous-release")
case "$PREVIOUS" in "$BASE"/releases/*) ;; *) exit 1 ;; esac
export RELEASE=${PREVIOUS##*/}
docker compose --project-directory "$PREVIOUS" "${ENV_FILES[@]}" up -d --wait --wait-timeout 120 --remove-orphans
ln -sfn "$PREVIOUS" "$BASE/app.next"
mv -Tf "$BASE/app.next" "$BASE/app"
# Database and uploaded files stay in place. No destructive data restore.
curl --fail --silent --show-error https://drive.everbright.it/healthz/
