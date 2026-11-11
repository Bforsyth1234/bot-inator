# Releasing a signed, notarized build

This is the prep for #96 in `todo.md`. The script builds the menu bar app,
signs it with your Developer ID, has Apple notarize it, and staples the
ticket so the download opens without a Gatekeeper warning.

You hold the certificate and the notarization credentials. Nothing in this
repo stores them.

The release app carries its own daemon: a relocatable Python with the
daemon's packages and sources inside `bot-inator.app`, started by the app
when nothing else answers on port 8000 and stopped when it quits. A person
who never opens a terminal downloads, unzips and opens it. (A daemon started
with `./run.sh` still wins: the app leaves it alone.)

## One-time setup

1. **Join the Apple Developer Program** (99 USD a year) at
   <https://developer.apple.com/programs/>.
2. **Create a Developer ID Application certificate.** In Xcode: Settings →
   Accounts → your team → Manage Certificates → **+** → *Developer ID
   Application*. Or create it at developer.apple.com → Certificates and
   import it into your login keychain.
   Check that it's there, and copy its full name:

   ```bash
   security find-identity -v -p codesigning
   ```

   It looks like `Developer ID Application: Your Name (ABCDE12345)`.
3. **Store notarization credentials in your keychain.** Make an
   app-specific password at <https://account.apple.com> (Sign-In and
   Security → App-Specific Passwords), then:

   ```bash
   xcrun notarytool store-credentials bot-inator-notary \
     --apple-id you@example.com --team-id ABCDE12345
   ```

   It prompts for the app-specific password and saves it under the profile
   name `bot-inator-notary`.

## Each release

From a clean checkout of the commit you're releasing:

```bash
DEVELOPER_ID="Developer ID Application: Your Name (ABCDE12345)" \
NOTARY_PROFILE=bot-inator-notary \
VERSION=0.2.0 \
scripts/release/build-release.sh
```

The script:

1. builds `dist/bot-inator.app` with `swiftc -O` and stamps `VERSION` and
   the build number (the commit count) into its Info.plist;
2. runs `scripts/release/bundle-daemon.sh`, which puts the daemon inside the
   app (see "What's in the bundle" below);
3. signs every Mach-O file inside-out with the hardened runtime (~280
   libraries, then the bundled Python with
   `scripts/release/Python.entitlements`), then the app with
   `scripts/release/MenuBarUI.entitlements` (microphone, calendars), and
   runs `codesign --verify --deep --strict`;
4. runs `scripts/release/smoke-test.sh`: the *signed* Python imports MLX,
   PyObjC, torch and sqlite-vec, and the daemon starts on a free port with a
   throwaway home and answers `/health` (this is where a library-validation
   or entitlement problem would show, before you spend a notarization);
5. zips it, submits it with `notarytool --wait`, staples the ticket and
   re-zips;
6. checks it with `spctl --assess` and prints the zip's SHA-256.

Without `DEVELOPER_ID` it signs ad hoc and runs steps 1–4, then stops: a
full local check of the bundle without your certificate. `BUNDLE_DAEMON=0`
builds the menu bar app alone (the old behaviour); `SMOKE_TEST=0` skips
step 4.

The first run downloads CPython (27 MB, pinned by SHA-256) into
`dist/cache/` and the daemon's wheels (pip's cache); later runs take about a
minute. Building needs the Xcode command line tools (watchdog builds from
source for Python 3.14); the person installing the app needs nothing.

## What's in the bundle

| Path in `bot-inator.app/Contents` | What | Size |
| :--- | :--- | :--- |
| `MacOS/MenuBarUI` | the menu bar app | 7.5 MB |
| `Resources/python/` | CPython 3.14.10 from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) (`20261026`, `install_only_stripped`) plus `agent-daemon/requirements.txt` minus browser-use and playwright (nothing imports them); `.pyc` compiled at build time | 1.5 GB (torch alone is 634 MB) |
| `Resources/agent-daemon/` | the daemon's tracked sources, no tests, evals or `.env` | 4 MB |

The zip is about 480 MB. Models are not in it: with the local provider they
download on first start, as with `./run.sh`.

When the app starts its daemon it runs
`Resources/python/bin/python3 -I -B -m uvicorn main:app --app-dir Resources/agent-daemon --host 127.0.0.1 --port 8000`
with `AGENT_DATA_DIR=~/Library/Application Support/bot-inator`. The bundle
is never written to: the generated tools (and an optional `.env`) live in
that folder, everything else stays in `~/.bot-inator` as before, and the log
is `~/Library/Logs/bot-inator/daemon.log` (Settings → App → Show log).

### Entitlements

- **The app** (`MenuBarUI.entitlements`): microphone and calendars, as
  before.
- **The bundled Python** (`Python.entitlements`): calendars only, for the
  daemon's own EventKit backend (`tools/calendar_access.py`,
  `tools/reminders.py`). Tested under the hardened runtime: ctypes
  callbacks, PyObjC blocks, `mx.compile` (GPU and CPU), torch and sqlite-vec
  need neither `allow-jit` nor `allow-unsigned-executable-memory`. Library
  validation stays on: every library is in the bundle and signed with your
  Team ID, and Python runs with `-I`, so nothing from outside the bundle is
  loaded. The smoke test (step 4) proves this with your signature.
- **Ad-hoc builds only** (`Python-adhoc.entitlements`): also
  `disable-library-validation`, because ad-hoc code has no Team ID and the
  hardened runtime then refuses the bundle's own `.so` files.

If the smoke test fails on a Developer ID build with "different Team IDs"
or "not valid for use in process", a library came in unsigned or signed by
someone else: re-run the build from a clean `dist/`. Don't add
`disable-library-validation` to `Python.entitlements` without finding out
which library it is.

Then attach `dist/bot-inator-<VERSION>.zip` to a GitHub release:

```bash
gh release create v0.2.0 dist/bot-inator-0.2.0.zip --title "bot-inator 0.2.0" --notes "…"
```

## If something fails

- **"no signing identity"**: the name in `DEVELOPER_ID` must match
  `security find-identity -v -p codesigning` exactly.
- **Notarization "Invalid"**: get the reason with
  `xcrun notarytool log <submission-id> --keychain-profile bot-inator-notary`.
- **Permissions after switching builds**: a Developer ID build is a
  different code identity from the ad-hoc `./install.sh` build, so macOS
  asks again for Calendar, Microphone and the rest the first time.
- **The app shows "the assistant couldn't start"**: Settings → App → Show
  log. "address already in use" means something else holds port 8000.
- **"git isn't installed"** after installing a generated tool: on a Mac
  without the Xcode command line tools the daemon skips the tool's version
  history instead of running the `/usr/bin/git` stub (which would pop up
  "install developer tools"). The tool still works; run
  `xcode-select --install` to get history back.
