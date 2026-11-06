# Reply to the GitGuardian incident notification

> Incident: `7cb0d388-2ea7-4754-8e90-086b6972db61`
> Repository: `github.com/samzli/ontology`
> Use this text to answer the security team once the provider keys
> have been revoked. Replace every `<…>` placeholder before sending.

---

## Subject

`[Resolved] Exposed credentials in samzli/ontology — incident 7cb0d388`

## Body

Hello,

Thank you for the notification. We have completed the remediation for
incident `7cb0d388-2ea7-4754-8e90-086b6972db61` and would like to
request a re-scan and closure.

What was exposed.

1. A plaintext SiliconFlow API key and a plaintext Deloitte DNova API
   key, hard-coded in a test fixture
   (`tests/test_env_and_crud.py`).
2. The application master key (`data/.onti.key`). That key encrypted
   the model credentials stored in the bundled SQLite database and also
   signed session cookies, so it was treated as the higher-severity
   item.

Remediation completed.

- Both provider keys were revoked at the provider and re-issued on
  `<revocation date and time, timezone>`; the old values are no longer
  accepted by SiliconFlow or Deloitte DNova. New keys exist only in
  the deployment environment and in the encrypted model configuration.
- The master key was rotated. A new 32-byte value now lives in the
  `ONTI_SECRET_KEY` environment variable of the deployment; the key
  file is no longer part of the repository and is generated locally on
  first start. All sessions issued under the old key are invalid.
- The credentials were removed from the working tree and replaced with
  obviously invalid fixtures, and the paths are covered by
  `.gitignore`.
- History was rewritten with `git filter-repo`: the key file was
  removed from every commit and both credential strings were replaced
  throughout. All branches and tags were force-pushed.
  `git log --all -S "<value>"` and
  `git log --all -- data/.onti.key` both return no commits.
- The repository has been made private.
- A regression test (`tests/verify_secrets.py`) now runs in the normal
  suite and fails if a tracked file contains a credential-shaped
  string, if the key file becomes tracked again, or if the bundled
  database carries an encrypted secret or a live session.

Current state.

- The deployed instance reports no decryptable model credential until
  the operator enters the newly issued key in the admin Model centre,
  which is the expected state after rotation.
- No secret has been committed since the rotation.

Please let us know if the scan still reports the finding, and whether
you need the rewritten commit range or any further evidence for your
records.

Best regards,
`<name>`
`<role>`, `<organisation>`
`<contact email>`
