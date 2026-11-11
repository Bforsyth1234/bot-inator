# Releasing a signed, notarized build

This is the prep for #96 in `todo.md`. The script builds the menu bar app,
signs it with your Developer ID, has Apple notarize it, and staples the
ticket so the download opens without a Gatekeeper warning.

You hold the certificate and the notarization credentials. Nothing in this
repo stores them.

> **Not done yet (rest of #96):** the release app is still only the menu bar
> app. It does not bundle Python or start the daemon, so a person without a
> terminal can't use it yet. Until that lands, a release is for people who
> also run `./install.sh` and `./run.sh`.

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
2. signs it with the hardened runtime and
   `scripts/release/MenuBarUI.entitlements` (microphone, calendars);
3. zips it, submits it with `notarytool --wait`, staples the ticket and
   re-zips;
4. checks it with `spctl --assess` and prints the zip's SHA-256.

Without `DEVELOPER_ID` it stops after an unsigned build, which is a quick
way to check the build itself.

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
