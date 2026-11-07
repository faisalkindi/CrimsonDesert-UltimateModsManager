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
- **Bug report upload** — **Submit Bug Report**, which encrypts the report
  and uploads it to a PrivateBin server (see the note below), and the
  user-name scrubbing done before the upload.

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

**Submit Bug Report**, on CDUMM's Bug Report page, uploads your report to a
third-party paste service: the public PrivateBin instance at
`https://privatebin.net/`, unless you set another one under **Settings → Bug
Report Uploads (PrivateBin)**. CDUMM shows a privacy notice before the first
upload.

- The report is encrypted on your machine (AES-256-GCM, by the `privatebin`
  package) and the key is the part of the link after `#`, so the server
  can't read it, though like any website it sees your IP address. Anyone
  with the full link can read it until the paste expires: one week by
  default, adjustable in the same settings.
- CDUMM then copies the link and opens GitHub's new-issue page. A link
  pasted into an issue is public.
- CDUMM doesn't keep the paste's delete token, so it can't remove an upload
  before it expires.
- Before uploading, CDUMM replaces the user name with `USER` in paths
  written as `C:\Users\<name>\` (any drive letter, `\` or `/`). Paths
  written any other way keep it: the doubled backslashes of a Python error
  quoted from the log (`C:\\Users\\<name>\\`), a path that ends at the user
  name, and macOS `/Users/<name>/` or Linux `/home/<name>/` paths. The rest
  of the text isn't changed.
- **Copy to Clipboard** and **Save as File** upload nothing, but they don't
  hide your user name either.
