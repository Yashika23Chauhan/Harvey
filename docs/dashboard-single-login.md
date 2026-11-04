# Dashboard: single login

The dashboard has one account. The login screen shows only **Sign in**; there is
no "Create account" option.

## How it works

- **First run:** when no account exists yet, the dashboard shows a one-time
  setup form to create the account (username + password).
- **After that:** only the Sign in form is shown. The "Create an account" and
  "Already have an account? Sign in" links are removed.
- **Server-side:** `POST /auth/signup` refuses any new account once one exists
  (`400`, "This dashboard already has an account. Sign in instead."), so hiding
  the link is not the only protection.
- `GET /auth/status` reports `signup_open: false` once an account exists.
- `HARVEY_DASHBOARD_SIGNUP=closed` still closes sign-up entirely, even on first run.

## Files changed

| File | Change |
|---|---|
| `harvey/dashboard_auth.py` | `UserStore.signup` refuses when any account exists (checked under the store lock) |
| `harvey/dashboard.py` | `/auth/status` returns `signup_open` only when no account exists |
| `harvey/web/app.js` | Removed the sign-in / create-account switch link |
| `harvey/web/index.html` | Removed the `auth-switch` element |
| `harvey/web/app.css` | Removed the `.auth-switch` styles |
| `tests/test_dashboard_login.py` | A second sign-up is refused, whatever the username |

## Accounts created before this change

Accounts are stored in `data/dashboard-users.json`. Any accounts that existed
before this change can still sign in. To keep only one, remove the extra
entries from the `users` list in that file.

## Resetting the login

To start over (for example, a forgotten password), stop the dashboard, delete
`data/dashboard-users.json`, and reload the page: the first-run setup form
appears again.
