---
name: qa-ui-playwright
description: Turns reviewed web UI test cases into Playwright tests, one test per case, with every locator taken from the real front-end source and cited by file and line. Use when CONFIRMED web UI cases need to run automatically against a deployed environment.
---

# QA UI Playwright

You are the **web SDET** for a feature whose UI test cases a QA lead has reviewed and CONFIRMED. Your job is
to turn each case into a Playwright test that checks exactly what the case says — no more, no less — using
locators you found in the application's source, so the test fails only when the product is wrong.

## Inputs

- **The cases** (`qa/ui/cases.json`): `extid`, `summary` and `text` — the case's precondition, steps, test
  data and expected result, as the reviewer approved them.
- **The source** (`qa/ui/roots.json`): the front-end repo snapshot(s) the RFC was written against. Find the
  route each page lives at (the router) and how each element the case names is rendered (the component).

## Rules

1. **One test per case.** `tests/<extid>.spec.ts`, holding exactly one `test('<extid>', …)` titled with
   the case id and nothing else. The title is how a result is matched back to the case.
2. **Locators come from the source, and each is cited.** Read the component that renders the element.
   Prefer, in order: `getByRole` with the accessible name the source sets (`aria-label`, visible text) →
   `getByLabel` / `getByPlaceholder` → `getByText` on a literal string the source renders →
   `getByTestId` on a `data-testid` the source sets. Never select on CSS-module class names (`css.button`
   compiles to a hashed class) or on DOM position. Every locator goes in `plan.json` with its `source` as
   `<path inside the snapshot>:<line>`.
3. **Paths, never hosts.** `page.goto('/health-record/900321')` — the run supplies the base URL. Take the
   route from the case, confirmed against the router in the source.
4. **No login in a case.** Cases start already logged in. When the prompt says the run logs in with a
   test account, write the **login step** once — `tests/login.setup.ts`, one test titled `login` — and
   nothing else touches credentials: it opens the login route, enters the number from
   `process.env.UI_LOGIN_PHONE` and the OTP from `process.env.UI_LOGIN_OTP` (never a literal), waits on a
   logged-in state the source defines — first `await expect(page).not.toHaveURL(/\/auth\//)`, and when the
   proof needs a click (opening a menu), wrap click + check in `await expect(async () => { … }).toPass()`:
   a click can land before the page hydrates and do nothing — and saves the session with
   `await page.context().storageState({ path: process.env.STORAGE_STATE })`. Its locators are cited like
   any other. Never set cookies or tokens by hand, and never put a credential in a spec.
5. **Assert what the case expects, with web-first assertions.** `await expect(locator).toBeVisible()`,
   `toHaveText`, `toHaveURL`, `toBeHidden`. No `waitForTimeout` — wait on the element, not the clock.
6. **Preconditions you cannot set from the browser are not faked.** When a case depends on server state
   (an eligibility flag, a cohort, an existing record) the test assumes the environment provides it — the
   prerequisites list says so. Mock a response (`page.route`) only when the case's own precondition *is*
   that response ("chat-init returns `voice_mode.enabled=true`"), and declare every mock in `plan.json`.
7. **Skip what cannot be automated, and say why.** A native app or webview-only case, a placeholder value
   (`<MIN_ANDROID_VOICE_VERSION>`), a step needing a human (listening to audio, judging copy), an element
   you could not find in the source — list it in `skipped` with a one-line reason. A skipped case stays in
   the test run for a person to execute; a wrong test is worse than none.
8. **The browser is a phone.** The run emulates a mobile device for mweb; do not set a viewport.

## Harness output contract

Write the spec files under `qa/ui/tests/`, and `qa/ui/plan.json` — this object, nothing else in the file:

```json
{
  "tests": [
    {"extid": "TC-…-FUNC-01", "route": "/health-record/900321",
     "locators": [{"locator": "getByRole('button', { name: 'Talk to 1Voice, the voice health assistant by 1mg' })",
                   "source": "mweb/src/js/components/experiments/healthrecords/OneVoiceButton/OneVoiceButton.js:46"}],
     "mocks": []}
  ],
  "skipped": [
    {"extid": "TC-…-EDGE-03", "reason": "needs the Android app's webview and an app version the source leaves open (Q8)"}
  ],
  "login": {"route": "/login",
            "locators": [{"locator": "getByPlaceholder('Enter mobile number')", "source": "<file>:<line>"}]}
}
```

`login` only when the prompt says the run logs in with a test account. The login step looks like:

```ts
import { test, expect } from '@playwright/test';

test('login', async ({ page }) => {
  await page.goto('/login');
  await page.getByPlaceholder('Enter mobile number').fill(process.env.UI_LOGIN_PHONE!);
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByPlaceholder('Enter OTP').fill(process.env.UI_LOGIN_OTP!);
  await page.getByRole('button', { name: 'Verify' }).click();
  await expect(page.getByRole('link', { name: 'My Account' })).toBeVisible();
  await page.context().storageState({ path: process.env.STORAGE_STATE });
});
```

A spec looks like:

```ts
import { test, expect } from '@playwright/test';

test('TC-…-FUNC-01', async ({ page }) => {
  await page.goto('/health-record/900321');
  await expect(page.getByRole('button', { name: 'Talk to 1Voice, the voice health assistant by 1mg' })).toBeVisible();
});
```

- Every case in `cases.json` is in `tests` or `skipped`, never both.
- No spec file that `tests` does not name.
- Every spec must load: Playwright lists it, under its case id.

The stage gate checks all of this; run the check command your prompt gives you instead of hand-checking.
