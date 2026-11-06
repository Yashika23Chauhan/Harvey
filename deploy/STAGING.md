# Harvey staging server

A staging server runs Harvey around the clock so you can test the real
15-minute loop, without it emailing anyone until you say so. These steps use
a DigitalOcean Droplet, but any Ubuntu 22.04 or 24.04 server works the same way.

## What staging mode means

`deploy/staging-setup.sh` leaves the server set up so tests are safe:

- **Approval on:** every email waits in the Outbox until you approve it.
- **Sending paused:** nothing goes out until you run `harvey sending resume`.
- **Free discovery:** `DATAFORSEO_SANDBOX=1` makes DataForSEO answer from its
  free sandbox. OpenStreetMap is free anyway.
- **Not on the internet:** the firewall only allows SSH. The dashboard listens on
  `127.0.0.1` and you open it through an SSH tunnel.

Give the server its own `.env` with test keys and a test mailbox. Don't copy
your local `.env` across.

## 1. Create the server

On DigitalOcean: **Create → Droplets**
- Image: **Ubuntu 24.04 LTS**
- Size: **Basic, 2 GB RAM** (1 GB runs out of memory when Chromium runs)
- Authentication: **SSH key** (recommended) or a password
- Note the server's IP address once it's created.

## 2. Copy Harvey onto it

From your own computer, in the folder that holds `harvey-project.zip`:

```
scp harvey-project.zip root@SERVER-IP:/opt/
```

Then sign in to the server and unzip it:

```
ssh root@SERVER-IP
apt-get update && apt-get install -y unzip
cd /opt && unzip harvey-project.zip
```

(Or `git clone` your repository into `/opt/harvey`, if your changes are pushed.)

## 3. Run the setup script

```
cd /opt/harvey
sudo bash deploy/staging-setup.sh
```

It takes about five minutes. It installs Python packages, Chromium and Claude
Code, creates a `harvey` user, adds swap, turns on the firewall and creates two
services: `harvey-dashboard` (started now) and `harvey-run` (started later).
Running it again is safe.

## 4. Sign in to Claude and train Harvey

```
sudo -iu harvey
cd /opt/harvey
claude                       # sign in (it shows a link to open in your browser), then /exit
nano .env                    # add test keys: email provider, a verifier
.venv/bin/harvey train https://your-website.com
exit
```

Then start the loop:

```
sudo systemctl start harvey-run
```

## 5. Open the dashboard

On your own computer:

```
ssh -L 5555:127.0.0.1:5555 root@SERVER-IP
```

Leave that window open and browse to **http://127.0.0.1:5555/**. The first time,
create the dashboard's account. After that, sign in with it.

## Everyday commands (on the server)

| What | Command |
|---|---|
| Is it running? | `systemctl status harvey-run harvey-dashboard` |
| Live log | `journalctl -u harvey-run -f` |
| Restart after a code change | `sudo systemctl restart harvey-run harvey-dashboard` |
| Stop the loop | `sudo systemctl stop harvey-run` |
| Pipeline summary | `sudo -iu harvey bash -c 'cd /opt/harvey && .venv/bin/harvey status'` |
| Start real sending | `sudo -iu harvey bash -c 'cd /opt/harvey && .venv/bin/harvey sending resume'` |

## Updating the code

Upload a new zip and unzip it over `/opt/harvey` (your `.env`, `data/` and
`harvey.local.yaml` aren't in the zip, so they're kept), then:

```
cd /opt/harvey && sudo bash deploy/staging-setup.sh
sudo systemctl restart harvey-run harvey-dashboard
```
