# BUG — Desktop note opening blocked by unavailable Obsidian CLI

Date: 2026-10-07
Status: desktop opening restored through URI fallback; ten target tabs verified.
Severity: low — desktop note opening is blocked through the selected CLI route.
Tracker publication: pending; no remote issue was created by this report-only request.

## User impact

The operator requested opening ten existing frontmatter-only notes in Obsidian
without modifying their contents. The agent selected the desktop `obsidian`
command, but that command was unavailable. None of the ten notes was opened
through this route.

This is not evidence of a defect in the OSM MCP server or its `osm` executable.
The desktop `obsidian` command and the project's `osm` command are different
interfaces. Note-reading MCP tools do not prove that desktop tabs were opened.

## Reproduction and observed evidence

Read-only checks were repeated twice on the operator's Mac:

```sh
command -v obsidian
/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' /Applications/Obsidian.app/Contents/Info.plist
pgrep -x Obsidian
```

- `command -v obsidian` returned no path and exited nonzero.
- The installed application version was `1.11.7` on both checks.
- `pgrep -x Obsidian` returned no matching process on both checks.

An attempted `obsidian help open` failed with:

```text
zsh:3: command not found: obsidian
```

Additional inspection found the desktop application and its executable, but no
`obsidian` entry at the three checked locations: `/usr/local/bin/obsidian`,
`/opt/homebrew/bin/obsidian`, and the operator's local bin directory. This is
not an exhaustive search of all possible installation locations.

## Interpretation and limits

The installed agent skill documents the desktop CLI as requiring Obsidian
1.12.7 or newer, with CLI support enabled in the application's settings. The
observed installation is older than that documented prerequisite. That version
requirement was read from the skill, not independently verified against the
current vendor documentation during this report.

No GUI settings were inspected. The report does not claim the application is
missing, that its CLI toggle is disabled, or that the OSM server is broken.
The direct observed blocker is the missing command in the current shell.

## Proposed remediation

1. Prefer a documented, non-mutating `obsidian://` desktop-opening route when
   the optional desktop CLI is unavailable. Validate the vault and exact file
   path; URL-encode parameters and do not create notes on a lookup failure.
2. If CLI support is desired, verify vendor prerequisites, update the desktop
   application with operator approval, enable CLI support, and verify command
   discovery. Do not change MCP registrations just to address desktop opening.
3. Make the preflight distinguish application availability, command discovery,
   CLI support, and a running desktop instance. Report the specific failed
   prerequisite instead of saying that Obsidian itself is absent.

## Operational repair — 2026-10-07

The vendor documents `obsidian://open?path=...&paneType=tab`: `path` accepts an
absolute file path and selects the registered vault; `paneType=tab` opens a new
tab. Encode the entire parameter value, including `/`, spaces, Unicode, `&`
and `?`. Source: [Obsidian URI documentation](https://help.obsidian.md/Extending+Obsidian/Obsidian+URI).

Read-only local inspection found the registered vault
`/Users/airm2max/Documents/OBSIDIAN_ICLOUD/coredev` in
`~/Library/Application Support/obsidian/obsidian.json`. The application's
`CFBundleURLTypes` declares the `obsidian` scheme. These are configuration
observations, not proof that a desktop tab opened.

Use this preflight for each exact requested note before dispatching any URI:

1. Confirm `/Applications/Obsidian.app` exists. Check `command -v obsidian`
   independently; a missing command selects the URI route and does not imply
   that the application is absent. Check `pgrep -x Obsidian` independently;
   an absent process means the URI will launch the application.
2. Resolve the supplied file path strictly, require an existing regular file
   inside the registered vault, and record its SHA-256. Validate every target
   before opening the first. Reject lookup failures; never use `new`, `append`
   or `prepend` actions. Filenames containing `#` require separate validation
   because Obsidian interprets that character as a heading reference.
3. Build the URI with Python's `urllib.parse.urlencode`, using
   `quote_via=urllib.parse.quote` and `safe=""` for the absolute `path` value and
   `paneType=tab`. Dispatch with an argument list:
   `subprocess.run(["/usr/bin/open", "-a", "/Applications/Obsidian.app", uri], check=True, timeout=10)`.
   Do not interpolate a filename into shell command text.
4. Verify the intended note's tab in the desktop separately, then compare its
   SHA-256 with the preflight value. Successful URI dispatch alone does not
   prove tab selection or unchanged file contents. If the CLI becomes
   available, inspect its own `help open` before choosing flags; an execution
   failure must be reported rather than treated as successful opening.

The owner subsequently supplied ten relative note paths. Live preflight stopped
at the first target because `/Users/airm2max/Documents/OBSIDIAN_ICLOUD/coredev/10_ai`
does not exist (`FileNotFoundError`). The relative-path base must be confirmed
before opening any target; no arbitrary notes were substituted. No URI opening,
application update, settings change, or note modification was performed.
The owner confirmed that the supplied paths are relative to `coredev/notes/`.
All ten existing files were strictly resolved there before dispatch; repeated
SHA-256 checks were stable. Ten `obsidian://open` URIs with encoded absolute
paths and `paneType=tab` were dispatched successfully using `/usr/bin/open`.
Two subsequent checks confirmed Obsidian running, all ten exact paths present
as Markdown tabs in `.obsidian/workspace.json`, and every target's SHA-256
unchanged. Saved workspace state confirms the requested tabs; no screenshot
inspection was performed. The desktop CLI remains unavailable.

No OSM source change is needed: the unavailable command belongs to the agent's
desktop workflow.

## Acceptance checks for a future fix

### Persistent agent workflow patch

The active plugin's `obsidian-cli/SKILL.md` now documents the URI fallback.
A self-contained local `obsidian-desktop-opening` skill is installed at
`~/.codex/skills/obsidian-desktop-opening/SKILL.md` so the opening workflow
survives replacement of the plugin cache. Both skill validators passed; an
independent read-only scenario confirmed that missing relative paths stop
dispatch rather than silently inserting `notes/`. No OSM source change,
commit, push, or application upgrade was required.

A later recheck could not repeat the earlier ten-note verification:
`coredev/notes/10_ai/learning/llm_fundamentals/llm.md` no longer resolved.
The earlier successful tab and hash checks describe their observation time;
future opening must validate the current paths again.

- With no desktop CLI, an existing note can be opened through the supported
  fallback and its bytes remain unchanged.
- File paths containing spaces, Unicode, or URL-reserved characters resolve to
  the intended note; missing files do not create new notes.
- With CLI support available, the preflight selects the supported command and
  reports failures accurately. Desktop opening is verified separately from
  MCP content retrieval.

No implementation or test suite execution was part of this report.
