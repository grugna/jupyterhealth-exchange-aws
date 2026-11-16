# What JupyterHealth Exchange requires at runtime

For designing or reviewing any deployment of
[JupyterHealth Exchange](https://github.com/the-commons-project/jupyterhealth-exchange) (JHE),
on any platform. Everything here is a property of JHE itself, read from its source
(`jhe/settings.py`, `Dockerfile`, `deploy/entrypoint.sh`, the `patient_access` package,
`mcp_server/`). Nothing here is about AWS or about this repository; the
[README](../README.md#what-this-build-chose) says what this repository chose for each
requirement.

This document follows JHE, not this repository. Check it against the JHE version you deploy.

## Processes

| Process | What it is | Started by | Needs |
|---|---|---|---|
| **web** | gunicorn serving Django on port 8000. The image runs two synchronous workers with a 30-second timeout; tune by overriding the command | always on, behind a load balancer that terminates the public HTTPS and sends `X-Forwarded-Proto` | the database, the secrets, writable local disk, outbound internet |
| **a patient-access pull** | a thread *inside the web process* that handled the patient's consent (JHE's dispatcher is `thread`): fetch from the EHR into a local spool, deliver to object storage | a consent | the web process to live through the pull; see [Disk and state](#disk-and-state) |
| **`patient_access_run_jobs`** | the sweep: resumes pulls whose process died, retries failed deliveries, removes orphaned spools. JHE's own crontab runs it every minute | cron | the same as web |
| **`ow_poll`** | Open Wearables polling; exits at once while the module is off in Settings | cron, every 15 minutes | the same as web |
| **migrate**, then **bootstrap** | `manage.py migrate` before each release, then `manage.py bootstrap` | an operator, before the new version serves | the database credential |
| **one-off commands** | any `manage.py` command: seeds, checks, data fixes | an operator | as migrate |
| **mcp** | the MCP server, a separate image on port 8401, under its own hostname | always on | its own secret; it reaches JHE through JHE's public URL, never directly |

JHE ships a crontab for a [supercronic](https://github.com/aptible/supercronic) sidecar; any
scheduler that runs the two commands in the JHE image with the web process's environment
will do.

`manage.py bootstrap` is idempotent and creates what a migrated database lacks: the Settings
rows at neutral values, the reference data (codeable concepts, data sources, the root
organization), the `JHE Admin UI` OAuth application the browser login needs, the EHR vendor
rows without client ids, and the MCP broker's OAuth client when `MCP_OAUTH_CLIENT_ID` and
`MCP_OAUTH_CLIENT_SECRET` are in its environment. It leaves existing rows alone.

## Environment and secrets

Every variable `jhe/settings.py` reads. *Secret* means it must come from a secret store; the
rest are plain configuration.

| Variable | Required | Secret | What it is |
|---|---|---|---|
| `SECRET_KEY` | yes | yes | Django's signing key; rotating it invalidates sessions |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | yes | the password | Postgres |
| `DB_SSLMODE`, `DB_SSLROOTCERT` | | | libpq's TLS mode and CA bundle. Unset, the driver *prefers* TLS and never verifies the server; `verify-full` with the server's CA bundle makes the hop authenticated. The image's build argument `DB_CA_BUNDLE_URL` fetches a bundle to `/etc/ssl/certs/db-ca-bundle.pem` |
| `SITE_URL` | yes | | the public URL; `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` derive from it unless set |
| `OIDC_RSA_PRIVATE_KEY` | yes | yes | the key JHE signs its own OIDC tokens with |
| `PATIENT_ACCESS_ENCRYPTION_KEYS` (or `_FILE`) | for patient access | yes | comma-separated Fernet keys, the first encrypts; EHR tokens and spool keys at rest |
| `PATIENT_ACCESS_SPOOL_DIR` | | | where a pull waits between fetch and delivery; default `/data/patient-access/spool` |
| `ALLOWED_HOSTS` | | | comma-separated |
| `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS` | | | HSTS on HTTPS responses; off unless set |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | | user, password | the mail relay; see [Email](#email) |
| `OW_S3_ENDPOINT_URL`, `OW_S3_BUCKET`, `OW_S3_PREFIX`, `OW_S3_REGION`, `OW_S3_ACCESS_KEY`, `OW_S3_SECRET_KEY` | for Open Wearables | the keys | raw-payload storage for the Open Wearables module |
| `DJANGO_LOG_LEVEL`, `TIME_ZONE`, `X_FRAME_OPTIONS`, `JHE_IGNORE_DOTENV`, `PATIENT_ACCESS_HTTP_DEBUG_LOGGING` | | | plain knobs; `JHE_IGNORE_DOTENV=1` stops the image reading a `.env` file |
| `DEBUG` | | | **never set in a deployment**: debug mode, and with it plain-HTTP EHR endpoints. (Read as a flag: `false` is false) |
| `JHE_TLS`, `JHE_TLS_CN`, `JHE_TLS_DAYS`, `JHE_TLS_DIR`, `JHE_TLS_CERTFILE`, `JHE_TLS_KEYFILE` | | | the image's entrypoint: `self-signed` or `provided` makes gunicorn serve HTTPS; see [Names, certificates, TLS](#names-certificates-tls) |

The MCP server reads:

| Variable | Secret | What it is |
|---|---|---|
| `JHE_BASE_URL`, `JHE_REDIRECT_URI` | | JHE's public URL and the server's own callback |
| `JHE_CLIENT_ID`, `JHE_CLIENT_SECRET` | yes | the OAuth client registered in JHE; see [The MCP server's OAuth client](#the-mcp-servers-oauth-client) |
| `MCP_BROKER_KEY` | yes | the key behind the client registrations and tokens the server issues, 32 characters or more; rotating it revokes them all |
| `MCP_RESOURCE_URL`, `MCP_REQUIRE_AUDIENCE` | | the server's own public URL, and whether tokens must name it as audience (off by default) |
| `MCP_HTTP_PORT` | | 8401 by default |
| `MCP_TLS`, `MCP_TLS_CN`, `MCP_TLS_CERTFILE`, `MCP_TLS_KEYFILE` | | as `JHE_TLS` above, for the MCP container |

Its `/register` endpoint is open dynamic client registration, and `mcp_server/README.md` asks
for rate limiting at an edge.

## Settings rows

Some of what JHE needs is database rows under Settings in the web UI (or the Django admin's
*JHE settings*), not configuration. `bootstrap` creates the rows with neutral values. The
operator must then set these:

- `site.registration_invite_code`: the code a visitor must give to self-register. Created
  empty, which keeps registration closed; set it to open registration. The development seed
  (`manage.py seed`) sets it to `jhe`.
- `patient_access.s3.bucket`, `patient_access.s3.region`, `patient_access.s3.encryption`, and
  optionally `patient_access.s3.endpoint_url`, `patient_access.s3.layout`,
  `patient_access.s3.access_key_id` and `patient_access.s3.secret_access_key`: where pulls are delivered; see
  [Object storage](#object-storage). They do not exist until created. Until they are usable,
  every pull waits at "Starting" and the web log says `settings not usable`.
  `manage.py patient_access_check` reads them as a job would and writes and deletes a marker.
- The EHR vendor rows: the client id of your own EHR app registration, entered on the EHRs
  page; brands and locations imported from the EHR's published bundle with
  `import_ehr_brands`.

Settings rows are cached per process for 60 seconds.

## Database

Postgres. One role for everything: JHE has no notion of a separate migration credential, so
whoever runs `migrate` holds the same credential the web process serves with. Require TLS on
the server and verify it from the client (`DB_SSLMODE`, above). Sessions, tokens and all
application state live in the database; the web process keeps nothing locally that another
copy would need.

## Object storage

Each pull becomes one FHIR document bundle written to S3, or any S3-compatible store, under a
layout the module computes. Where, and with what credentials, comes from the
`patient_access.s3.*` Settings rows, not the environment: bucket, region, optional endpoint,
encryption mode (`bucket-default` leaves encryption to the bucket), and either an access key
pair or nothing, in which case the process's own identity is used (on AWS, its IAM role). The
bucket must allow put, get and a delete of the module's probe marker.

If the bucket is versioned, a rewritten or deleted bundle leaves prior versions behind: a
data-deletion request must purge versions, not only the current object.

## Disk and state

A pull spools fetched documents to local disk under `PATIENT_ACCESS_SPOOL_DIR` before
delivery, up to the module's caps: `patient_access.documents.max_total_bytes`, 20 GiB by
default, per pull. The web process therefore needs writable local disk sized for the
concurrent pulls you expect, or lower caps. With `JHE_TLS=self-signed` the container also
writes its own certificate at start. The image runs as an unprivileged user (uid 10001) that
owns only `/data` and `/run/jhe-tls`.

Because the pull runs inside the web process, replacing or scaling in a web process mid-pull
loses that pull until the next sweep resumes it, refetching while the EHR's token is still
valid and failing otherwise. Give web processes the longest stop timeout the platform allows
and roll them one at a time; several copies can serve at once, with that one caveat.

Static files are served by the application itself (whitenoise); no CDN or static bucket is
needed.

## Network

Inbound: the public HTTPS listener only. Behind it, port 8000 on the web containers and 8401
on MCP from the load balancer, and 5432 on Postgres from the JHE processes. `GET /health` is
the liveness probe: no database, no auth, answers any Host header.

Outbound, from web, the scheduled jobs and one-off commands: **any HTTPS host on the
internet**, because the EHR FHIR servers are whatever the brands table names (and the Open
Wearables API, if used), so egress cannot be allow-listed by address; plus the object store,
the secret store and the mail relay. JHE refuses plain-HTTP EHR endpoints unless `DEBUG` is
set. The MCP server needs only JHE's public URL and its own clients.

## Names, certificates, TLS

Two hostnames, JHE's and the MCP server's, with a public certificate for both. JHE's browser
login is OIDC against JHE itself, which requires HTTPS for any host but localhost.

A load balancer may re-encrypt to the container. With `JHE_TLS=self-signed` (or `MCP_TLS`)
the container issues its own key and certificate at start and serves HTTPS with it; with
`provided` it serves the files `JHE_TLS_CERTFILE` and `JHE_TLS_KEYFILE` name. Unset, it
serves plain HTTP.

## The MCP server's OAuth client

The MCP server is an OAuth client of JHE and needs an application registered there. Either
`bootstrap` creates it (when `MCP_OAUTH_CLIENT_ID` and `MCP_OAUTH_CLIENT_SECRET` are in its
environment), or an admin adds it at `/admin/`, under *Django OAuth Toolkit › Applications*:

| Field | Value |
|---|---|
| Name | `JHE MCP Server` |
| Client type | Confidential |
| Authorization grant type | Authorization code |
| Redirect uris | `https://<mcp host>/oauth/callback` |
| Algorithm | RSA with SHA-2 256 |
| Skip authorization | checked |
| Client id, client secret | as generated; **copy the secret before saving**, it is hashed on save and never shown again |

The client id and secret are then the MCP server's `JHE_CLIENT_ID` and `JHE_CLIENT_SECRET`.

## Email

JHE sends invitation and one-time-code mail through SMTP on port 587 with STARTTLS, from the
hardcoded address `support@jhe.org`, so a relay must be allowed to send as that domain. With
no `SMTP_HOST`, mail is dropped and one warning is logged at startup (in development, with
`DEBUG`, it is printed to the log instead); invitation links are then copied from the admin.

## Rotating keys

- `PATIENT_ACCESS_ENCRYPTION_KEYS` is a list: prepend a new Fernet key and restart. The first
  key encrypts; the old keys stay to decrypt what they encrypted.
- `OIDC_RSA_PRIVATE_KEY`: replacing it invalidates every outstanding token.
- `SECRET_KEY`: replacing it invalidates every session.

Processes read all three at start, so a changed value needs a restart.
