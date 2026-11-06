# Security — secret handling and incident response

> Audience: whoever operates or deploys Onti.
> See [../README.md](../README.md) for setup and
> [TECHNICAL.md](TECHNICAL.md) for architecture.

---

## 1. Secrets this application holds

| Secret          | Where it lives                  | Why it matters                |
| --------------- | ------------------------------- | ----------------------------- |
| Master key      | `.onti.key` / `ONTI_SECRET_KEY` | Signs cookies, encrypts keys  |
| Model API keys  | `model_configs`                 | Billable provider credentials |
| Session cookies | Browser only                    | Full access, including admin  |
| User passwords  | `users` (PBKDF2 hash)           | Account access                |

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

## 4. What the guard test enforces

`tests/verify_secrets.py` runs in the normal suite and fails when:

* `data/.onti.key` is tracked, or `.gitignore` stops covering it;
* the bundled `data/onti.db` carries any encrypted model secret or a
  live session;
* a tracked text file contains a credential-shaped string
  (`sk-…`, `ak-…`, `AKIA…`, `ghp_…`);
* the README stops telling operators to change the default password.

Test fixtures must use obviously invalid values such as
`sk-test-not-a-real-key`, never a real provider key.

## 5. Known, accepted risks

* **The seeded administrator password is public.** `admin` /
  `Onti@2026` ships in the documentation for first-run convenience.
  Change it immediately after deploying, or pre-provision accounts and
  disable the seed login.
* **Registration is open by default.** Close it in Users & audit when
  the instance is not a demo.
* **Serverless instances do not share storage.** Accounts registered at
  runtime live on one instance only; see `README.md` → Deploying to
  Vercel. Use the Docker deployment for durable multi-user state.
