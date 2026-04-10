---
name: security_review
triggers: [security, vulnerability, injection, XSS, CSRF, auth, credentials, secrets, CVE]
roles: [reviewer, analyst]
---
# Skill: Security Review

## When to Use
Apply when reviewing authentication, authorisation, file/network I/O, user-input handling, subprocess execution, or cryptography code. Also apply when asked to check for vulnerabilities.

## Strategy
Security bugs are correctness bugs with exploitability. Review with adversarial intent: assume the caller is hostile.

## Execution Steps

1. **Identify the trust boundary.**
   Where does untrusted data enter? (HTTP request body, environment variable, file contents, CLI args, IPC). Mark every entry point.

2. **Injection checks.**
   - **Shell injection**: Is user data ever passed to `shell=True`, `os.system()`, or string-interpolated into a shell command? Fix: use `subprocess.run([...], shell=False)` with explicit argument lists.
   - **SQL injection**: Is user data concatenated into a query string? Fix: use parameterised queries.
   - **Path traversal**: Is user data used to construct a file path without `Path.resolve()` + allowlist check?
   - **Template injection**: Is user data rendered into a template without escaping?

3. **Authentication & authorisation.**
   - Are all state-changing endpoints protected by auth checks?
   - Are auth tokens validated (signature, expiry, scope) before use?
   - Is there a check that the authenticated user is authorised to access the requested resource (not just authenticated)?

4. **Secrets management.**
   - Are secrets/API keys stored in environment variables or a secrets manager — never in source code or config files committed to VCS?
   - Are secrets logged anywhere (even at DEBUG level)? Fix: mask or omit from log calls.
   - Are secrets compared with `hmac.compare_digest()` to prevent timing attacks?

5. **Cryptography.**
   - Is a modern, well-audited library used (e.g. `cryptography`, `PyNaCl`)? Never roll your own crypto.
   - Is a secure random source used (`secrets`, `os.urandom`)? Never `random.random()` for security.
   - Are passwords hashed with a proper KDF (bcrypt, argon2, scrypt)? Never `MD5` or raw `SHA-*`.

6. **Dependency hygiene.**
   - Are pinned versions used in requirements?
   - Are known-vulnerable packages present? (Check with `pip-audit` or equivalent.)

7. **Output encoding.**
   - Is user-supplied data escaped before being rendered in HTML/XML/JSON responses?

8. **Report findings.**
   - Severity: CRITICAL (exploitable remotely, data loss/takeover) > HIGH (local privilege escalation, data exposure) > MEDIUM (requires unusual conditions) > LOW (hardening).
   - For each finding: location, attack scenario, recommended fix.
