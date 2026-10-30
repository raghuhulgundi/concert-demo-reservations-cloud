# Changelog

Versions follow `MAJOR.MINOR.PATCH`. Each release is tagged in git as `vX.Y.Z`.

## v1.0.1 (2026-10-30)

- **Example settings:** `config.example.json` and the README use a placeholder admin email instead of a real one. Put your own email in `config.cloud.json`.

## v1.0.0 (2026-10-30)

First release of the IBM Cloud Code Engine edition, based on `concert-demo-reservations` v1.1.0 (the RHEL VM edition). It has the same features: reservation board, IBMid sign-in, partner access list, editable Robin/Yoda links and full reservation history.

Changes for Code Engine:
- **Settings from a secret:** settings come from the `RESERVE_CONFIG_JSON` environment variable (a Code Engine secret), and the app listens on `$PORT`. HTTPS is handled by Code Engine.
- **Storage in Cloud Object Storage:** the database is restored from the bucket at startup and saved back about 15 seconds after changes, with one upload per burst. Pending changes are saved on shutdown, and the first save each day also writes a backup copy. The app refuses to start if it can't read the bucket, so an empty database can never overwrite saved data.
- **Sign-in survives restarts:** the session signing key is stored in the database, so people stay signed in when the app scales to zero.
- **Fewer requests:** the page loads everything in one `/api/snapshot` request, refreshes every 60 seconds only while the tab is visible, and pauses after 15 idle minutes.
- **Deployment:** a `Dockerfile` and `deploy/codeengine.sh` set up Object Storage (Smart Tier bucket, Writer key), the Code Engine project, the secret and the app (0.125 vCPU, 0.25 GB, scale 0 to 1).
