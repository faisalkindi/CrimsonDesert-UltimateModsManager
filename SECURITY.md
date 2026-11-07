# Security Policy

Thanks for helping keep CDUMM and its users safe.

## Supported versions

CDUMM ships frequent releases and updates itself in place, so security fixes
land in the **latest release only**. Always update to the newest
[release](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases/latest)
before reporting — older builds are not patched separately.

| Version | Supported |
| ------- | --------- |
| Latest release | ✅ |
| Anything older | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for security problems.** Public issues are
visible to everyone and can expose users before a fix is out.

Instead, use GitHub's private reporting:

1. Go to the repository's **Security** tab → **Report a vulnerability**
   ([Privately reporting a security vulnerability](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).
2. Describe the issue with enough detail to reproduce it (see below).

If private reporting is not enabled, open a minimal issue that says only *"I'd
like to report a security issue privately"* — without details — and wait for a
private channel. Don't post the specifics publicly.

### What to include

- The CDUMM version and your OS.
- A clear description of the vulnerability and its impact.
- Step-by-step reproduction (and a proof-of-concept if you have one).
- Any relevant logs, with secrets such as your Nexus API key redacted.

### What to expect

CDUMM is maintained by a small team. Reports are handled on a best-effort
basis — expect an initial acknowledgement within a few days, and please allow
reasonable time for a fix to ship before any public disclosure.

## Scope

CDUMM is a desktop application that, by design, touches several sensitive
areas. Reports in these areas are in scope:

- **Self-updater** — the download-and-swap flow that replaces the running
  executable (e.g. tampering, downgrade, or unsigned-payload concerns).
- **ASI loader injection** — the proxy DLL CDUMM installs into the game's
  `bin64` directory.
- **Mod import / apply** — handling of untrusted mod archives (`.zip`, `.7z`,
  `.rar`) and crafted game-table payloads (path traversal, zip-slip, archive
  bombs, memory-safety issues in parsers).
- **Nexus integration** — storage and handling of the Nexus API key and the
  `nxm://` download handler.
- **Local data** — the CDUMM database, snapshots, and backups under the game
  directory.
- **Bug report upload** — **Upload to PrivateBin**, which encrypts the report
  and uploads it to a PrivateBin server (see the note below), and the
  user-name scrubbing CDUMM applies to the bug report, whether it's copied,
  saved, or uploaded, and to what the other Copy Report buttons copy.

### Out of scope

- The Crimson Desert game itself, its anti-cheat, or any publisher service.
- Third-party mods and their content. CDUMM applies mods; it does not vouch
  for them.
- Nexus Mods, Steam, Epic, Xbox, or other external platforms.
- Issues that require an already-compromised machine or physical access.

## A note for users

CDUMM modifies local game files through an overlay and installs an ASI loader.
Only download CDUMM from the official
[Releases](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/releases)
page, and only install mods from sources you trust.

### Bug report uploads

**Upload to PrivateBin**, on CDUMM's Bug Report page, uploads your report to a
third-party paste service: the public PrivateBin instance at
`https://privatebin.net/`, unless you set another one under **Settings → Bug
Report Uploads (PrivateBin)**. CDUMM shows a privacy notice before the first
upload.

- The report is encrypted on your machine (AES-256-GCM, by the `privatebin`
  package) and the key is the part of the link after `#`, so the server
  can't read it, though like any website it sees your IP address. Anyone
  with the full link can read it until the paste expires: one week by
  default, adjustable in the same settings.
- CDUMM then copies the link and tells you to share it on NexusMods or
  Discord. For GitHub, it points you to **Report on GitHub** instead,
  because the issue forms need the report itself.
- CDUMM doesn't keep the paste's delete token, so it can't remove an upload
  before it expires.
- Before uploading, CDUMM replaces the user name with `USER` in paths, in
  two steps:
  - First the name of your own user folder, matched whole, wherever it
    comes after `Users` or `home` and a slash: `C:\Users\<name>` with any
    drive letter and with `\`, `/`, or the doubled backslashes a Python
    error in the log uses when it quotes a path (`C:\\Users\\<name>\\`),
    macOS `/Users/<name>`, Linux `/home/<name>`, and Wine's
    `Z:\home\<name>`. A name with an apostrophe, like O'Brien, goes in full.
  - Then any other `C:\Users\<name>` path, in the same forms. Here the name
    is everything after `Users\` up to the next `\`, `/`, `:`, `;`, `,`,
    `[`, `]`, `@`, `'`, `"`, `<`, `>`, `|`, `*`, `?`, line break, or the
    drive letter of the next path. So where a path stops at the name, the
    text after it is replaced too, up to the next of those:
    `Home: C:\Users\<name> (default profile)` becomes `Home: C:\Users\USER`.

  The rest of the text isn't changed. If the name has non-ASCII characters,
  the report says so in a `User name: has non-ASCII characters` line.
- Other people's names can stay: someone else's macOS `/Users/<name>/` or
  Linux `/home/<name>/`, and the part after the apostrophe in someone
  else's `C:\Users\O'Brien\`, which becomes `C:\Users\USER'Brien\`.
- The preview, **Report on GitHub**, **Copy to Clipboard**, and **Save as
  File** all use the same scrubbed text, so the preview shows what each
  button shares. Report on GitHub sends GitHub your description, scrubbed
  the same way, and a title made from it, in the link it opens. Nothing is
  posted until you submit the form. Copy and Save upload nothing. The
  **Copy Report** buttons elsewhere in CDUMM (the failed-imports and
  diagnostic dialogs, Find Culprit, and Inspect) scrub what they copy the
  same way, though their windows still show your own paths.
