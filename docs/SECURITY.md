# Security — secret handling and incident response

> Audience: whoever operates or deploys Onti.
> See [../README.md](../README.md) for setup and
> [TECHNICAL.md](TECHNICAL.md) for architecture.

---

## 1. Secrets this application holds

| Secret          | Where it lives    | Why it matters               |
| --------------- | ----------------- | ---------------------------- |
| Master key      | `.onti.key` / env | Signs cookies, encrypts keys |
| Model API keys  | `model_configs`   | Billable credentials         |
| Session cookies | Browser only      | Full access, incl. admin     |
| User passwords  | `users` (PBKDF2)  | Account access               |

Only the **master key** protects the others at rest, so a leaked master
key is equivalent to leaking every stored model key, plus the ability
to forge any session cookie — including an administrator's.

### Golden rules

* Never commit `data/.onti.key`, `.env`, or a database that carries real
  provider keys.
* On a deployed host set `ONTI_SECRET_KEY` in the platform environment;
  do not ship a key file.
* Keep `data/onti.db` free of secrets. It is a seed file and ships in
  the repository.
* Rotate a credential the moment it appears in a commit — deleting the
  file afterwards does not un-leak it.

## 2. Generating a master key

`modules/db.py` creates `data/.onti.key` on first start when the file is
absent and `ONTI_SECRET_KEY` is unset. To create one explicitly:

```powershell
python -c "import secrets, pathlib; pathlib.Path('data').mkdir(exist_ok=True); \
pathlib.Path('data/.onti.key').write_bytes(secrets.token_bytes(32))"
```

On a hosting platform, set the environment variable instead:

```powershell
# 32 random bytes, base64 encoded, then pasted into the platform UI
python -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

`ONTI_SECRET_KEY` takes precedence over the file, so a platform-hosted
instance never needs the key on disk.

On an ephemeral host (Vercel) with neither variable nor file available,
`_machine_key` derives the key from the storage credential the deployment
already shares (`BLOB_READ_WRITE_TOKEN` or `ONTI_SNAPSHOT_TOKEN`) so every
instance validates the same session cookies. That fallback inherits the
credential's lifecycle: rotating the storage token also rotates the master
key and invalidates sessions and encrypted model keys. Set
`ONTI_SECRET_KEY` explicitly to decouple the two.

## 3. Incident response — leaked credential

Work top to bottom. Steps 1 and 2 stop the damage; the rest clears the
finding.

1. **Revoke first.** Delete or roll the exposed key at the provider
   (SiliconFlow, Deloitte DNova, or whichever service it belongs to).
   Revocation is the only step that ends the exposure — a forced key
   rotation on our side does not invalidate theirs.
2. **Rotate the master key.** Delete `data/.onti.key`, generate a new one
   (§2), and set a new `ONTI_SECRET_KEY` on every deployed environment.
   Every existing session becomes invalid, which is intended. Stored
   model keys encrypted with the old master key can no longer be read:
   re-enter fresh keys in the Model centre.
3. **Remove the secret from the working tree.** Edit the file to drop the
   value, and add the path to `.gitignore`. Commit that on its own.
4. **Purge the value from history.** Deleting the file in a new commit
   leaves the old blobs reachable, so scanners keep reporting it. Use
   `git filter-repo` (preferred) or `git filter-branch`:

   ```bash
   # replace the exact leaked string everywhere it ever appeared
   git filter-repo --replace-text <(echo "LEAKED_VALUE==>REDACTED")
   ```

   Then force-push every branch and tag and tell every collaborator to
   re-clone. A force push rewrites shared history: get the repository
   owner's explicit approval, and never force-push a protected branch
   without coordinating with the team.
5. **Verify.** Run the guard test and confirm the value is gone from
   both the tree and history:

   ```powershell
   python tests\verify_secrets.py
   git log --all -S "LEAKED_VALUE" --oneline
   ```

   The second command must print nothing.
6. **Close the alert.** Reply to GitGuardian with the revocation time,
   the rotation, and the commit range that removed the value. Mark the
   incident resolved once the scan is clean.
7. **Restrict visibility.** If the repository must keep shipping data
   files, make it private. A public repository with committed databases
   will keep producing findings.

## 4. Authentication and password policy

All accounts are e-mail accounts and all sessions are **two-factor**:
the password is the first factor, and one of two second factors is verified
before a cookie is issued. A password alone never produces a cookie.

| Control          | Implementation                                                                                                                                            |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Username         | Derived from the address (`alice@example.com` → `alice`), suffixed `-2`, `-3`… on collision. Never entered by the user.                                   |
| Password length  | ≥ 10 and ≤ 256 characters                                                                                                                                 |
| Password classes | At least 3 of {lowercase, uppercase, digit, symbol}                                                                                                       |
| Password history | The last 24 months are kept in `password_history`; a hash match rejects the change                                                                        |
| Rotation         | 90-day window (`password_changed_at`); a banner appears in the last 14 days and after expiry, with a link to `/account/password`                          |
| E-mail OTP       | Six random digits, `SHA-256(challenge_id:code)` stored — never the code; 10-minute TTL, 60-second resend cooldown, 5 attempts, single use                 |
| Authenticator MFA | Per-account flag in `users.mfa_enabled`. When set, sign-in branches to a TOTP step (`/api/auth/signin/mfa/verify`) using a base32 secret stored as `mfa_secret`; QR provisioning via `/api/auth/mfa/bind`. One factor only on any account — MFA replaces the e-mail OTP while enabled. |
| Legacy accounts  | No address → password step returns `bind_required` with a 15-minute HMAC-signed bind token; the address is confirmed by code before any session is issued |
| Sessions         | HMAC-signed HttpOnly `SameSite=Lax` cookie, also recorded in `sessions`; logout deletes the row and writes a revocation entry                             |
| Duplicate guard  | Self-registration against an address already in `users` returns HTTP 409 `"already registered"` — a front-end-friendly path to tell the user to sign in instead of retry. |

Sign-up deliberately writes **nothing** to `users` until the code verifies,
so a mistyped address or an abandoned form cannot leave a half-created
account that later blocks the same address.

Codes are delivered through Resend (`RESEND_API_KEY`) or SMTP
(`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
`SMTP_SECURITY`). Neither configured → every code request answers **503**;
the app never pretends a message was sent. `ONTI_MAIL_ECHO=1` prints the
code and returns `debug_code`; it is a local debugging switch and must not
be set on a public deployment.

Challenges are stored in SQLite **and** mirrored to the durable store
(`onti/shared/otp/<sha256-of-challenge>.json`) because the instance that
sends a code may not be the instance that verifies it. They carry only a
hash and expire in ten minutes; the mirror is deleted when the challenge is
consumed or superseded.

Administrators resetting a password through the console are subject to the
same policy, and the reset is written to `password_history` like a
self-service change. Roles, enable/disable and password resets are audited.

## 5. What the guard test enforces

`tests/verify_secrets.py` runs in the normal suite and fails when:

* `data/.onti.key` is tracked, or `.gitignore` stops covering it;
* the bundled `data/onti.db` carries any encrypted model secret or a
  live session;
* a tracked text file contains a credential-shaped string
  (`sk-…`, `ak-…`, `AKIA…`, `ghp_…`);
* the README stops telling operators to change the default password.

Test fixtures must use obviously invalid values such as
`sk-test-not-a-real-key`, never a real provider key.

## 6. Known, accepted risks

* **The seeded administrator password is public.** `admin` /
  `Onti@2026` ships in the documentation for first-run convenience.
  Change it immediately after deploying, or pre-provision accounts and
  disable the seed login. The first sign-in also forces an e-mail binding,
  so the account cannot be used without an address somebody controls.
* **Registration is open by default.** Close it in Users & audit when
  the instance is not a demo.
* **Mail delivery is operator-supplied.** With Resend the message leaves
  through a third party; with SMTP it leaves through your relay. Treat the
  provider account as production infrastructure, and keep
  `ONTI_MAIL_ECHO` off outside local development — it returns the code in
  the HTTP response.
* **Serverless instances do not share a filesystem.** Accounts and
  verification challenges are mirrored to the durable store (Vercel Blob
  or your HTTP endpoint), so they survive cold starts; uploads and reports
  still rely on the whole-database snapshot. Without a store,
  `/api/auth/status` reports `accounts_persist: false` and self-registration
  is refused. Use the Docker deployment for guaranteed durability.
