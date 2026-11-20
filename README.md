# EverDrive

Personal file storage at `https://drive.everbright.it`. Login through EverID; every user with access sees and manages the same shared data.

Authenticated uploads stay private and have no automatic expiry. Anonymous uploads produce unguessable download links, accessible without login. No public file directory exists. The owner can manage temporary files; anonymous uploaders can remove their own files from the browser that uploaded them.

## Login

EverID (Authentik at `id.everbright.it`) is the primary login: OIDC authorization code with PKCE, client `everdrive`, callback `/oidc/callback`, scopes `openid email profile`. Access requires group `app-drive` in the `groups` claim; other EverID users get HTTP 403 and no session. Users are matched by email (case-insensitive) or created on first login without a usable password. Logout ends the local session and redirects to the EverID end-session endpoint with `id_token_hint`. Anonymous upload and share links stay public.

Unauthenticated access first requests silent OIDC authentication. When interaction is required, the browser goes to the central login page. Logout suppresses silent authentication until the user chooses to sign in again. The application does not accept a credential form. `LEGACY_LOGIN=0` invalidates existing legacy sessions; startup refuses that setting without OIDC client credentials.

## Storage

Initial quota equals available filesystem space minus 20 decimal GB and 64 MiB operational headroom. Quota persists in SQLite. Live availability also accounts for disk consumption by other services and for pending uploads. Files are uploaded in verified 4 MiB chunks. Space is reserved before upload; each disk write checks live free space. Interrupted uploads expire after 15 idle minutes. File allocation includes filesystem rounding and metadata allowance.

The app cannot prevent unrelated VM services from consuming disk space. Its own uploads stop before touching the reserved 20 GB. The interface displays Drive capacity only, in decimal GB.

## Retention

Anonymous expiry is no later than 48 hours from upload start. Expired links are rejected even if cleanup has not run. A separate cleanup container runs every 10 seconds and removes files up to 30 seconds early, leaving margin before the deadline. Cleanup also runs before every deployment starts serving requests. Private files are never expired. Anonymous file contents must not be copied into backups.

## Deployment

`.github/workflows/ci-cd.yml` tests Python behavior, routing transformations and production containers. Pull requests run checks. Main deploys through SSH using `ci/deploy.sh`; no manual application copy or server-side code edits are required. The source-controlled script prepares persistent storage, migrates SQLite, creates the owner only when absent, configures the new sslh route and starts Compose.

Required repository secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`, `DJANGO_SECRET_KEY`, `OWNER_PASSWORD`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`; variables `OIDC_ISSUER`, optional `LEGACY_LOGIN`. Deploy stops when the EverID client is missing. EverID settings are rewritten to `oidc.env` on every deploy. Runtime secret remains unchanged on later deploys. Existing owner password is never reset by deployment. Runtime files live outside release directories in `/opt/everbright-drive/data`; Compose publishes only `127.0.0.1:4447`.

DNS record A `drive.everbright.it` must resolve to the VM before first deployment. Caddy obtains its certificate through TLS-ALPN via sslh. The existing services page has its own pipeline and is updated separately after Drive is reachable.

## Rollback

The Rollback workflow runs `ci/rollback.sh`. Operation `previous` starts the recorded previous release using the same persistent data. Operation `disable` stops the new web application and restores `/etc/sslh.cfg.pre-everbright-drive`, while keeping anonymous cleanup running. No private file or database is deleted. SQLite schema changes require backward-compatible migrations. The pipeline records an online database backup, excluding file contents, before migration on subsequent releases. Existing services app backup lives in `/opt/servizi/app.pre-drive`.

## Local checks

Set `DJANGO_SECRET_KEY`, `DRIVE_DATA_DIR`, `DRIVE_ALLOWED_HOSTS=localhost,127.0.0.1,testserver` and `DRIVE_INSECURE_LOCAL=1`. Create the data directory, then run:

```sh
python -m pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py bootstrap_owner < owner-password.txt
python manage.py test storage
python -m unittest discover -s ci -p 'test_*.py'
python manage.py runserver 127.0.0.1:8791 --noreload --insecure
```

Supply the owner password through stdin, never a committed file. `DRIVE_QUOTA_BYTES` can lower the initial quota in an isolated local environment. `DRIVE_ANONYMOUS_TTL_SECONDS` can shorten retention for tests; values greater than 48 hours are rejected. Production Compose exposes neither override.

## Traps

- GitHub-hosted runners can have less than 20 GB free. Container integration tests use a shared 24 GiB sparse tmpfs fixture and a 64 MiB application quota. Only test payload bytes allocate memory. The application container keeps tmpfs mounted across migration and bootstrap containers; unmounting the last consumer discards tmpfs contents. The production reserve check remains active in the tests.
- Same-origin form submissions require `Referrer-Policy: same-origin`. Do not enable `no-referrer` globally or whitelist `Origin: null` to bypass CSRF checks.
