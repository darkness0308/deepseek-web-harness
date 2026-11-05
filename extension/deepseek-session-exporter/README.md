# DeepSeek Session Connector

A tiny Manifest V3 extension (Chrome, Edge, Brave) that hands your **already signed-in** DeepSeek session to the
local harness with one click, so nothing has to log in through an automated browser (which the AWS WAF captcha blocks).

Why an extension: a cookie export does **not** contain the login token. DeepSeek keeps it in
`localStorage.userToken`; only code running in the signed-in browser can read it.

> **Educational use only.** This extension hands your own signed-in session to your own local program. Using DeepSeek's website from programs may violate its Terms of Service and can get the account blocked or suspended; see the main README.

## Install once

1. Open `chrome://extensions` (or `edge://extensions`) and turn on **Developer mode**.
2. **Load unpacked** and pick this folder (`extension/deepseek-session-exporter`). If an older version is installed
   (it asked for a port and an API key), **remove it first** - this version has a new, fixed identity.

## Use

1. Sign in at <https://chat.deepseek.com> in that browser.
2. In the harness (`python run.py`) type **`/connect`**. It waits (countdown, no `You >` prompt); **Esc** cancels.
3. Click the extension, then **Connect**. The harness prints `[connect] Connected`, uses the new login immediately and shows `You >` again.

No port, no API key. On a shared computer type `/connect code` instead and enter the 6-digit code it shows.
Harness not running? **Save a file instead** downloads `deepseek-session.json`; move it into `cookies/`, then `/refresh`.

## What it touches

* Reads `localStorage.userToken` of the open chat.deepseek.com tab and the `deepseek.com` cookies, only when you press Connect.
* May contact only `chat.deepseek.com` pages it reads and `http://127.0.0.1:47123` (declared in `manifest.json`).
  The listener there exists only after `/connect`, serves only this extension (its ID is pinned by the `key` in the
  manifest) and closes after one successful connection or 2 minutes.
* No background code, no storage, no analytics. The token is never displayed.
* The saved file is a live login: delete it after importing. `cookies/` is git-ignored.
