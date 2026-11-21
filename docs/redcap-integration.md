# REDCap integration: invitation links from a Data Entry Trigger

When a REDCap record is saved, REDCap calls a URL (its *Data Entry Trigger*). This optional part
of the deployment is that URL: a Lambda that

1. checks a shared token in the URL,
2. reads the record's e-mail (and whether it already has a link) through REDCap's API,
3. signs in to JHE as an integration account with an API key,
4. finds or creates the patient in a JHE organization and creates an invitation to the
   *USCDI Patient Access* client, which returns the link,
5. writes the link into a field of the same record.

```
REDCap --DET POST--> Lambda function URL (?token=…) --> JHE  (token, patient, invitation)
                              └────────────────────--> REDCap API (read record, write link)
```

**Nothing here exists unless `redcap_integration` is set in the environment's configuration.**
Unset (the default, `null` in `config/example.json`), `jhe-deploy` creates no stack, no secret and no
function, and `secrets` and `status` ignore it.

## Configuration

In `config/local/<env>.json`:

```json
"redcap_integration": {
  "redcap_api_url": "https://redcap.example.org/api/",
  "email_field": "email",
  "link_field": "jhe_link",
  "record_id_field": "record_id",
  "organization_id": 1,
  "jhe_client_name": "USCDI Patient Access"
}
```

| key | meaning |
|---|---|
| `redcap_api_url` | the REDCap API endpoint (https only) |
| `email_field` | the field holding the patient's e-mail |
| `link_field` | the field the link is written to (a text field; it must be empty for a link to be made) |
| `record_id_field` | the project's record id field, default `record_id` |
| `organization_id` | the JHE organization new patients join (the integration account must be a member) |
| `jhe_client_name` | the JHE client the invitation is for, found by name; default `USCDI Patient Access` |
| `memory_mb`, `timeout_seconds` | the function's size (256) and time limit (30) |

JHE's address is `compute.domain_name`; it is not repeated here.

## Setting it up

**1. JHE: the integration account and its key.** Make a practitioner for the integration
(for example a service mailbox), add it to the organization as `member`, and create its API key
(`PractitionerClient`). The JHE side is covered in the runbook's server-to-server section; the
key's owner must be in the `auth.practitioner_clients.allowed_emails` setting if that setting is
used.

**2. REDCap: an API token.** In the project, give a user (or a dedicated service user) *API
Export* and *API Import/Update* rights on the project and generate its token. The function only
exports the record id, e-mail and link fields and imports the link field.

**3. Deploy.** Add the block above, then:

```bash
jhe-deploy -c config/local/<env>.json secrets init     # creates jhe/<env>/redcap, generates TRIGGER_TOKEN
jhe-deploy -c config/local/<env>.json secrets set --secret redcap JHE_API_KEY=<key> REDCAP_API_TOKEN=<token>
jhe-deploy -c config/local/<env>.json deploy ...        # the redcap stack comes with compute
```

`secrets set` takes the values from the command line you type; for a value you would rather
not put in a shell history, set the field in the Secrets Manager console. Deploy prints the
trigger URL. Read the generated token with
`aws secretsmanager get-secret-value --secret-id jhe/<env>/redcap --query SecretString --output text`.

**4. REDCap: the trigger.** *Project Setup → Additional customizations → Data Entry Trigger*:

```
https://<function-url>/?token=<TRIGGER_TOKEN>
```

Add a field (for example `jhe_link`) to the instrument, matching `link_field`.

## What it does on each save

REDCap fires the trigger on **every** save of the record, so the function is idempotent: if
`link_field` already has a value it does nothing (`{"status": "skipped"}`). The link is therefore made
once per record. To issue a new one, clear the field and save.

If the e-mail already belongs to a JHE patient, that patient is reused (and added to the
organization if they were not in it); otherwise one is created from the e-mail alone. The
trigger carries no field values, so the function reads the record back from REDCap; an
`email` sent with the trigger (query or body) takes precedence.

| answer | meaning |
|---|---|
| 200 `ok` / `skipped` | link written / record already had one |
| 400 | the request had no `record` |
| 403 | wrong or missing token |
| 404 | REDCap has no such record |
| 422 | the record has no valid e-mail address |
| 502 | JHE or REDCap refused a call (the function's log has the status; the answer does not repeat it) |
| 503 | the secret's trigger token or API credentials are empty |

Logs: `jhe-deploy logs redcap --since 1h`. They name records by REDCap id and never contain
tokens, e-mail addresses or links.

## What to know

- **The URL is public.** REDCap has to reach it from the internet, so it cannot sit behind the
  JHE allowlist. The token in the URL is the only gate; it is compared in constant time, and
  anyone holding the URL can make links for records REDCap confirms exist. Treat the URL like a
  password: REDCap stores it in the project settings, and a leak is fixed by setting a new
  `TRIGGER_TOKEN` in the secret and in REDCap. A wrong token costs one cheap invocation.
- **The integration account** can create patients in its organization and create invitations;
  JHE's invitation endpoint does not check the caller's role, so give the account only the
  `member` role in one organization and watch what it does.
- **Timing.** A trigger waits for the function (typically a few seconds: one token, three JHE
  calls and two REDCap calls). If REDCap times out the call, the function still finishes and the
  link appears; the next save sees it and skips.
- **Secret values** are read from `jhe/<env>/redcap` and kept for five minutes, so a changed
  token or key is in use within five minutes, with no deploy.
- **Removing it:** delete the `redcap_integration` block and deploy; the stack is removed. The
  secret outlives it (like the others); delete it by hand if it is no longer wanted.
