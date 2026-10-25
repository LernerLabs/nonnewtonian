# NonNewtonian Physicists operator runbook (M7)

**You run every server command yourself.** Nothing in this repo, and no
Claude session, connects to the server. Commands marked **LAPTOP** run on
your Mac; everything else runs on the server over ssh. Every server step is
a copy-paste block followed by a "You should see:" line; if what you see
differs, stop there.

This runbook matches the code on `main` as of the M7 review fixes
(`66f387b`), not the M7 design spec where the two differ. Where the code
deliberately differs from the original plan, the limitation is stated where
it matters to you and collected in [Known limitations](#known-limitations).

Backups have their own runbook, `deploy/backup/README.md`; this file links to
it rather than repeating it.

## Contents

- [Glossary and placeholders](#glossary-and-placeholders)
- [What you must do personally](#what-you-must-do-personally)
- [Server layout and configuration](#server-layout-and-configuration)
- [Session 1: minimum viable launch](#session-1-minimum-viable-launch)
- [Updating](#updating)
- [Session 2: monitoring and drills](#session-2-monitoring-and-drills)
- [Session 3: the static mirror](#session-3-the-static-mirror)
- [Session 4 (optional): email recovery](#session-4-optional-email-recovery)
- [Drills](#drills)
- [Lost manage link](#lost-manage-link)
- [Takedown](#takedown)
- [Restore and rebuild](#restore-and-rebuild)
- [Away for a month](#away-for-a-month)
- [Incident quick reference](#incident-quick-reference)
- [Known limitations](#known-limitations)
- [Drill log](#drill-log)

## Glossary and placeholders

- **VPS** (Virtual Private Server): the DreamCompute instance (DreamHost's
  cloud-server product) the app runs on.
- **LTS** (Long Term Support): Ubuntu 24.04 LTS, the server OS.
- **TLS** (Transport Layer Security): the "s" in https. Caddy (the web
  server in front of the app) gets and renews the certificate itself.
- **ACME** (Automatic Certificate Management Environment): the protocol Caddy
  uses to get Let's Encrypt certificates. `NNP_ADMIN_EMAIL` is the ACME
  contact address Let's Encrypt writes to about expiry problems.
- **HSTS** (HTTP Strict Transport Security): the
  `Strict-Transport-Security: max-age=31536000` header Caddy sends. Browsers
  that saw it refuse plain http to nonnewtonian.org for a year; there is no
  going back to http-only for returning visitors.
- **WAL** (write-ahead log): SQLite's journal mode here. The database is
  `app.db` plus the sidecar files `app.db-wal` and `app.db-shm`. A stale
  `-wal` left next to a restored `app.db` is replayed into it and corrupts it.
- **gthread**: gunicorn's threaded worker class. The app runs as ONE process
  with 8 threads (`deploy/gunicorn.conf.py`).
- **OOM** (out of memory): the kernel or systemd killing a process that went
  over its memory limit (`MemoryMax=400M` in `nnp.service`).
- **NAT** (Network Address Translation): a school's many devices share one
  public IP, so a per-IP rate limit applies to a whole class at once.
- **XFF** (X-Forwarded-For): the header in which Caddy passes the real client
  IP to the app. The app trusts exactly one hop, and only because gunicorn
  listens on 127.0.0.1 where Caddy is the sole client.
- **CSRF** (Cross-Site Request Forgery): forged form posts from another site.
  Every form carries a token tied to the session cookie.
- **Dead-man check vs uptime prober**: a dead-man check (healthchecks.io)
  alerts when an expected ping does NOT arrive, so it catches a job or a whole
  box that silently stopped. An uptime prober (UptimeRobot) polls the site
  from outside and alerts when it answers wrong. You need both: the prober
  cannot see a dead backup, and the dead-man check cannot see a slow 502.
- **B2**: Backblaze B2, the object store the encrypted backups live in.
- **restic**: the backup tool; it encrypts client-side with a passphrase that
  only you (and the server's root-only env file) hold.
- **Deploy key**: an ssh key GitHub attaches to ONE repository rather than to
  your account. The mirror's key can write only to `nonnewtonian-mirror`.
- **Orphan (parentless) commit**: a git commit with no parent. Each mirror
  push replaces `gh-pages` with one such commit, so taken-down content is not
  in the branch's history.
- **SHA**: a git object id (40 hex characters).
- **SMTP** (Simple Mail Transfer Protocol) and **STARTTLS** (upgrading an
  SMTP connection to TLS on port 587): how the optional `/recover` flow sends
  mail.
- **SPF / DKIM** (Sender Policy Framework / DomainKeys Identified Mail): DNS
  TXT records that let receiving mail servers verify mail "from"
  nonnewtonian.org really came from your provider. Without them, recovery
  mail lands in spam.
- **TTL** (time to live): how long resolvers cache a DNS record.
- **MP** (megapixels): image size in millions of pixels; a recent phone photo
  is about 12 MP.

Placeholders you substitute:

| Placeholder | Meaning                                                      |
| ----------- | ------------------------------------------------------------ |
| `SERVER_IP` | the instance's public IPv4 address                           |
| `ADMIN_URL` | `https://nonnewtonian.org/admin/<NNP_ADMIN_TOKEN>`           |
| `<slug>`    | a class slug (from its `/c/<slug>` link) or a scientist slug |
| `YOUR-UUID` | the per-check part of a healthchecks.io ping URL             |
| `BUCKET`    | your B2 bucket name                                          |

## What you must do personally

Session 1 (minimum viable launch; students can submit at the end). Minutes
are estimates.

| #   | Step                                                                                                                          | Min |
| --- | ----------------------------------------------------------------------------------------------------------------------------- | --- |
| 1   | Review the M7 commits (`git log --oneline 60579e4..66f387b`); confirm the submit limit `200 per hour;60 per minute`; say go   | 25  |
| 2   | Accounts: password-manager entries, B2 bucket + scoped key, healthchecks.io `nnp-backup` check, UptimeRobot, auto-renew (1.1) | 15  |
| 3   | DreamCompute 512MB Ubuntu 24.04 instance, ssh key, security group 22/80/443 (1.2)                                             | 15  |
| 4   | DNS A + www CNAME; `dig` verify (1.3)                                                                                         | 10  |
| 5   | ssh, clone, first `install.sh`; save the two secrets; verify the CLI, gunicorn, caddy validate, log redaction (1.4)           | 20  |
| 6   | Seed on the laptop with the original decks, upload DB + photos, rerun `install.sh` (1.5-1.6)                                  | 25  |
| 7   | Phone smoke test: approve seeds, class, two submissions, moderate, slide + deck, deck timing, class-deck MemoryPeak (1.7)     | 20  |
| 8   | Backups: `deploy/backup/README.md` steps 4-6 (`--enable-backups`), green check (1.8)                                          | 20  |
| 9   | UptimeRobot monitor (1.9)                                                                                                     | 5   |
| 10  | Drill D1a (kill -9, Caddy serves photos while the app is stopped) (1.10)                                                      | 10  |
| 11  | Hand the class its submit link; store the manage link and kit (1.11)                                                          | 5   |
|     | Total                                                                                                                         | 170 |

Session 2 and later:

| #   | Step                                                                                                                | Min |
| --- | ------------------------------------------------------------------------------------------------------------------- | --- |
| 12  | Verify unattended upgrades cover Caddy (already installed in Session 1); `--enable-selfcheck` + its check (2.1-2.2) | 15  |
| 13  | Drills D1b (12-minute stop, DOWN/UP emails) and D1c (reboot)                                                        | 25  |
| 14  | Drill D2 (laptop restore from password-manager credentials)                                                         | 30  |
| 15  | Drill D3 (fail path + silence path)                                                                                 | 25  |
| 16  | Decided 2026-10-25: class pages stay out of the mirror; privacy wording approved; submit limit 200/h, 60/min        | 0   |
| 17  | Mirror: repo, `--mirror-key`, deploy key, fingerprint check, mirror.env, `--enable-mirror`, Pages, down.html        | 35  |
| 18  | Drills D4 (takedown, incl. the old-SHA record) and D5 (file://)                                                     | 45  |
| 19  | Optional SMTP: provider, SPF/DKIM, four vars, restart, end-to-end re-issue test, spam-folder check                  | 45  |
| 20  | B2 lifecycle "keep prior versions 30 days"; .com DNS + uncomment the Caddy redirect (2.4)                           | 15  |

Corrections to the spec's version of these tables: unattended upgrades and
the journald cap are installed by the plain `install.sh` run in Session 1
(the spec had them in Session 2); SMTP needs four variables, not five
(`NNP_SMTP_PORT` is optional, default 587); step 5 now includes checking
that the `nonnewtonian` command and gunicorn installed, which could not be
installed where the code was written.

## Server layout and configuration

```
/opt/nnp                      root-owned git checkout (never edit on the server); /opt/nnp/.venv
/etc/nnp/nnp.env              root:root 0600, app env (systemd reads it as root)
/etc/nnp/last-good-commit     commit that last passed install.sh's healthz check (rollback)
/etc/nnp/monitor.env          root:root 0600, self-check ping URL (Session 2)
/etc/nnp/mirror.env           root:root 0600, mirror config (Session 3)
/etc/nnp-backup.env           root:root 0600, restic/B2 config (deploy/backup/README.md)
/etc/nnp-backup/restic-pass   root 0600 (directory 0700), restic passphrase
/var/cache/nnp-backup         nightly backup's restic cache (by hand: /root/.cache/restic); disposable
/var/lib/nnp                  nnp:nnp 0750; user caddy is in group nnp
/var/lib/nnp/app.db           nnp:nnp 0600 (+ -wal/-shm)
/var/lib/nnp/photos/          content-hash photo tree, files 0640; Caddy serves it directly
/var/lib/nnp/export/current/  static export; Caddy's outage fallback; only down.html until Session 3
/var/lib/nnp/mirror.git       local bare repo for mirror commits (Session 3)
/var/lib/nnp/.ssh/            mirror deploy key + pinned github.com host key (Session 3)
/var/lib/nnp-backup/staging   root 0700, plaintext DB snapshot during a backup
```

Units and timers: `nnp.service` (gunicorn, 127.0.0.1:8000, `Restart=always`),
`caddy.service`, `nnp-backup.timer` (03:30 + up to 15 min),
`nnp-mirror.timer` (04:30 + up to 10 min), unattended-upgrades reboot if
needed at 05:30, `nnp-selfcheck.timer` (06:15). All timers are
`Persistent=true` (they run at boot if the box was off at their time).

Environment variables:

| Variable                 | File                   | Meaning                                                                                        | Required  |
| ------------------------ | ---------------------- | ---------------------------------------------------------------------------------------------- | --------- |
| `NNP_DB`                 | `/etc/nnp/nnp.env`     | `/var/lib/nnp/app.db`; must exist, be absolute, mode 0600, fully migrated                      | yes       |
| `NNP_PHOTOS`             | nnp.env                | `/var/lib/nnp/photos`; absolute                                                                | yes       |
| `NNP_SITE_URL`           | nnp.env                | `https://nonnewtonian.org`; must start with https://                                           | yes       |
| `NNP_SECRET_KEY`         | nnp.env                | session/CSRF/signing key, >= 32 chars, generated by install.sh; must stay stable               | yes       |
| `NNP_ADMIN_TOKEN`        | nnp.env                | the `/admin/<token>` capability, >= 32 chars, never `dev-admin-token`; generated by install.sh | yes       |
| `NNP_ADMIN_EMAIL`        | nnp.env                | ACME contact; install.sh writes it into the Caddyfile (rerun install.sh after changing it)     | no        |
| `NNP_SMTP_HOST`          | nnp.env                | SMTP host; HOST, USER, PASSWORD and MAIL_FROM all set or all unset                             | no        |
| `NNP_SMTP_PORT`          | nnp.env                | default 587 (also when empty); must be an integer; STARTTLS only (port 465 does not work)      | no        |
| `NNP_SMTP_USER`          | nnp.env                | SMTP login                                                                                     | no        |
| `NNP_SMTP_PASSWORD`      | nnp.env                | SMTP password                                                                                  | no        |
| `NNP_MAIL_FROM`          | nnp.env                | From address on nonnewtonian.org                                                               | no        |
| `NNP_BACKUP_PING_URL`    | `/etc/nnp-backup.env`  | healthchecks.io URL; the backup refuses to run without it                                      | yes       |
| `NNP_BACKUP_STAGING`     | nnp-backup.env         | default `/var/lib/nnp-backup/staging`; refused if under `/var/lib/nnp`                         | no        |
| `NNP_BACKUP_TAG`         | (never in a file)      | install.sh sets `pre-migration` for one run; any other non-empty value is refused              | no        |
| `RESTIC_REPOSITORY` etc. | nnp-backup.env         | restic/B2 config; see `deploy/backup/nnp-backup.env.example`                                   | yes       |
| `NNP_SELFCHECK_PING_URL` | `/etc/nnp/monitor.env` | healthchecks.io URL for the daily self-check                                                   | Session 2 |
| `NNP_MIRROR_REMOTE`      | `/etc/nnp/mirror.env`  | `git@github.com:mglerner/nonnewtonian-mirror.git`                                              | Session 3 |
| `GIT_SSH_COMMAND`        | mirror.env             | pins the deploy key and the recorded github.com host key                                       | Session 3 |
| `NNP_MIRROR_PING_URL`    | mirror.env             | optional healthchecks.io URL for the nightly mirror                                            | no        |
| `NNP_MIRROR_GITDIR`      | mirror.env             | local bare repo, default `/var/lib/nnp/mirror.git`                                             | no        |

nnp.env format: systemd, install.sh and `deploy/nnp-cli` all read it
literally (nnp-cli and install.sh no longer source it as shell), so write
`KEY=value` with nothing around it: spaces and `$` in a value need no
quoting, and one pair of quotes around the whole value is stripped.
Backslashes, line continuations and indented lines are not supported.

The photo fetcher ignores `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY`
(`session.trust_env = False` in `photos.py`): a proxied fetch would bypass
both the check that pins each fetch to the vetted public IP address (so a
link cannot reach an internal address) and the 20 s fetch deadline. Still do
not set them for nnp.service; 1.4 checks that none reach it, as a belt and
braces measure.

If any required nnp.env check fails, the app refuses to boot and
`journalctl -u nnp` shows `production preflight failed:` followed by EVERY
problem at once (one line each). Among them: `database missing at ...; seed
it on the laptop, upload it there, and rerun: sudo /opt/nnp/deploy/install.sh
...`, `pending migrations ...; run: sudo /opt/nnp/deploy/nnp-cli migrate --db
...`, and `NNP_SMTP_PORT must be an integer (got ...)`.

`deploy/nnp-cli` runs the `nonnewtonian` command (subcommands `seed-import`,
`migrate`, `export-static`) as user nnp with nnp.env loaded. Give it literal
paths: `sudo /opt/nnp/deploy/nnp-cli migrate --db /var/lib/nnp/app.db`. A
`"$NNP_DB"` on that command line expands in YOUR shell, where it is empty,
before nnp-cli loads nnp.env. `migrate` on a missing database exits 1
(`database missing at ...; pass --create for a new EMPTY one ...`) instead of
creating one; `--create` makes a new EMPTY database (mode 0600) and is never
the production fix: upload the seeded database and rerun
`sudo /opt/nnp/deploy/install.sh`, which does not use `--create`.

## Session 1: minimum viable launch

Target about 3 hours. Do 1.6 through 1.8 in one sitting: from 1.6 on the
site is public, and its privacy page says backups exist, which is only true
once 1.8 passes. Do not hand out any link before 1.8.

### 1.1 Accounts (LAPTOP, browser; 15 min)

1. Password manager: create an entry "nnp server". It will hold the restic
   passphrase (create it now, long and random), the B2 bucket name, keyID and
   applicationKey, every healthchecks.io ping URL (they are secrets: anyone
   holding one can fake pings), and after 1.4 `NNP_SECRET_KEY` and
   `NNP_ADMIN_TOKEN`. Rebuilding the server from scratch needs only this
   entry plus the repo.
2. Backblaze: a PRIVATE B2 bucket and an application key scoped to that
   bucket only (`deploy/backup/README.md` step 2).
3. healthchecks.io: a check `nnp-backup`, period 1 day, grace 3 hours
   (README step 3). Email integration to a mailbox you read daily.
4. UptimeRobot: an account (the monitor comes in 1.9).
5. Registrar (nonnewtonian.org) and DreamHost billing: auto-renew ON.

You should see: one password-manager entry with the passphrase, bucket name
and both B2 key values, and the healthchecks ping URL.

### 1.2 DreamCompute instance (browser; 15 min)

- Image: Ubuntu 24.04 LTS. Flavor: `gp1.semisonic` (512MB, 1 vCPU, $4.50
  per month cap; DreamHost's "DreamCompute billing" article as of 2026-07-08,
  checked 2026-10-25). `gp1.subsonic` is the 1GB one ($6.00). Pick by the
  RAM column if the names have changed.
- Key pair: your ssh public key.
- Security group: inbound TCP 22, 80, 443 only (80 is needed for ACME
  validation and the http->https redirect).
- Note the public IPv4 (`SERVER_IP`). Add an AAAA record later only if IPv6
  actually routes to the instance; a broken AAAA breaks ACME for clients that
  prefer IPv6.

You should see: the instance "Active" with an IPv4 address.

### 1.3 DNS (browser + LAPTOP; 10 min)

At the DNS host: `A nonnewtonian.org -> SERVER_IP`, `CNAME www ->
nonnewtonian.org`, TTL 300. Then, BEFORE 1.4 (Caddy requests certificates
the moment it starts, and failed validations count against Let's Encrypt
rate limits):

**LAPTOP**

```
dig +short nonnewtonian.org
dig +short www.nonnewtonian.org
```

You should see: `SERVER_IP` for the first; `nonnewtonian.org.` then
`SERVER_IP` for the second. Wait and repeat until both are right.

### 1.4 First install (20 min)

The Caddy apt-repo commands in `install.sh` were compared with
https://caddyserver.com/docs/install ("Debian, Ubuntu, Raspbian", stable) on
2026-10-25 and match. If apt fails on the Caddy repo in step 2/9, re-compare
them with that page, fix `deploy/install.sh` on the laptop, commit, push,
pull on the server and rerun.

On the server (the default user on DreamCompute's Ubuntu images is `ubuntu`,
per DreamHost's "How to find the default user of an image" article; the
instance's image shows it under Custom Properties as `default_user`):

**LAPTOP**

```
ssh ubuntu@SERVER_IP
```

You should see: an Ubuntu 24.04 banner and a shell prompt.

```
sudo DEBIAN_FRONTEND=noninteractive apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
[ -e /var/run/reboot-required ] && echo REBOOT-NEEDED
```

You should see: apt finishing without errors. If `REBOOT-NEEDED` prints:
`sudo reboot`, wait a minute, ssh back in.

```
sudo git clone https://github.com/LernerLabs/nonnewtonian /opt/nnp
sudo /opt/nnp/deploy/install.sh
```

You should see, step by step (install.sh prints its own "You should see:"
after each `== N/9` header; read them):

- `1/9 swap`: a `/swapfile file 1024M` line and `vm.swappiness = 10`.
- `2/9`: no apt errors, `v2.x` from `caddy version`,
  `Automatic-Reboot "true"`, `Automatic-Reboot-Time "05:30"`,
  `Unattended-Upgrade "1"`, `SystemMaxUse=200M`, `MaxRetentionSec=1month`.
  If the Caddy repo is unreachable, it stops here with an apt or curl
  error.
- `3/9`: uid/gid lines for nnp, and `nnp` among caddy's groups.
- `4/9`: no pip errors and `gunicorn (version 22.x or 23.x)`. This is the
  FIRST place the `deploy` extra (gunicorn) and the `nonnewtonian` console
  script are ever installed; they could not be installed where the code was
  written (no package index access).
- `5/9`: `created /etc/nnp/nnp.env with a new secret key and admin token`,
  then at once
  `SAVE THIS IN YOUR PASSWORD MANAGER NOW: https://nonnewtonian.org/admin/...`
  (43 characters after `/admin/`) and `Also save NNP_SECRET_KEY from: sudo
  cat /etc/nnp/nnp.env` (save the URL now: if a later step aborts this run,
  no rerun prints it), then `-rw------- 1 root root ... /etc/nnp/nnp.env`.
- `6/9`: `WARN: NNP_ADMIN_EMAIL not set ...` (expected on the first run),
  `Valid configuration`, then `active`. This step always restarts Caddy
  (never a reload), on every run.
- `7/9`: `DB missing. Seed on the laptop (RUNBOOK 1.5) and upload to
  /var/lib/nnp/app.db, then rerun this script.`, then the same two SAVE
  lines again as a reminder. The script exits 0 here; steps 8-9 run on the
  next invocation.
- Step 3 or 7 (or `--mirror-key`) can stop with `ERROR: <path> is a
  symlink; the app user may be compromised: investigate before rerunning`
  if a symlink sits at one of the paths install.sh touches under
  `/var/lib/nnp`. On a fresh box that cannot happen; see the
  [Incident quick reference](#incident-quick-reference).

If `caddy validate` rejects the config (it was never run where the Caddyfile
was written), the likeliest culprits are `status {err.status_code}` inside
`handle_errors` and the `fields { }` sub-block of the global `log` block; see
"Caddy rejects the status line" and "Caddy rejects the log block" in the
[Incident quick reference](#incident-quick-reference).

Save the secrets:

```
sudo grep -e '^NNP_SECRET_KEY=' -e '^NNP_ADMIN_TOKEN=' /etc/nnp/nnp.env
```

You should see: two lines, each with a 43-character value. Put both, and
`ADMIN_URL`, into the password-manager entry now. install.sh prints the
admin URL only on the run that creates nnp.env (step 5, repeated at the end
of that run), never on a rerun.

Set the ACME contact (replace the address; the rerun in 1.6 applies it):

```
sudo sed -i 's/^#NNP_ADMIN_EMAIL=$/NNP_ADMIN_EMAIL=you@example.org/' /etc/nnp/nnp.env
sudo grep '^NNP_ADMIN_EMAIL=' /etc/nnp/nnp.env
```

You should see: `NNP_ADMIN_EMAIL=` followed by your address.

Verify what could not be verified where the code was written:

```
ls -l /opt/nnp/.venv/bin/nonnewtonian /opt/nnp/.venv/bin/gunicorn
sudo /opt/nnp/deploy/nnp-cli --help | head -1
sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
sudo systemd-analyze verify /etc/systemd/system/nnp.service /etc/systemd/system/nnp-backup.service /etc/systemd/system/nnp-backup.timer /etc/systemd/system/nnp-mirror.service /etc/systemd/system/nnp-selfcheck.service
```

You should see: both files listed; `usage: nonnewtonian [-h]
{seed-import,migrate,export-static} ...`; `Valid configuration`; and from
`systemd-analyze verify` no output, or only lines you can explain (for
example about `/etc/nnp/mirror.env`, which does not exist until Session 3).
Any `Unknown key`, `Invalid` or `Failed to parse` line is a real error: stop
and fix it in the repo.

No proxy variable reaches the app (the fetcher ignores them anyway; see
[Server layout and configuration](#server-layout-and-configuration)):

```
sudo grep -ic proxy /etc/nnp/nnp.env
systemctl show nnp -p Environment
systemctl show-environment | grep -ic proxy
```

You should see: `0`, `Environment=` with nothing after it, and `0`. Any
proxy variable: remove it (from nnp.env, a drop-in under
`/etc/systemd/system/nnp.service.d/`, or `DefaultEnvironment=` in
`/etc/systemd/system.conf`) before 1.6.

**LAPTOP**

```
curl -s -o /dev/null -w '%{http_code}\n' https://nonnewtonian.org/healthz
```

You should see: `502` (Caddy has a certificate and serves `down.html`
because the app is not started yet). `000` means the TLS handshake failed,
i.e. ACME did not get a certificate: `sudo journalctl -u caddy -n 50
--no-pager` on the server. (Until Session 3 installs the static export,
`/` itself answers `404` during an outage: install.sh puts a copy of
`down.html` at `index.html` so the front page shows the notice, but
`/scientists` and friends have no archived copy yet.)

Caddy's error log must not record URLs (manage and admin tokens are URL
paths) or client IPs. While the app is still down, every request ends in a
proxy error, the kind Caddy logs, so this is the moment to check.
**LAPTOP**:

```
curl -s -o /dev/null -w '%{http_code}\n' https://nonnewtonian.org/manage/redaction-check
```

You should see: `502`. Then on the server (`$SSH_CLIENT` begins with your
laptop's public IP, the same address the curl came from):

```
echo "${SSH_CLIENT%% *}"
sudo journalctl -u caddy --since '-10 min' --no-pager | grep -c -F -e redaction-check -e "${SSH_CLIENT%% *}"
sudo journalctl -u caddy --since '-10 min' --no-pager | grep -c REDACTED
```

You should see: your laptop's public IP (if this line is empty, stop: the
next grep would match every line), then `0` (neither the path nor your IP
in Caddy's log), then a count of 1 or
more (the error was logged, with `REDACTED` in place of the URI and IPs). A
non-zero first count means the redaction is not working: stop and fix the
Caddyfile's global `log` block before any real link exists. A `0` second
count means Caddy did not log the 502 at all (not verified which Caddy
does); then this check proves nothing either way, so note it in the drill
log.

### 1.5 Seed the database (LAPTOP; 15 min)

Seeding runs on the laptop because the original decks, the only surviving
copies of photos whose URLs have rotted, are there. The CLI silently ignores
a `--decks-dir` that does not exist, so check it first.

**LAPTOP**

```
ls ~/coding/courses/IntroductoryPhysics/Textbooks | head -3
rm -rf ~/nnp-seed && mkdir ~/nnp-seed
~/coding/nonnewtonian/.venv/bin/python -m nonnewtonian.cli seed-import --db ~/nnp-seed/app.db --photo-dir ~/nnp-seed/photos --decks-dir ~/coding/courses/IntroductoryPhysics/Textbooks
```

You should see: three `.pptx` names, then a seed report with `Files: 39`,
`Entries created: 41`, `Placements: 65 (61 matched, 4 unassigned)`,
`Photos: 41 (S stored, R recovered from pptx, D dead)`,
`Textbooks loaded: knight-calc-3rd, knight-college-2nd, mandi-4th`,
`Wanted scientists: 6`, per-file lines, and `Wrote .../nnp-seed/app.db`.
(Counts from a `--no-photos` run on 2026-10-20; S, R and D depend on which
photo URLs are alive today.) R = 0 with a large D means the decks were not
used. Read the per-file `flags=` notes (for example C.V. Raman's "born
1988"): they are corpus fixes to make later, not blockers. Photo intake now
applies here too: each URL fetch is cut off after 20 s, stored photos are
shrunk to 2000 px on the long edge, and a non-JPEG image over 12 MP is
rejected, so a few large PNG/GIF/WebP originals may count as D (dead), and
a recovered deck image of that kind is skipped without being counted.

```
sqlite3 ~/nnp-seed/app.db ".backup '$HOME/nnp-seed/upload.db'"
sqlite3 ~/nnp-seed/upload.db 'PRAGMA integrity_check; SELECT count(*) FROM entries;'
scp ~/nnp-seed/upload.db ubuntu@SERVER_IP:
scp -r ~/nnp-seed/photos ubuntu@SERVER_IP:
```

You should see: `ok` and `41`, then two scp transfers with no errors.

### 1.6 Install the database and start the app (10 min)

```
sudo install -o nnp -g nnp -m 600 ~/upload.db /var/lib/nnp/app.db
sudo rm -rf /var/lib/nnp/photos
sudo mv ~/photos /var/lib/nnp/photos
sudo chown -R nnp:nnp /var/lib/nnp/photos
sudo chmod -R u=rwX,g=rX,o= /var/lib/nnp/photos
rm ~/upload.db
sudo ls -l /var/lib/nnp
```

You should see: `-rw------- ... nnp nnp ... app.db`, `drwxr-x--- ... nnp
nnp ... photos`, and `export`.

```
sudo /opt/nnp/deploy/install.sh
```

You should see: steps 1-6 as before but `kept existing /etc/nnp/nnp.env` and
no `NNP_ADMIN_EMAIL` warning; step 7 printing `up to date` (from the check)
and `up to date` (from the migrate); step 8 `local healthz: ok` and
`public https: ok`; step 9 nothing more (no admin URL on a rerun).

If step 8 prints `WARN: public https not up yet` but the laptop check below
works, the instance cannot reach its own public IP (hairpin NAT is not
guaranteed on DreamCompute; not verified). That is harmless; the self-check
uses `--resolve` to 127.0.0.1 for this reason.

**LAPTOP**

```
curl -fsS https://nonnewtonian.org/healthz; echo
```

You should see: `ok`.

### 1.7 Go-live smoke test (20 min)

1. Laptop browser: open `ADMIN_URL`, press "Approve all seeded entries".
   You should see: `0 entries awaiting review | 41 approved and live.`
2. Phone, Wi-Fi OFF (cellular tests the real outside path): open
   `https://nonnewtonian.org/new` and create your real class (give your
   email; it is optional but recovery depends on it, and if given it must
   look like an address, or the form answers `That email address doesn't
   look right. Leave it blank or fix it.`). You should see: the welcome page
   with the manage link and kit. Save both in the password manager now.
3. Phone: open `/c/<slug>/submit`; submit scientist A with an UPLOADED photo
   taken just now with the phone camera (a real full-size phone photo, not a
   screenshot: step 6 measures memory on it), then scientist B (a different
   name) with a photo URL. You should see: the thanks page each time, with
   no `Your photo didn't upload (...)` line. That line carries the reason,
   for example `Could not download that link; check that it opens in a
   browser.`, `That site was too slow to send the image.` (20 s total), `That
   image is too large for this site ... please use a JPEG ...` (non-JPEG over
   12 MP), or `the server was busy with other photos just now` (another
   photo was being processed for more than 2 s); the entry is saved without
   the photo either way.
4. Manage page: approve both. Download one entry's slide and the class deck
   (`/c/<slug>/deck.pptx`); open both. You should see: slides with the photos
   (stored photos are shrunk to 2000 px on the long edge).
5. **LAPTOP** deck timing:

   ```
   curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' https://nonnewtonian.org/textbooks/knight-calc-3rd/deck.pptx
   ```

   You should see: `200` and a time. Record it in the [Drill log](#drill-log).
   A `503` means another deck build or download held the single deck slot
   for more than 2 s (the slot is held until the download finishes); a
   `429` means more than 6 deck downloads from your IP in a minute (wait a
   minute); a `413` means the deck's stored photos exceed 100 MB. See
   [Known limitations](#known-limitations).
6. Server, memory (the 512MB decision). `MemoryPeak` is the peak since nnp
   last started (1.6), so it covers step 3's phone-photo upload, step 4's
   CLASS deck build with that photo, and step 5's textbook deck; do not
   restart nnp between 1.6 and here:

   ```
   systemctl show nnp -p MemoryPeak -p MemoryCurrent
   free -m
   ```

   You should see: `MemoryPeak` well under `MemoryHigh=330M` (346030080
   bytes). If it prints `[not set]`, rerun the class deck download on the
   phone and read `MemoryCurrent` during it instead (not verified that Ubuntu
   24.04's systemd reports `MemoryPeak` for this unit). Near 330M: resize the
   instance before the class uses it. Record it in the drill log. One phone
   photo understates a full class deck: rerun this check once a real class
   has approved entries with phone photos.
7. Photo served by Caddy: on a scientist page, open the photo in its own tab.
   You should see: a URL `/photos/<2 hex>/<64 hex>.jpg` (or `.png`) and the
   image.
8. Delete the test entries: their ids are in the slide URLs
   (`/c/<slug>/entry/<id>/slide.pptx`); `ADMIN_URL` -> "Take down any entry"
   -> each id -> "Look up...". Nothing is deleted yet: a confirm page shows
   the entry's scientist, class, contributor, status and photo count. Check
   it is your test entry, then press "Take down entry N". You should see:
   `Entry N taken down.`

### 1.8 Backups (20 min)

Do `deploy/backup/README.md` steps 4, 5 and 6 (the bucket, key and
healthchecks check are done in 1.1). Step 6 is:

```
sudo /opt/nnp/deploy/install.sh --enable-backups
```

You should see: `snapshot ... saved`, `nnp backup complete: ...`,
`restore OK: integrity ok, N entries in the restored database`,
`nnp-backup.timer` in the `list-timers` output, and the healthchecks.io
`nnp-backup` check green, and no `unable to open cache` line in the
journal output. Only now is the privacy page's "encrypted backups kept
off-site" line true. (The restore test fails with `restored db has no
entries` on an empty database, so 1.6 must come first.)

### 1.9 Uptime prober (5 min)

UptimeRobot: new monitor, type HTTP(s) (a plain status monitor, NOT keyword),
URL `https://nonnewtonian.org/healthz`, interval 5 minutes, alert to your
email. It counts any non-2xx as down: `/healthz` returns 503 on low disk or
an unwritable photo dir, and while the app is down Caddy serves `down.html`
WITH the 502. A keyword monitor on "ok" would false-match, because
`down.html` contains the string "OK" (inside "RUNBOOK" in a comment).

You should see: the monitor "Up" within 5 minutes.

### 1.10 Drill D1a (10 min)

Run [D1a](#d1-crash-and-restart) and log it.

### 1.11 Hand out the class link (5 min)

Give the class `https://nonnewtonian.org/c/<slug>/submit`. Keep the manage
link and kit to yourself.

## Updating

**LAPTOP**: commit and push to `main`. Then on the server:

```
sudo git -C /opt/nnp pull --ff-only && sudo /opt/nnp/deploy/install.sh
```

You should see: the same nine steps. Step 7 prints `up to date` twice (the
check, then the migrate), or
`pending: <names>`, `pending migrations: taking a pre-migration backup
snapshot first`, the backup's own output (in the terminal, NOT in
`journalctl -u nnp-backup.service`), and then `applied: <names>`; step 8
`local healthz: ok` and `public https: ok`. Every run restarts Caddy in
step 6 (a moment in which connections are refused outright, not even
`down.html`), and step 8's app restart is about 2-3 s of 502, served as
`down.html` (or the archived page once the mirror exists).

- Never edit files under `/opt/nnp` on the server; `pull --ff-only` then
  refuses. Every change goes through the repo.
- The pre-migration snapshot is taken only if `nnp-backup.timer` is enabled
  (after 1.8). Before that, install.sh prints `WARN: pending migrations and
  backups are not enabled yet; no pre-migration snapshot` and migrates
  without one. If the snapshot itself fails, it stops with `ERROR:
  pre-migration backup failed; not migrating (output above)`.
- Pre-migration snapshots are tagged `nnp,pre-migration` and are NEVER
  pruned automatically (the nightly `restic forget` keeps them with
  `--keep-tag pre-migration`), so each one outlives the six-month window the
  privacy page promises until you prune it by hand. Once a deploy has proved
  good, follow `deploy/backup/README.md` "Pre-migration snapshots (prune by
  hand)" (`restic snapshots --tag pre-migration`, then `restic forget
  --prune <ID>`).
- If `migrate` itself fails, install.sh prints `migrate FAILED. Each
  migration runs in one transaction, ...` (plus `A pre-migration snapshot
  exists: restic snapshots --tag pre-migration ...` when it took one) and
  exits 1 before step 8: the old app keeps running, and any migration that
  completed before the failing one stays applied.
- On a failed healthz, install.sh prints the last 40 log lines and the
  rollback line `sudo git -C /opt/nnp checkout <sha> && sudo
  /opt/nnp/deploy/install.sh`, where `<sha>` is `/etc/nnp/last-good-commit`
  (written after every successful step 8; absent before the first one, in
  which case it says to fix forward). That reruns pip, the units, migrate
  and the health check on the old commit. Two caveats:
  - If that run applied a migration, the old code may not match the new
    schema. With a snapshot, install.sh says to restore it (`restic
    snapshots --tag pre-migration`; see
    [Restore and rebuild](#restore-and-rebuild), in place, with that
    snapshot's id). Without one (backups not enabled yet) it says `This run
    applied a migration with NO pre-migration snapshot ... there is nothing
    to restore`: fix forward.
  - The checkout leaves `/opt/nnp` on a detached HEAD. Once the fix is
    pushed, update with `sudo git -C /opt/nnp checkout main && sudo git -C
    /opt/nnp pull --ff-only && sudo /opt/nnp/deploy/install.sh`.

## Session 2: monitoring and drills

### 2.1 Unattended upgrades: confirm Caddy is covered (5 min)

install.sh already installed `/etc/apt/apt.conf.d/52nnp-unattended-upgrades`
and the journald cap in Session 1. What is NOT verified is whether the
`site=dl.cloudsmith.io` origin pattern actually matches Caddy's repo.

```
apt-cache policy caddy | head -8
sudo unattended-upgrade --dry-run --debug 2>&1 | grep -i -e 'allowed origins' -e caddy
```

You should see: a `dl.cloudsmith.io` line in the policy, and
`site=dl.cloudsmith.io` in the "Allowed origins" line. If not, replace the
pattern in `deploy/system/52nnp-unattended-upgrades` with the `o=...` value
`apt-cache policy` shows for cloudsmith (LAPTOP, commit, push), then
[update](#updating).

### 2.2 Daily self-check (10 min)

Requires 1.8 (it checks that `nnp-backup.timer` is active). healthchecks.io:
add a check `nnp-selfcheck`, period 1 day, grace 3 hours; copy its URL.

```
sudo cp /opt/nnp/deploy/monitor/monitor.env.example /etc/nnp/monitor.env
sudo chmod 600 /etc/nnp/monitor.env
sudo nano /etc/nnp/monitor.env
sudo /opt/nnp/deploy/install.sh --enable-selfcheck
```

You should see: `selfcheck ok` in the log lines, `nnp-selfcheck.timer` listed
with its next 06:15 run, and the `nnp-selfcheck` check green. It checks: nnp,
caddy and nnp-backup.timer (and nnp-mirror.timer once enabled) active;
`/healthz` via 127.0.0.1; certificate valid 14+ more days; data disk under
90%; swap on; no symlinks under `/var/lib/nnp/photos` or
`/var/lib/nnp/export`; no orphan photo temp files (`.*.tmp`) older than a
day; no failed units. Any failure POSTs the list to healthchecks.io and
exits 1. A failed unit stays "failed" (and keeps failing the self-check)
until it next succeeds or you run `sudo systemctl reset-failed <unit>`.

### 2.3 Drills

Run [D1b, D1c](#d1-crash-and-restart), [D2](#d2-restore-on-the-laptop) and
[D3](#d3-dead-man-lapse); log each.

### 2.4 B2 lifecycle and the second domain (15 min)

- Backblaze: bucket -> Lifecycle Settings -> keep prior versions for 30 days.
  This makes deletions by a stolen server key recoverable for 30 days. It is
  a mitigation, not a guarantee: the server's key can delete, and whether a
  key with delete rights can also purge prior versions depends on its
  capabilities (not verified here).
- `nonnewtonianphysicists.com`: point A records for it and `www` at
  `SERVER_IP`; LAPTOP `dig +short nonnewtonianphysicists.com` until it shows
  `SERVER_IP`. Then LAPTOP: uncomment the last block of
  `deploy/caddy/Caddyfile` (do NOT do this before DNS resolves: Caddy would
  retry ACME and burn Let's Encrypt failed-validation limits), commit, push,
  [update](#updating). **LAPTOP** check:

  ```
  curl -sI https://nonnewtonianphysicists.com/ | grep -i '^location'
  ```

  You should see: `location: https://nonnewtonian.org/`.

## Session 3: the static mirror

The nightly `nnp-mirror.service` runs `nonnewtonian export-static` (the
public communal site rendered to files in `/var/lib/nnp/export/current`,
which is also what Caddy serves while the app is down) and then
`nnp-mirror.sh`, which pushes it as one orphan commit to `gh-pages` of a
DEDICATED public repo served by GitHub Pages: a read-only escape hatch if the
server dies. Class pages (`/c/*`) are EXCLUDED (decided 2026-10-25: class
work was not consented to a public git repo, and a public repo enumerates
every class slug, hence every submit link); only communal content is
exported. Entries a teacher shares with the communal collection reach the
mirror that way. A later "close class" + "archive class publicly" opt-in on
the manage page is the planned middle ground, after launch.

### 3.1 Repo and deploy key (15 min)

1. GitHub (browser): create a PUBLIC, EMPTY repo `mglerner/nonnewtonian-mirror`
   (no README, no license: no branch at all).
2. Server:

   ```
   sudo /opt/nnp/deploy/install.sh --mirror-key
   ```

   You should see: an `ssh-ed25519 AAAA... nnp-mirror@nonnewtonian.org` line,
   then a `256 SHA256:... github.com (ED25519)` fingerprint. Compare that
   SHA256 with the ED25519 line at
   https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints.
   If it differs, STOP: `sudo rm /var/lib/nnp/.ssh/known_hosts` and rerun later
   from a network you trust.
3. GitHub: `nonnewtonian-mirror` -> Settings -> Deploy keys -> Add, paste the
   `ssh-ed25519` line, tick "Allow write access". Add it to this repo only.

### 3.2 Configure and enable (10 min)

Optional: a healthchecks.io check `nnp-mirror` (period 1 day, grace 3 hours)
for `NNP_MIRROR_PING_URL`.

```
sudo cp /opt/nnp/deploy/mirror/mirror.env.example /etc/nnp/mirror.env
sudo chmod 600 /etc/nnp/mirror.env
sudo nano /etc/nnp/mirror.env
sudo /opt/nnp/deploy/install.sh --enable-mirror
```

You should see: `exported N pages, M slides, P photos, E entries to
/var/lib/nnp/export/current` (with only the seeds approved: 50 pages, 39
slides, 41 entries, measured on a laptop seed; more once shared class entries
are approved), then `pushed <sha> (tree <sha>)`, and
`nnp-mirror.timer` listed. The GitHub side (key, ssh, push) was never
exercised where the code was written; this is its first test.

### 3.3 GitHub Pages and down.html (10 min)

1. GitHub: `nonnewtonian-mirror` -> Settings -> Pages -> Source "Deploy from
   a branch", branch `gh-pages`, folder `/ (root)`. Wait a few minutes.

   **LAPTOP**

   ```
   curl -s -o /dev/null -w '%{http_code}\n' https://mglerner.github.io/nonnewtonian-mirror/
   ```

   You should see: `200`.
2. **LAPTOP**: in `deploy/caddy/down.html`, uncomment the mirror line (the
   HTML comment `<!-- <p>Meanwhile, a read-only copy ... -->`) in the REPO
   copy. Do not edit `/var/lib/nnp/export/current/down.html` on the server:
   every export re-copies `down.html` from the repo and overwrites it. Commit,
   push, [update](#updating), then:

   ```
   sudo systemctl start nnp-mirror.service
   journalctl -u nnp-mirror.service -n 3 --no-pager
   sudo systemctl start nnp-mirror.service
   journalctl -u nnp-mirror.service -n 3 --no-pager
   ```

   You should see: `pushed <sha> (tree <sha>)` after the first start (down.html
   changed), and `mirror unchanged (tree <sha>)` after the second.
3. Outage fallback for directory URLs (a few seconds of real downtime; the
   Caddy part is UNVERIFIED until this runs). The export's links are
   relative, so while the app is down Caddy redirects a directory URL with
   no trailing slash to the slash form before serving it from the export:

   ```
   sudo systemctl stop nnp
   curl -sI --resolve nonnewtonian.org:443:127.0.0.1 https://nonnewtonian.org/scientists | grep -i -e '^HTTP' -e '^location'
   curl -s -o /dev/null -w '%{http_code}\n' --resolve nonnewtonian.org:443:127.0.0.1 https://nonnewtonian.org/scientists/
   curl -s --resolve nonnewtonian.org:443:127.0.0.1 https://nonnewtonian.org/scientists/ | grep -c 'read-only copy'
   sudo systemctl start nnp
   sleep 5; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/scientists/
   ```

   You should see: `HTTP/2 302` and `location: /scientists/`; `502`; `1`
   (the archived page, not `down.html`); and `200` once the app is back
   (the app serves `/scientists` and `/scientists/` alike, so a redirected
   URL keeps working). A `502` with no `location` for the first curl means
   the redirect is not in effect: the archived pages then load without
   their stylesheet and with broken relative links.

### 3.4 Drills

Run [D4](#d4-takedown) and [D5](#d5-file-check); log both.

What makes a nightly run fail (and therefore leave BOTH the mirror and
Caddy's outage fallback at the previous night's content):

- `export-static: ...` errors: any public page not returning 200; zero
  approved entries; a referenced photo missing; or the leak guard. The leak
  guard refuses the export if any exported text (also after decoding HTML
  entities) contains the admin token, the secret key, a CSRF field, a link
  to `/manage/`, `/admin/`, `/recover/` or `/c/`, or any class slug (all
  matched anywhere, case-insensitive); or, matched as whole words only, a
  stored teacher email (`... contains the teacher email of collection N`;
  only stored values containing `@` are checked) or the `contributor_name`
  (4+ characters) of an entry whose attribution is not "full" and whose name
  is not already shown publicly. Whole-word matching means "Mark" no longer
  hits "marked", but the guard still fails closed on an innocent collision:
  an anonymous one-word name equal to a whole word in public text, for
  example a hidden "Grace" while "Grace Hopper" is a public scientist (or
  "Chen" beside "Chen-Ning Yang": a hyphen counts as a word boundary). See
  [Incident quick reference](#incident-quick-reference).
- `refusing: export shrank from X to Y files (pass --allow-shrink)`: the
  export has under half the files on `gh-pages` (protects the mirror from a
  wrong or empty database). If the shrink is real (a mass takedown), push once
  by hand:

  ```
  sudo bash -c 'set -a; . /etc/nnp/nnp.env; . /etc/nnp/mirror.env; set +a; exec runuser -u nnp -- /opt/nnp/deploy/mirror/nnp-mirror.sh /var/lib/nnp/export/current --allow-shrink'
  ```

  You should see: `pushed <sha> (tree <sha>)`.
- `ls-remote failed (rc=N): network or auth, not a first push`: GitHub
  unreachable or the deploy key was removed.
- `refusing: non-regular file in export`: something planted a symlink in the
  export dir. Treat as a compromise of user nnp.

## Session 4 (optional): email recovery

Without SMTP, `/recover` shows instructions to email you, and you re-issue
links by hand ([Lost manage link](#lost-manage-link)). With it, a teacher
gets a signed confirm link by email. (Deviation from the plan: the email
carries a confirm link, not the manage token; the old link dies only when the
inbox holder presses the button, so knowing a teacher's address cannot lock
them out, and mail scanners that prefetch links change nothing.)

1. Provider: a mailbox on nonnewtonian.org (DreamHost) or a transactional
   provider. You need host, user, password and a From address. Port 587 with
   STARTTLS is required (the code does not do implicit TLS on 465). Whether
   DreamCompute allows outbound 587 was not verified.
2. DNS: add the SPF and DKIM records your provider documents. **LAPTOP**:

   ```
   dig +short TXT nonnewtonian.org
   ```

   You should see: a `"v=spf1 ..."` record naming the provider (and, for DKIM,
   `dig +short TXT <selector>._domainkey.nonnewtonian.org` shows the key).
3. Server: uncomment and fill `NNP_SMTP_HOST`, `NNP_SMTP_USER`,
   `NNP_SMTP_PASSWORD`, `NNP_MAIL_FROM` (and `NNP_SMTP_PORT` only if not 587):

   ```
   sudo nano /etc/nnp/nnp.env
   sudo systemctl restart nnp
   sleep 5; curl -fsS http://127.0.0.1:8000/healthz; echo
   ```

   You should see: `ok`. If not, `journalctl -u nnp -n 20 --no-pager` shows
   `SMTP is half-configured; ...` when one of the four is missing, or
   `NNP_SMTP_PORT must be an integer (got ...)`.
4. End to end, with a test class created with your own email:
   - `/recover`, enter that email. You should see: "If that address matches a
     class here, a link is on its way ...". Also try an unknown address: the
     same page, and no mail.
   - The mail arrives (check spam). In Gmail "Show original": SPF PASS, DKIM
     PASS. A send failure shows in `journalctl -u nnp` only as `recover mail
     failed: <ExceptionName>` (no address, no link).
   - Open the confirm link: a page with one button. The OLD manage link still
     works at this point.
   - Press the button: the welcome page with a NEW link. The old link now
     404s. Reopen the confirm link: "This link was already used or has
     expired."
   - Delete the test class from its manage page.

Limits: 5 requests per hour per IP (every POST counts); 3 per day per
address, counted only for requests that get past the spam traps; 100 per day
in total, counted only for requests that actually send mail. All are
in-memory and reset when nnp restarts. A user who hits one sees a "Too many
requests" page pointing at the mglerner@smith.edu fallback.

## Drills

Each drill is numbered steps with the exact expected result. Server public
checks use `--resolve nonnewtonian.org:443:127.0.0.1` so they do not depend
on hairpin NAT. Log every run in the [Drill log](#drill-log).

### D1 Crash and restart

**D1a: kill -9 and Caddy without the app** (Session 1; 10 min)

1. ```
   systemctl show -p NRestarts --value nnp
   sudo kill -9 "$(systemctl show -p MainPID --value nnp)"
   sleep 8
   systemctl show -p NRestarts --value nnp
   curl -fsS --resolve nonnewtonian.org:443:127.0.0.1 https://nonnewtonian.org/healthz; echo
   ```

   You should see: the second NRestarts is the first plus 1, then `ok`.
2. Kill only the worker (the master respawns it):

   ```
   sudo kill -9 "$(pgrep -P "$(systemctl show -p MainPID --value nnp)")"
   sleep 5
   journalctl -u nnp -n 5 --no-pager
   systemctl show -p NRestarts --value nnp
   ```

   You should see: a `Booting worker with pid` line, and NRestarts unchanged
   from step 1.
3. Stop the app; Caddy still serves photos and static files:

   ```
   P=$(sudo find /var/lib/nnp/photos -type f \( -name '*.jpg' -o -name '*.png' \) | head -1 | sed 's#^/var/lib/nnp##'); echo "$P"
   sudo systemctl stop nnp
   for u in "$P" /static/style.css /healthz; do curl -s -o /dev/null -w "%{http_code} $u\n" --resolve nonnewtonian.org:443:127.0.0.1 "https://nonnewtonian.org$u"; done
   curl -s --resolve nonnewtonian.org:443:127.0.0.1 https://nonnewtonian.org/healthz | grep -c 'Back soon'
   sudo systemctl start nnp
   sleep 5; curl -fsS http://127.0.0.1:8000/healthz; echo
   ```

   You should see: a `/photos/xx/<hex>.jpg` path; `200 /photos/...`,
   `200 /static/style.css`, `502 /healthz`; `1` (the down.html body); `ok`.
   A `200 /healthz` while stopped means the `status` line was dropped from
   the Caddyfile (see the incident table). After Session 3,
   `/scientists/<slug>` redirects (302) to `/scientists/<slug>/`, which,
   like its `slide.pptx`, comes back during the stop as the archived copy
   with status 502 (3.3 step 3).

**D1b: the prober notices** (Session 2; 15 min; real downtime, pick a quiet
hour). A kill -9 restart takes about 3 s, invisible to a 5-minute prober,
hence this separate drill.

1. ```
   sudo systemctl stop nnp; date
   ```

   You should see: the time. Wait 12 minutes.
2. You should see: an UptimeRobot DOWN email for `/healthz` (502).
3. ```
   sudo systemctl start nnp; sleep 5; curl -fsS http://127.0.0.1:8000/healthz; echo
   ```

   You should see: `ok`, and an UptimeRobot UP email within about 5 minutes.

**D1c: reboot** (Session 2; 10 min)

1. ```
   sudo reboot
   ```

2. **LAPTOP**, after 2 minutes:

   ```
   curl -fsS https://nonnewtonian.org/healthz; echo
   ```

   You should see: `ok`.
3. ssh back in:

   ```
   systemctl is-active nnp caddy
   systemctl list-timers --no-pager | grep nnp
   swapon --show
   systemctl --failed --no-legend
   ```

   You should see: `active` twice; `nnp-backup.timer` and
   `nnp-selfcheck.timer` (and `nnp-mirror.timer` after Session 3), each with
   a NEXT time; the `/swapfile` line; nothing under `--failed`.

### D2 Restore on the laptop

(Session 2; 30 min.) The point is proving that the PASSWORD-MANAGER copy of
the credentials works, so take every value from there, not from the server.
The drill dirs live in `~/nnp-drills`, outside the repo: the restored
database is plaintext and holds teacher emails.

1. **LAPTOP** (zsh; `read -s 'VAR?prompt'` is zsh syntax and keeps the
   secrets out of shell history):

   ```
   brew install restic
   export RESTIC_REPOSITORY='b2:BUCKET:nnp'
   export B2_ACCOUNT_ID='KEY-ID-FROM-PASSWORD-MANAGER'
   read -s 'B2_ACCOUNT_KEY?B2 applicationKey: '; export B2_ACCOUNT_KEY; echo
   read -s 'RESTIC_PASSWORD?restic passphrase: '; export RESTIC_PASSWORD; echo
   restic snapshots
   ```

   You should see: a table of snapshots tagged `nnp` (some also
   `pre-migration`), host `nnp` (snapshots from before the M7 review fixes
   show the server's real hostname), the newest from the last 03:30-03:45
   run (or a manual one).
2. **LAPTOP**:

   ```
   rm -rf ~/nnp-drills/restore
   restic --no-lock restore latest --target ~/nnp-drills/restore
   sqlite3 ~/nnp-drills/restore/var/lib/nnp-backup/staging/app.db 'PRAGMA integrity_check; SELECT count(*) FROM entries;'
   ```

   You should see: restic finishing without errors, then `ok` and a count.
3. Server, for comparison:

   ```
   sudo -u nnp sqlite3 /var/lib/nnp/app.db 'SELECT count(*) FROM entries;'
   ```

   You should see: the same count, unless entries were added or removed after
   the snapshot's time (shown by `restic snapshots`).
4. **LAPTOP**, browse the restored copy:

   ```
   NNP_DB=$HOME/nnp-drills/restore/var/lib/nnp-backup/staging/app.db NNP_PHOTOS=$HOME/nnp-drills/restore/var/lib/nnp/photos ~/coding/nonnewtonian/.venv/bin/flask --app nonnewtonian.web:create_app run --port 5050
   ```

   Open `http://127.0.0.1:5050/scientists` and a scientist page that has a
   photo. You should see: the page with its photo. Ctrl-C.
5. **LAPTOP**, clean up:

   ```
   rm -rf ~/nnp-drills/restore
   unset RESTIC_PASSWORD B2_ACCOUNT_KEY
   ```

   You should see: no output.

### D3 Dead-man lapse

(Session 2, after 2.2; 25 min.)

**D3a: fail path**

1. ```
   sudo bash -c 'set -a; . /etc/nnp-backup.env; set +a; RESTIC_REPOSITORY=b2:nnp-no-such-bucket-drill:x /opt/nnp/deploy/backup/nnp-backup.sh'; echo "exit=$?"
   ```

   You should see: a restic error about the bucket, then `exit=1` (any
   non-zero); within a few minutes the `nnp-backup` check turns red and
   emails you (a `/fail` ping alerts at once, without waiting for the grace
   period).
2. ```
   sudo systemctl start nnp-backup.service; journalctl -u nnp-backup.service -n 3 --no-pager
   ```

   You should see: `nnp backup complete: ...` and the check green again.

**D3b: silence path**

1. healthchecks.io: set `nnp-backup` to period 10 minutes, grace 5 minutes.
   Then immediately:

   ```
   sudo systemctl start nnp-backup.service
   sudo systemctl stop nnp-backup.timer
   date
   ```

   You should see: the time (the fresh ping starts the 15-minute clock).
2. Meanwhile, confirm the self-check sees the stopped timer:

   ```
   sudo systemctl start nnp-selfcheck.service; journalctl -u nnp-selfcheck.service -n 3 --no-pager
   ```

   You should see: `selfcheck FAILED:` with `nnp-backup.timer is not active`,
   the start command itself reporting failure, and the `nnp-selfcheck` check
   red with that text in its body.
3. About 15 minutes after step 1: You should see: a "nnp-backup is DOWN" email.
4. Put everything back: healthchecks.io `nnp-backup` to period 1 day, grace 3
   hours; then

   ```
   sudo systemctl start nnp-backup.timer
   sudo systemctl start nnp-backup.service
   sudo systemctl start nnp-selfcheck.service
   systemctl list-timers nnp-backup.timer --no-pager
   ```

   You should see: no errors, the timer listed, and both checks green.

### D4 Takedown

(Session 3; 35 min; needs the mirror.)

1. Browser: `/new`, class "Takedown drill", with "Offer students the option
   to share their work with the communal site" ticked. Save its manage link.
2. Phone or browser: `/c/<slug>/submit`, scientist "Takedown Testperson",
   share ticked, contributor name blank, and a photo you just took (so its
   content hash is unique to this entry).
3. Manage page: approve it. `ADMIN_URL` -> "Shared from classes" -> "Approve
   to communal". You should see: Takedown Testperson under "Published on the
   communal site".
4. Server:

   ```
   sudo systemctl start nnp-mirror.service; journalctl -u nnp-mirror.service -n 3 --no-pager
   ```

   You should see: `exported ...` and `pushed <sha> (tree <sha>)`.
5. **LAPTOP** (wait a few minutes for Pages):

   ```
   curl -s -o /dev/null -w '%{http_code}\n' https://mglerner.github.io/nonnewtonian-mirror/scientists/takedown-testperson/
   rm -rf ~/nnp-drills/mirror-before
   git clone -q -b gh-pages --single-branch https://github.com/mglerner/nonnewtonian-mirror ~/nnp-drills/mirror-before
   OLD=$(git -C ~/nnp-drills/mirror-before rev-parse HEAD); echo "$OLD"
   PHOTO=$(grep -o 'photos/[0-9a-f]\{2\}/[0-9a-f]\{64\}\.[a-z]*' ~/nnp-drills/mirror-before/scientists/takedown-testperson/index.html | head -1); echo "$PHOTO"
   ```

   You should see: `200`, a 40-hex SHA, and `photos/<2 hex>/<64 hex>.jpg`.
   Copy both into the drill log notes; keep this terminal open (later steps
   use `$OLD` and `$PHOTO`).
6. Take it down: `ADMIN_URL` -> "Published on the communal site" -> "Take
   down" on Takedown Testperson (the communal copy is a SEPARATE entry from
   the class entry). Then the drill class's manage page -> delete the class
   (type its slug to confirm). The photo file is unlinked only once no entry
   references it, so both steps are needed.
7. **LAPTOP**:

   ```
   for u in /scientists/takedown-testperson "/$PHOTO"; do curl -s -o /dev/null -w "%{http_code} $u\n" "https://nonnewtonian.org$u"; done
   ```

   You should see: `404` for both. (Browsers that already loaded the photo may
   keep it up to 1 day: Caddy sends `max-age=86400`.)
8. Server:

   ```
   sudo systemctl start nnp-mirror.service; journalctl -u nnp-mirror.service -n 3 --no-pager
   ```

   You should see: `pushed <sha> (tree <sha>)` with a new sha.
9. **LAPTOP**, a fresh clone:

   ```
   rm -rf ~/nnp-drills/mirror
   git clone -q -b gh-pages --single-branch https://github.com/mglerner/nonnewtonian-mirror ~/nnp-drills/mirror
   git -C ~/nnp-drills/mirror rev-list --count HEAD
   git -C ~/nnp-drills/mirror grep -i -l testperson $(git -C ~/nnp-drills/mirror rev-list --all); echo "grep exit=$?"
   ls ~/nnp-drills/mirror/$PHOTO
   ```

   You should see: `1`; no file names and `grep exit=1`; `No such file or
   directory`.
10. **LAPTOP**, about 10 minutes later:

    ```
    curl -s -o /dev/null -w '%{http_code}\n' https://mglerner.github.io/nonnewtonian-mirror/scientists/takedown-testperson/
    curl -s -o /dev/null -w '%{http_code}\n' https://github.com/mglerner/nonnewtonian-mirror/commit/$OLD
    rm -rf ~/nnp-drills/mirror-before
    ```

    You should see: `404` for Pages. RECORD the second code: `200` means
    GitHub still serves the orphaned old commit by its SHA, so a real legal
    takedown needs the delete-and-recreate in [Takedown](#takedown); `404`
    means it is gone. Either way, restic still holds the content, encrypted,
    for up to 6 months.

### D5 file:// check

(Session 3, right after D4; 10 min.)

1. **LAPTOP**:

   ```
   open ~/nnp-drills/mirror/index.html
   ```

   You should see, all offline from `file://`: the "read-only copy of
   nonnewtonian.org, refreshed nightly" note; Textbooks -> a textbook -> its
   chapter table; a scientist page with its photo; "Download slide" saving a
   `.pptx` that opens. Search and the whole-book deck links go to
   `https://nonnewtonian.org/...` (by design).

## Lost manage link

1. SMTP on (Session 4): point the teacher at `https://nonnewtonian.org/recover`.
   It only works if they gave an email when creating the class (the field is
   optional) and typed it correctly (it is never verified).
2. Otherwise, verify the request BEFORE touching anything: it must come FROM
   the class's stored teacher email (shown in `ADMIN_URL` -> Classes), or be
   confirmed by phone or in person if the class has no email.
3. `ADMIN_URL` -> Classes -> "Re-issue manage link" on that class. The click
   kills the old link at once. You should see: the new manage link and
   welcome link, shown once, and a prefilled `mailto:` link. Do not reload
   or go Back on that page: the resubmitted form does NOT mint a second link
   (the swap only happens if the class's link is still the one the form was
   loaded with), and you land on the admin page with `Not re-issued: the
   manage link for class N changed after that form was loaded ...`; the link
   just shown still works. To re-issue again, press the button on the
   freshly loaded admin page.
4. Send it out of band (that `mailto:`, to the verified address). It is not
   stored anywhere readable; if you lose it, re-issue again.

## Takedown

A takedown request names a scientist page, a student name, or a photo.

1. Find the entry ids. A class entry's id is in its slide URL
   (`/c/<slug>/entry/<id>/slide.pptx`). A class entry that was shared and
   approved has a SEPARATE communal copy with its own id; taking down one does
   not remove the other. To list every entry for a scientist slug:

   ```
   sudo -u nnp sqlite3 /var/lib/nnp/app.db "SELECT id, collection_id, scientist_name, communal_status FROM entries WHERE scientist_slug='<slug>';"
   ```

   You should see: one row per entry; `collection_id` empty = communal.
2. Remove: `ADMIN_URL` -> "Take down any entry" with each id -> "Look
   up...", check the confirm page (scientist, class, contributor, status,
   photo count) names the entry you mean, then "Take down entry N" (or "Take
   down" in the "Published on the communal site" list, a one-step button
   with a browser confirm). You should see: `Entry N taken down.` The
   entry's photo file is deleted only if no other entry references it.
3. If the complaint is about a photo, confirm it is gone. Its `file_path` is
   the part of its URL after `/photos/`:

   ```
   sudo -u nnp sqlite3 /var/lib/nnp/app.db "SELECT entry_id FROM photos WHERE file_path='<2 hex>/<64 hex>.jpg';"
   ```

   You should see: no rows. Any row is another entry using the same image
   (content-addressed storage shares files); take that one down too if the
   photo itself is the problem.
4. **LAPTOP** (substitute the scientist slug and the photo path):

   ```
   for u in /scientists/<slug> /photos/<2 hex>/<64 hex>.jpg; do curl -s -o /dev/null -w "%{http_code} $u\n" "https://nonnewtonian.org$u"; done
   ```

   You should see: `404` for both (a scientist page stays 200 while any other
   approved entry for that slug exists).
5. Mirror and outage fallback (once Session 3 is done):

   ```
   sudo systemctl start nnp-mirror.service; journalctl -u nnp-mirror.service -n 3 --no-pager
   ```

   You should see: `pushed <sha> (tree <sha>)`. This also refreshes
   `/var/lib/nnp/export/current`; until it runs, Caddy's outage fallback would
   still serve the old archived page. If it fails, see 3.4.
6. Reply with the honest scope: removed from the live site and from the
   mirror branch; browsers may cache the photo up to 1 day and GitHub Pages
   for minutes; the encrypted off-site backups keep it for up to 6 months (the
   privacy page says so); GitHub may serve the old mirror commit by SHA (your
   D4 result); copies other people already made cannot be recalled.
7. A real legal takedown of the mirror: delete `mglerner/nonnewtonian-mirror`
   on GitHub, recreate it empty with the same name, re-add the deploy key
   (`sudo cat /var/lib/nnp/.ssh/mirror_ed25519.pub`, write access), then
   `sudo systemctl start nnp-mirror.service` (it sees no `gh-pages` and does a
   first push) and re-enable Pages. Forks of a deleted public repo are not
   deleted with it (GitHub behavior, from memory; check before relying on it).
   Purging it from the backups means `restic forget` on every snapshot that
   holds it, which destroys that history; decide case by case.

Pending and rejected entries' photos are reachable by anyone who has their
unguessable content-hash URL (Caddy serves the whole photo tree behind a
strict hex-path regex, with no approval check), and a rejected photo stays on
disk until its class is deleted. A takedown of the entry removes the file.

## Restore and rebuild

### In place (bad data, bad migration)

1. If rolling back a migration, first put the code back:
   `sudo git -C /opt/nnp checkout <sha>`, where `<sha>` is the commit that
   matches the snapshot's schema. `/etc/nnp/last-good-commit` is the last
   commit that passed install.sh's healthz check: after a FAILED update that
   is the pre-update commit, after a successful one it is the new commit.
2. Follow `deploy/backup/README.md` "Restoring for real", with one change if
   you want an older snapshot than the latest: pick its id from
   `restic snapshots` (`restic snapshots --tag pre-migration` lists only the
   pre-migration ones; take the one just before the update) and use
   `restic restore <id> --target /root/restore`.
3. ```
   sudo /opt/nnp/deploy/install.sh
   ```

   You should see: step 7 `up to date`, step 8 `local healthz: ok`.
4. ```
   sudo rm -rf /root/restore /root/pre-restore
   ```

   You should see: no output (both held plaintext copies). Return to `main`
   before the next update (see [Updating](#updating)).

### Rebuild on a new instance

Backups hold `app.db` and `photos/` only; everything under `/etc` comes from
the repo, install.sh, and the password manager.

1. 1.2 (new instance), then 1.3 pointing the A record at the NEW IP (the old
   box is assumed dead), then 1.4 (clone, first `install.sh`; it stops at
   "DB missing").
2. Keep the old admin URL and secret key (optional; otherwise save the new
   ones from 1.4): replace the two values with the password-manager copies.

   ```
   sudo nano /etc/nnp/nnp.env
   sudo grep -c -e '^NNP_SECRET_KEY=..' -e '^NNP_ADMIN_TOKEN=..' /etc/nnp/nnp.env
   ```

   You should see: `2`. (Manage links survive either way: only their hashes
   are stored, independent of the secret key.)
3. Backup config from the password manager: `deploy/backup/README.md` steps
   4 and 5.
4. Restore:

   ```
   sudo bash -c 'set -a; . /etc/nnp-backup.env; set +a; restic snapshots'
   sudo bash -c 'set -a; . /etc/nnp-backup.env; set +a; restic restore latest --target /root/restore'
   sudo install -o nnp -g nnp -m 600 /root/restore/var/lib/nnp-backup/staging/app.db /var/lib/nnp/app.db
   sudo rm -rf /var/lib/nnp/photos
   sudo cp -a /root/restore/var/lib/nnp/photos /var/lib/nnp/photos
   sudo chown -R nnp:nnp /var/lib/nnp/photos
   sudo chmod -R u=rwX,g=rX,o= /var/lib/nnp/photos
   sudo rm -rf /root/restore
   ```

   You should see: the snapshot list (host `nnp`; older snapshots show the
   old box's hostname), restic finishing without errors, and no other
   output. (A fresh box has no stale `-wal`, so nothing to move aside.)
   Nothing needs pruning by hand after a rebuild apart from pre-migration
   snapshots: the nightly `restic forget` groups by tag (`--group-by tags`),
   not by host, so the new box prunes the old box's snapshots too.
5. ```
   sudo /opt/nnp/deploy/install.sh
   sudo /opt/nnp/deploy/install.sh --enable-backups
   ```

   You should see: `local healthz: ok`, `public https: ok`; then the backup
   output as in 1.8 (the repository exists, so no `restic init`).
6. Self-check: 2.2 (the healthchecks.io check is unchanged; reuse its URL).
7. Mirror: the old deploy key died with the box. Delete it from the mirror
   repo's Deploy keys, then 3.1 step 2-3 and 3.2 (new key, same repo).
8. UptimeRobot needs nothing. **LAPTOP**: `curl -fsS
   https://nonnewtonian.org/healthz; echo`. You should see: `ok`.

## Away for a month

- [ ] healthchecks.io and UptimeRobot alerts reach a mailbox you will read
      (optionally add a second contact on both).
- [ ] All checks green: `nnp-backup`, `nnp-selfcheck`, `nnp-mirror` (if set).
- [ ] Registrar (both domains) and DreamHost auto-renew on; card valid.
- [ ] Backblaze card valid.
- [ ] The password-manager entry has everything in 1.1, plus the admin URL.
- [ ] Last D2 restore drill within 6 months (see the drill log).
- [ ] No pre-migration snapshot you no longer need:
      `restic snapshots --tag pre-migration` per `deploy/backup/README.md`
      "Pre-migration snapshots (prune by hand)"; nothing else prunes them.
- [ ] No pending update half-applied: `git -C /opt/nnp status -sb` shows
      `## main...origin/main` and nothing else.

Neither monitor can see a domain or billing lapse from the box itself;
auto-renew is the only guard.

## Incident quick reference

| Symptom                                  | First command                                                               | Likely fix                                                                                    |
|------------------------------------------|-----------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------|
| Site down / UptimeRobot DOWN             | `systemctl status nnp caddy --no-pager; journalctl -u nnp -n 50 --no-pager` | `production preflight failed:` lists every problem; fix nnp.env, `sudo systemctl restart nnp` |
| nnp restarting in a loop after an update | `journalctl -u nnp -n 50 --no-pager`                                        | Roll back per [Updating](#updating)                                                           |
| OOM kill                                 | `journalctl -k --no-pager -g oom`                                           | Resize the instance; then raise `MemoryHigh`/`MemoryMax` in `nnp.service` (repo)              |
| Deck downloads return 503 "busy"         | `journalctl -u nnp -n 50 --no-pager`                                        | One build slot by design; after a resize raise it in `views_public.py` (see below)            |
| Deck downloads return 429                | (none; 6 per minute per IP on each deck route, in memory)                   | Wait a minute; a school NAT shares the limit; see below                                       |
| Deck download returns 413                | (none)                                                                      | The deck's stored photos exceed 100 MB; slides one entry at a time, or raise the cap (below)  |
| A class gets 429 "Too Many Requests"     | (none; limits are per IP, in memory)                                        | School NAT; see below                                                                         |
| `/healthz` 503 "low disk"                | `df -h /var/lib/nnp; sudo du -sh /var/lib/nnp/* /var/log/journal`           | Under 256MB free; clear space or grow the disk                                                |
| Self-check: certificate expires soon     | `journalctl -u caddy -n 100 --no-pager`                                     | Usually DNS or port 80 blocked; fix, `sudo systemctl reload caddy`                            |
| Self-check: a unit failed                | `systemctl --failed; journalctl -u <unit> -n 40 --no-pager`                 | Fix, rerun the unit, `sudo systemctl reset-failed <unit>`                                     |
| Self-check: symlinks under photos/export | `sudo find /var/lib/nnp/photos /var/lib/nnp/export -type l -ls`             | Suspected app compromise; see below                                                           |
| Self-check: orphan photo temp files      | `sudo find /var/lib/nnp/photos -name '.*.tmp' -mtime +1 -ls`                | Left by a kill mid-write (OOM); delete them, see below                                        |
| install.sh: `<path> is a symlink; ...`   | `sudo find /var/lib/nnp -maxdepth 2 -type l -ls`                            | Suspected app compromise; see below. Do not rerun install.sh until explained                  |
| install.sh: pre-migration backup failed  | the restic output just above the error                                      | Fix the backup (as for a red backup check); rerun install.sh; nothing was migrated            |
| `database missing at ...`                | `sudo ls -l /var/lib/nnp`                                                   | Upload the seeded or restored DB (1.6 or a restore), rerun install.sh; never `--create`       |
| Backup check red                         | `journalctl -u nnp-backup.service -n 40 --no-pager`                         | Read the error; `sudo systemctl start nnp-backup.service` when fixed                          |
| Mirror run failed                        | `journalctl -u nnp-mirror.service -n 20 --no-pager`                         | See 3.4; the mirror AND the outage fallback stay at the last good export                      |
| Leak guard: contributor name of entry N  | the `sqlite3` SELECT below                                                  | A real leak: fix the cause. A name collision: see below                                       |
| Lost admin URL                           | `sudo grep '^NNP_ADMIN_TOKEN=' /etc/nnp/nnp.env`                            | Or rotate: new value in nnp.env, `sudo systemctl restart nnp`; save it                        |
| Caddy rejects the status line            | `sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new` | See below                                                                                     |
| Caddy rejects the log block              | `sudo caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new` | See below                                                                                     |
| Caddy log shows `REDACTED`, not the URL  | `journalctl -u caddy -n 50 --no-pager`                                      | By design (tokens are URL paths); match by time against `journalctl -u nnp`                  |
| A teacher lost the manage link           | (none)                                                                      | [Lost manage link](#lost-manage-link)                                                         |

Deck builder busy: `_HEAVY_BUILD = threading.BoundedSemaphore(1)` and
`HEAVY_BUILD_WAIT_SECONDS = 2` in `src/nonnewtonian/web/views_public.py`.
The slot is held from the start of the build until the download finishes,
so one slow downloader makes every other deck request a fast 503 (with
`Retry-After: 30`) for that long. The same file has `DECK_RATE_LIMIT = "6
per minute"` (per IP, separately for `/textbooks/<slug>/deck.pptx` and
`/c/<slug>/deck.pptx`) and `DECK_PHOTO_BYTES_CAP = 100 * 1024 ** 2` (the
413). Raise the slot count or the cap only after a memory resize and a fresh
1.7 step 6 measurement (and raise `MemoryHigh`/`MemoryMax` with it), then
[update](#updating).

429 for a class: the submit limit is `@limiter.limit("200 per hour;60 per
minute")` on `submit_post` in `src/nonnewtonian/web/views_class.py`, per
public IP, so a whole school behind one NAT shares it. Raise it in the repo
and update; `sudo systemctl restart nnp` resets every counter at once as a
stopgap (it also resets the spam limits).

Leak guard on a contributor name: look at the entry first.

```
sudo -u nnp sqlite3 /var/lib/nnp/app.db "SELECT id, collection_id, scientist_name, contributor_name, attribution_mode FROM entries WHERE id=N;"
```

You should see: one row. If the hidden name genuinely occurs in public text
for another reason (a one-word name that is also a whole word of a
scientist's name, such as an anonymous "Grace" beside "Grace Hopper"), it
is a false positive; the export refuses until the stored name no longer
matches. Clearing it loses only a name that was never shown:
`sudo -u nnp sqlite3 /var/lib/nnp/app.db "UPDATE entries SET contributor_name=NULL WHERE id=N;"`,
then `sudo systemctl start nnp-mirror.service`. That is your judgment call;
any other leak-guard message (token, class slug, teacher email, a forbidden
link) is a real bug: do not work around it.

Symlinks under `/var/lib/nnp` (self-check `symlinks under
/var/lib/nnp/photos or /var/lib/nnp/export (first 20):` or `(find failed)
...`, or install.sh's `is a symlink` refusal): the app never creates
symlinks there, so treat any hit as a compromise of user nnp. Caddy's
`file_server` follows symlinks, so a planted link can serve any file the
caddy user can read, including Caddy's own TLS and ACME private keys under
`/var/lib/caddy`. Look first:

```
sudo find /var/lib/nnp -type l -ls
```

You should see: each link and its target. Note them, then remove the links
(`sudo find /var/lib/nnp -type l -delete`; `find -delete` removes the link,
never its target), rerun `sudo systemctl start nnp-selfcheck.service`, and
find out how they got there before trusting the box. If any target was
under `/var/lib/caddy`, rotate the certificate and ACME account key: `sudo
systemctl stop caddy`, move `/var/lib/caddy/.local/share/caddy` aside,
`sudo systemctl start caddy` (it obtains new ones; path from memory, not
verified: check `sudo ls /var/lib/caddy/.local/share/caddy` first).

Orphan photo temp files (self-check `orphan photo temp files older than a
day (delete them):`): photo intake writes `.<hash>.<pid>.<tid>.tmp` and
renames it into place; a kill mid-write (usually an OOM kill) leaves the
temp file, which restic would back up forever. Check `journalctl -k
--no-pager -g oom` for the cause, then:

```
sudo -u nnp find /var/lib/nnp/photos -name '.*.tmp' -mtime +1 -delete
sudo find /var/lib/nnp/photos -name '.*.tmp' | wc -l
```

You should see: `0` (or only files from the last day, which a running
upload may still own).

Caddy rejects the log block (the `fields { }` sub-block under `format
filter` in the global options, which an older Caddy may not accept): the
old config keeps running, as below. LAPTOP: in `deploy/caddy/Caddyfile`,
delete the `fields {` line and its closing `}` so the three `request>...
replace REDACTED` lines sit directly under `format filter`, commit, push,
update, and redo the redaction check at the end of 1.4 (stop nnp first:
`sudo systemctl stop nnp`, then `sudo systemctl start nnp` after).

Caddy rejects `status {err.status_code}`: install.sh validates
`/etc/caddy/Caddyfile.new` before replacing the live file, so the old config
keeps running. LAPTOP: delete the `status {err.status_code}` line (and its
braces) in `deploy/caddy/Caddyfile`, commit, push, update. Consequence:
during an outage, `down.html` and archived pages return 200, so the
UptimeRobot status monitor no longer sees an outage. Switch it to a keyword
monitor on text present only in a live `/healthz`; the body is just `ok` and
`down.html` contains "OK" inside "RUNBOOK", so that needs a code change to
the healthz body first.

## Known limitations

These are deliberate, from the M7 design, unless marked otherwise.

- Pending and rejected photos are reachable by their unguessable
  content-hash URL (no approval check, same as the app's own `/photos`
  route); rejected photos are never purged until their class is deleted. The
  plan's separate published/staging photo trees do not exist.
- Takedown is not total: restic keeps removed content, encrypted, up to 6
  months (`KEEP_MONTHLY=6`); GitHub may serve an old mirror commit by SHA;
  browsers cache photos up to 1 day.
- Class pages are excluded from the export and mirror (decided 2026-10-25;
  spec's "whole communal site" read as communal only). Per-class opt-in
  archiving, gated on a "close class" state that does not exist yet, is the
  planned post-launch follow-up.
- One gunicorn process with 8 threads (plan: 2 processes). Rate-limit and
  recovery counters live in that process's memory and reset on every restart
  (including unattended reboots); IPv6 clients are limited per address, so a
  /64 can rotate past them.
- Whole-book and class deck builds share ONE slot, held through the
  download: a second request waits up to 2 s, then gets 503 with
  `Retry-After: 30`. A class downloading decks at once will see this, and so
  will everyone while one slow client is still downloading. Deck builds are
  still in the request path (no queue).
- Deck downloads are limited to 6 per minute per IP on each deck route, so
  a school behind one NAT shares 6 a minute (the 7th gets 429).
- A deck whose stored photos add up to more than 100 MB is refused with 413
  ("Download the slides one entry at a time instead").
- Photo intake: stored photos are shrunk to 2000 px on the long edge (the
  original is not kept); a non-JPEG image over 12 MP is rejected (JPEG is
  allowed up to 30 MP); a fetched image is capped at 8 MB, and a whole
  submit form post at 10 MB (a larger post is refused outright, entry
  included, with Flask's 413). One photo is processed at a time: a submit
  that waits more than 2 s keeps its entry but loses the photo, with the
  warning `the server was busy with other photos just now`.
- The gunicorn `timeout` is a worker heartbeat, not a per-request deadline;
  photo fetches have their own 20 s total deadline, enforced on the socket in
  every phase (connect, headers, body, each redirect) EXCEPT the DNS lookup,
  which follows the system resolver's timeouts and attempts
  (`/etc/resolv.conf`).
- The photo fetcher ignores proxy environment variables by design; if that
  line in `photos.py` were ever removed, a proxy variable reaching
  nnp.service would defeat the deadline and the IP pinning (1.4 checks for
  such variables once, the self-check does not).
- Caddy's `file_server` follows symlinks and nnp owns `/var/lib/nnp`, so a
  compromised app could plant a link that serves Caddy's TLS key over
  HTTPS. Not prevented; the daily self-check DETECTS such links, so one can
  be served for up to a day before you hear of it.
- Caddy's error log has the URI and client IPs replaced by `REDACTED`, so it
  cannot tell you which URL or client hit an error.
- Pre-migration snapshots are kept until you prune them by hand, past the
  six-month window the privacy page promises (see [Updating](#updating)).
- While the app is down, whole-book and class decks are unavailable; only
  pages in the last export (after Session 3) and photos keep working.
- The mirror and outage fallback refresh only when `nnp-mirror.service`
  succeeds; a failing export leaves the previous night's content in both.
- No per-route error alert: a broken page with a green `/healthz` is
  invisible until someone reports it.
- Recovery email: synchronous send gives a small timing difference between
  known and unknown addresses; teacher emails are optional and unverified.
- The server's B2 key can delete backups; the 30-day lifecycle (2.4) is a
  mitigation, not a guarantee.
- NOT verified where the code was written (your first run is the test):
  `caddy validate` (including the log redaction block and the outage
  fallback's trailing-slash redirect), `systemd-analyze verify`, the
  unattended-upgrades Caddy origin, the `nonnewtonian` console script and
  gunicorn install, `MemoryPeak`, every GitHub step (deploy key, push,
  Pages), real SMTP, and Python 3.12 on the server (pins in
  `deploy/constraints.txt` were tested on 3.14). install.sh's idempotence
  was checked by review and `bash -n`, not by an integration run. Checked
  against the vendors' pages on 2026-10-25: the Caddy apt-repo commands, the
  DreamCompute flavor name (`gp1.semisonic`) and default user (`ubuntu`).
- Cosmetic: every generated deck carries python-pptx's default document
  properties (author "Steve Canny", created 2013).

## Drill log

| Drill                         | Date | Result | Notes                                   |
|-------------------------------|------|--------|-----------------------------------------|
| 1.4 Caddy log redaction       |      |        | path/IP count, REDACTED count           |
| 1.7 smoke: deck time, memory  |      |        | deck s =       MemoryPeak =             |
| D1a kill -9, Caddy only       |      |        |                                         |
| 3.3 outage trailing slash     |      |        |                                         |
| D1b prober DOWN/UP            |      |        |                                         |
| D1c reboot                    |      |        |                                         |
| D2 laptop restore             |      |        | snapshot id, entry count                |
| D3a backup fail path          |      |        |                                         |
| D3b backup silence path       |      |        |                                         |
| D4 takedown                   |      |        | OLD sha, PHOTO, commit/$OLD HTTP code   |
| D5 file://                    |      |        |                                         |
