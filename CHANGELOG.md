# Changelog

Versions follow `MAJOR.MINOR.PATCH`. Each release is tagged in git as `vX.Y.Z`.

## v1.1.1 (2026-10-30)

- **Reserve on the launch page:** Reserve time on Robin's or Yoda's launch page opens the booking form right there. The form suggests the next free slot and shows what's already booked that day. After booking, you stay on the launch page, with your reservation listed.
- **Cancel from the launch page:** your own upcoming reservations show a "YOU" tag and a Cancel button. Admins can cancel anyone's.
- **Status line:** it says "You have it until ..." when the current reservation is yours.

## v1.1.0 (2026-10-30)

- **Launch page per environment** (`/env/robin`, `/env/yoda`): tagline, live status, a Launch console button, details, notes, useful links and upcoming reservations. The Open buttons go here. Same as the VM edition v1.2.0.
- **Taglines on the main cards and new fields in the Edit dialog:** tagline, details and links. All changes are logged and saved to Object Storage like everything else.

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
