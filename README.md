# Concert Operate Demo Reservations (IBM Cloud Code Engine edition)

A web page where people reserve time on the **Robin** and **Yoda** Concert Operate demo environments and open each one from a link. Reservations are a courtesy calendar: they show others who is demoing when, but they don't lock anyone out of the clusters.

This repo runs the app on **IBM Cloud Code Engine**, using only free-tier allowances. For the version that runs on a RHEL VM (for example in TechZone), see the `concert-demo-reservations` repo. Both have the same features.

**What users see**
- **Status cards:** one card per environment showing "Available now" or "In use" (with who has it and until when), the next few reservations, and an **Open Robin / Open Yoda** button that goes to its launch page (section 4).
- **Sign-in (optional):** IBMers and the partners you approve log in with their IBMid (section 2).
- **Day schedule:** Robin and Yoda side by side. Click or drag to reserve, and click a reservation to see who has it.
- **Partner access, URL editing and history:** admins manage partners and the Robin/Yoda links from the page, and every reservation is kept with filters and a CSV download (sections 3 to 5).
- **Time zones:** each person sees times in their own local time zone.

**How the cloud version differs from the VM version**
- **Real HTTPS URL:** Code Engine gives the app an `https://...codeengine.appdomain.cloud` address with a real certificate, so there's no browser warning and no ports to open.
- **Scales to zero:** when nobody's using the page, the app shuts down. The first visit after a quiet spell takes a few seconds while it starts.
- **Storage:** data lives in a Cloud Object Storage bucket. The app loads it at startup and saves changes back about 15 seconds after they happen, plus a daily backup copy.
- **Settings:** settings live in a Code Engine secret, created from `config.cloud.json` on your Mac. Nothing secret goes into git or into the container image.
- **Lighter background refreshes:** the page refreshes once a minute, only while the tab is visible and in use, so idle tabs don't keep the app awake.

## 1. Deploy

**What you need**
- **An IBM Cloud account.** New accounts are Pay-as-you-go: you add a credit card for verification, but you aren't charged while you stay within the free allowances (see "Cost" below).
- **On your Mac:** the IBM Cloud CLI (`ibmcloud`), `git` and `python3`. The deploy script installs the Code Engine and Object Storage plugins itself.

**Steps**

```bash
git clone https://github.com/raghuhulgundi/concert-demo-reservations-cloud.git
cd concert-demo-reservations-cloud
ibmcloud login --sso
./deploy/codeengine.sh
```

The first run takes about 5 to 10 minutes. It:
- **Settings file:** creates `config.cloud.json` from the example, with sign-in off for now.
- **Storage:** creates an Object Storage instance, a Smart Tier bucket (the free option) and a Writer key for the app.
- **App:** creates a Code Engine project and a secret holding your settings, then builds the container and starts the app.
- **URL:** prints the app's URL and the exact redirect URL to register for IBMid.

Open the URL. Then:
1. Edit `config.cloud.json` with the Robin and Yoda console URLs (or set them later from the page with the Edit button), your email in `admin_emails`, and the IBMid settings (section 2).
2. Run `./deploy/codeengine.sh` again to apply them.

**Region:** the default is `us-south` (Dallas). To use another, run `REGION=us-east ./deploy/codeengine.sh` the first time. The script remembers your choices in `.cloud-deploy.env`, which is private and kept out of git.

**Updating to a new version**

```bash
git pull
./deploy/codeengine.sh
```

Deploy updates when nobody is booking. During the minute or so when the old and new copies of the app overlap, a booking made on the old copy could be lost.

## 2. Turn on IBMid sign-in

With sign-in on, anyone opening the page is sent to IBM to log in with their IBMid first. IBMers get in automatically; partners get in once you add them to the partner list (section 3). Their name and email fill in automatically, their reservations are tied to their account (so they can cancel from any browser), and you can name admins who can cancel anyone's booking.

**Step 1: register the app with IBM's SSO self-service tool**

Go to **w3.ibm.com/tools/sso** (IBM network or VPN) and register a new application. The screens change over time, but you'll be asked for roughly:

- **Protocol:** OpenID Connect (OIDC).
- **Identity provider:** IBMid. This lets IBMers, business partners and customers all sign in with their IBMid. (Don't pick w3id: it only works for IBM employees.)
- **Environment:** start with staging/pre-production to test if the tool offers it, then register production.
- **Redirect URL:** `<your app URL>/auth/callback` (the deploy script prints the exact value). This must match `redirect_uri` in `config.cloud.json` exactly, including `https://` and no trailing slash.
- **Grant type / client type:** authorization code, confidential client (has a client secret). Enable PKCE if asked.
- **Scopes / claims:** `openid`, `profile`, `email`.

When it's approved you get a **client ID**, a **client secret**, and a **discovery URL** (ends in `/.well-known/openid-configuration`).

**Step 2: fill in the `auth` block in `config.cloud.json`**

```json
"auth": {
  "enabled": true,
  "provider_name": "IBMid",
  "discovery_url": "https://login.ibm.com/oidc/endpoint/default/.well-known/openid-configuration",
  "client_id": "<from the SSO tool>",
  "client_secret": "<from the SSO tool>",
  "redirect_uri": "<your app URL>/auth/callback",
  "scopes": "openid profile email",
  "allowed_email_domains": ["ibm.com"],
  "allowed_emails": [],
  "admin_emails": ["you@ibm.com"],
  "session_hours": 12
}
```

- **`discovery_url`:** the one above is IBMid production. If you registered a staging environment, use the discovery URL the SSO tool shows you instead.
- **`admin_emails`:** put your own IBMid email here (and anyone who helps you run the page). Admins can manage the partner list and cancel any reservation from the page. **Set this before you go live**, otherwise nobody can add partners from the page.
- **Who can get in:** an access list is always on.
  - **IBMers:** any `@ibm.com` address, including country subdomains such as `us.ibm.com` or `in.ibm.com`, is always allowed. This is built in and can't be switched off by accident.
  - **Partners:** only people you add on the **Partner access** panel (section 3).
  - **`allowed_email_domains` / `allowed_emails` (optional):** a fixed list in the file, if you'd rather manage some entries there. Most people can leave these alone and use the panel.
- **Where the client secret lives:** `config.cloud.json` stays on your Mac (it's in `.gitignore`), and the deploy script copies it into the Code Engine secret. It never goes into git or the container image.

Run `./deploy/codeengine.sh` again. The app log (`ibmcloud ce app logs --name reservations`) should show `Sign-in with IBMid is on`.

**Troubleshooting**

- **"redirect_uri mismatch" on the IBM page:** the redirect URL registered in the SSO tool and `redirect_uri` in `config.cloud.json` differ. They must be identical.
- **"Sign-in unavailable, couldn't reach the sign-in service":** the app couldn't reach the discovery URL. Check `discovery_url` for typos, then look at the app log.
- **"didn't share an email address":** the app registration doesn't release the `email` claim. Add the email scope or claim in the SSO tool.
- **"Access needed" page:** the person's IBMid email isn't an IBM address and isn't on the partner list. Add them on the Partner access panel and ask them to click **Try a different account** (or reload). The page tells them which email it saw, which helps when a partner's IBMid uses a different address than you expected.

## 3. Manage partner access

Sign in with an email listed in `admin_emails`. A **Partner access** button appears under your name in the top right. It opens a panel where you can:

- **Add one person:** enter their email, for example `jane@partnerco.com`. Use this for partners whose IBMid is a personal address such as Gmail or Outlook.
- **Add a whole company:** enter the domain, for example `partnerco.com`. Everyone with an `@partnerco.com` IBMid (and subdomains such as `@emea.partnerco.com`) can sign in.
- **Record who they are:** the Company and Note fields are for your reference, for example "Reseller, joint demo team". The list also shows who added each entry and when.
- **Remove access:** click **Remove**, then **Confirm remove**. It takes effect on that person's next click, even if they're already signed in. Their past reservations stay on the calendar.

**Guardrails:**
- **Public email domains are blocked as whole domains:** you can't add `gmail.com`, `outlook.com` and similar as a domain, because that would let anyone in. Add those partners by individual email instead.
- **IBM addresses don't need adding:** they're always allowed.
- **Where the list lives:** in `data/reservations.db` alongside the bookings, so the normal backup covers it.

**Managing the list from the command line** (optional, uses the admin key):

```bash
# list
curl -H "X-Admin-Key: <ADMIN_KEY from .cloud-deploy.env>" <your app URL>/api/partners
# add a company or a person
curl -H "X-Admin-Key: <ADMIN_KEY from .cloud-deploy.env>" -H "Content-Type: application/json" \
  -d '{"value":"partnerco.com","company":"Partner Co","note":"Reseller"}' <your app URL>/api/partners
# remove (id from the list)
curl -X DELETE -H "X-Admin-Key: <ADMIN_KEY from .cloud-deploy.env>" <your app URL>/api/partners/<id>
```

## 4. Launch pages, notes and links for Robin and Yoda

Each environment has its own **launch page**, at `/env/robin` and `/env/yoda`. The **Open Robin** and **Open Yoda** buttons go there. A launch page shows:
- **Header:** the name and a short **tagline**, such as "App centric demo" or "Network centric demo". The tagline also appears on the main page cards.
- **Status:** live status (available, or who has it until when) and a **Launch console** button that opens the Concert Operate console in a new tab.
- **About this demo:** longer **details**, such as what the environment demos, scenarios, how to run it and who to ask. Line breaks are kept and web addresses become links.
- **Notes and links:** the short **notes** and a list of **useful links**, such as a demo script, a recording or a runbook.
- **Important notice:** an optional bold, highlighted box near the top, for rules people must not miss, such as "Before your demo". If its first line is short, it becomes the box's heading.
- **Reservations:** upcoming reservations, plus a **Reserve time** button that books right on the launch page, so you stay on it. Your own reservations there have a **Cancel** button.

**Editing (admins):** click **Edit** next to the name on the main page, or **Edit this page** on the launch page. You can change the tagline, console URL, description, notes, details and links. Enter links one per line as `Label | https://...`. In the details, wrap words in `**double asterisks**` to make them bold.

### Change the console URL

When a cluster is rebuilt or moved, update its link from the page. There's no need to redeploy.

1. Sign in as an admin (an email in `admin_emails`).
2. Click **Edit** next to **Robin** or **Yoda** on its card.
3. Change the **Console URL** (and anything else on the launch page), then click **Save**.

Everyone's page picks up the new link within 30 seconds, without reloading. Reservations aren't affected, because they belong to "Robin" or "Yoda", not to a URL.

**Change history:** each edit records the old value, new value, who changed it and when. You can see it under **Change history** in the Edit dialog.

**`config.cloud.json` vs. the page:** the URLs in `config.cloud.json` are the starting values. Once you've edited an environment on the page, the page value wins, and editing `config.cloud.json` for that environment no longer changes what users see. After the first page edit, keep using the page (or the command below).

**From the command line or a script** (for example right after rebuilding a cluster):

```bash
curl -X PUT -H "X-Admin-Key: <ADMIN_KEY from .cloud-deploy.env>" -H "Content-Type: application/json" \
  -d '{"url":"https://new-console-url.example.com"}' <your app URL>/api/environments/robin
```

Use `robin` or `yoda` (the `id` values in `config.cloud.json`). You can also send `tagline`, `description`, `notes`, `details` and `links` (a list of `{"label": ..., "url": ...}`).

## 5. Reservation history

Nothing is ever deleted. Cancelling a reservation marks it **cancelled**, with who cancelled it and when, and frees the slot on the calendar. Past reservations stay in the database too.

Click **History** (top right of the schedule) to see:
- **Every reservation:** date, time, environment, who, purpose, status (upcoming, in progress, completed or cancelled) and when it was booked.
- **Filters:** date range, environment, status, and a search box for name, email or purpose.
- **Totals:** reservation count and hours per environment for the current filters, not counting cancellations.
- **Download CSV:** the filtered list as a spreadsheet, with times in your local time zone plus UTC columns.

**Who sees what:** admins see everyone's history. Everyone else sees only their own reservations. With sign-in off, everyone sees everything.

**From the command line:**

```bash
curl -H "X-Admin-Key: <ADMIN_KEY from .cloud-deploy.env>" "<your app URL>/api/history?from=2026-11-01T00:00:00Z&env=robin&status=cancelled"
```


## 6. Cost: staying inside the free tier

**Code Engine (always free each month):** 100,000 vCPU-seconds, 200,000 GB-seconds and 100,000 HTTP requests.
- The app runs at 0.125 vCPU and 0.25 GB of memory, so that's roughly **220 hours of running time a month**.
- It only runs while people use the page, plus 2 minutes after the last request.

**Cloud Object Storage (Standard plan, Smart Tier, free for 12 months):** 5 GB of storage, 2,000 uploads and 20,000 downloads a month.
- The app uploads once per burst of changes and downloads once per start, so normal use stays far below these numbers.
- After 12 months, storage this small costs a few cents a month.

**Container Registry:** the build stores one small image in IBM Container Registry, well within its free allowance.

**Set a spending notification so nothing surprises you:** in the IBM Cloud console, go to **Manage**, then **Billing and usage**, then **Spending notifications**, and set an alert at, for example, USD 1.

**What could use up the free tier:**
- **Hundreds of people at once:** heavy, constant use.
- **Raising `min-scale` above 0:** this keeps the app running 24/7, which needs about 324,000 vCPU-seconds a month, over the free 100,000.

## 7. How the data is stored and backed up

- **Startup:** the app downloads `reservations.db` from the bucket before it accepts any requests. If it can't read the bucket (for example a wrong key or a network problem), it **refuses to start** rather than starting empty and overwriting your data. Code Engine retries automatically.
- **After each change:** the app waits about 15 seconds, so a burst of changes becomes one upload, then saves the whole database back to the bucket. When Code Engine shuts the app down, it saves any pending change first.
- **Save retries:** if a save fails (for example a transient network hiccup), the app retries automatically. The retry delay doubles after each consecutive failure — 30 s, 60 s, 120 s, up to a maximum of 5 minutes — so a prolonged COS outage doesn't flood the IBM Cloud API with rapid repeated attempts. The delay resets to 30 s after a successful save.
- **Daily backup:** each day, the first save also writes `backups/reservations-YYYY-MM-DD.db`.
- **Single copy:** `max-scale` is 1, so there's only ever one copy of the app writing to the bucket.

**Restore from a backup** (for example if something was deleted by mistake):

```bash
ibmcloud cos object-get --bucket <BUCKET> --key backups/reservations-2026-11-02.db --region us-south restore.db
ibmcloud cos object-put --bucket <BUCKET> --key reservations.db --body restore.db --region us-south
ibmcloud ce app update --name reservations --env RESTORED_AT="$(date +%s)"   # restarts the app so it loads the restored file
```

`BUCKET` is in `.cloud-deploy.env`. You can also browse the bucket in the IBM Cloud console under **Resource list**, then **Storage**.

## 8. Admin tasks and troubleshooting

**Logs:** `ibmcloud ce app logs --name reservations --follow`

**Health and storage status:** open `<your app URL>/healthz`. It shows the version, when data was last saved, and whether a save is pending.

**Command-line admin tasks** use the admin key in `.cloud-deploy.env`:

```bash
curl -H "X-Admin-Key: $ADMIN_KEY" "<your app URL>/api/history?status=cancelled"
curl -X DELETE -H "X-Admin-Key: $ADMIN_KEY" <your app URL>/api/bookings/<id>
```

**Common problems**
- **The first page load is slow:** the app was scaled to zero and is starting. That's normal, and it takes a few seconds.
- **The log says "Not starting, to protect saved data":** the app couldn't read the bucket. Run `./deploy/codeengine.sh` again to refresh the stored key, and check the bucket still exists.
- **Couldn't create the Object Storage instance:** your account needs to be Pay-as-you-go. Add a credit card in the console under **Manage**, then **Account**.
- **The build fails:** run `ibmcloud ce buildrun list`, then `ibmcloud ce buildrun logs --name <name>`.

## Notes

- **Not access control:** the page doesn't stop anyone from logging into Robin or Yoda outside their slot.
- **Sign-in is for this page only:** Robin and Yoda keep their own Concert Operate logins.
- **Secrets:** `config.cloud.json` and `.cloud-deploy.env` hold secrets. Both are in `.gitignore` and `.ceignore`, so they're never committed or uploaded with the build. The settings reach the app only through the Code Engine secret.
- **ID token signature:** per OpenID Connect Core §3.1.3.7, the app does not verify the JWT signature on the ID token. The token is received directly from the provider's token endpoint over verified TLS, which provides the same authenticity guarantee. Issuer, audience, expiry and nonce are still checked. If you ever route the token through a proxy or accept tokens from an untrusted source, add full JWT signature verification using the provider's JWKS endpoint.
- **Admin key security:** the admin key is stored in plaintext in `.cloud-deploy.env` (mode 0600, in `.gitignore`). Treat it like a password: don't share it, don't paste it into chat, and rotate it by running `./deploy/codeengine.sh` with `ADMIN_KEY=<new-value>`.
- **CLI access to history:** the `/api/history` endpoint accepts an `X-Admin-Key` header even when sign-in is on and no browser session exists. This is intentional so scripts can export data without a browser login. Protect the admin key accordingly.
