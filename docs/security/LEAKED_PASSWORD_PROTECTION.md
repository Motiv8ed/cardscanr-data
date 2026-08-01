# Enable leaked-password protection (owner step)

Supabase Security Advisor warning: **Leaked Password Protection Disabled**.

This is an Auth dashboard setting, not a SQL migration. Do **not** change
existing user passwords or authentication providers as part of the advisor
remediation.

## Exact owner steps

1. Open the hosted project Auth providers page:
   `https://supabase.com/dashboard/project/qstcdlczasmvexpgbpjk/auth/providers?provider=Email`
2. Under **Email** password settings, enable **Prevent use of leaked passwords**
   (HaveIBeenPwned.org Pwned Passwords).
3. Keep existing minimum-length / required-character policy unless intentionally
   changing password strength in the same review.
4. Save Auth settings.
5. Re-run Security Advisor and confirm `auth_leaked_password_protection` is clear.

## Notes

- Available on the Supabase **Pro Plan and above**.
- Existing users can still sign in with current passwords; weak/leaked passwords
  are rejected on new signups and password changes per Supabase Auth behaviour.
- Docs: https://supabase.com/docs/guides/auth/password-security#password-strength-and-leaked-password-protection
