# wanjuan-editor

A local, document-centred editorial workbench. Upload or paste a manuscript, run a rule-based review over it, keep every save as an immutable version, gate approval on a human, and derive a structured video plan from an approved piece.

## What it does / why

Editorial work has a shape: draft, review, revise, approve, and only then commission anything downstream. Tools usually implement the drafting and lose the rest — no version that survives, no record of who approved what against which text, nothing stopping a downstream task from being created against a draft that has since changed.

This keeps that chain intact, and it runs entirely on your own machine.

- **Two front doors.** A newsroom review flow, and a writing assistant. The card machinery behind both stays internal.
- **Immutable versions.** Every valid save creates a new version; the original hash is never rewritten. Restoring an old version creates a *new* version rather than deleting history. Any two versions can be diffed.
- **Rule-based review.** A local pass over high-risk stock phrases, repeated punctuation, formatting and over-long paragraphs. Results are stored with the cards, templates, personas and input hash used for that run.
- **Source-claim adjudication.** Claims extracted from attachment filenames, in-text URLs and byline lines are marked confirmed, doubtful, or a gap. A piece with no sources cannot unlock the flow with invented claims.
- **An approval that expires.** Human approval binds to the current revision, the current review record and a full snapshot of the source adjudication. Change any of them and the approval invalidates itself automatically.
- **A video plan, only after approval.** 30/60/90-second and long-form skeletons, interview and camera tasks, risk gaps and next steps. It is always labelled a draft and never dispatches work.
- **Stale-tab safety.** Every write compares against the version the front end was showing. An old tab gets a 409 rather than overwriting newer work.

## Requirements

- Python 3.10 or newer.
- `PyYAML` — `pip install -r requirements.txt`. That is the only dependency.
- **Optional, for the writer engine:** a `codex` CLI on `PATH`, signed in. `data/engine.json` defines the command; it runs read-only sandboxed with user config ignored. Without it the writer engine is unavailable and the rest of the application still works.
- **Optional, for the media pipeline:** `ffmpeg` and `ffprobe`, plus a local faster-whisper model snapshot. `FFMPEG_DIR` points at the binaries if they are not on `PATH`; `data/media_config.json` sets the model, device and language.

## Install and run

```powershell
git clone <repo-url> wanjuan-editor
cd wanjuan-editor
pip install -r requirements.txt
.\start.ps1
```

Or directly:

```
python app.py --host 127.0.0.1 --port 8765
```

Then open `http://127.0.0.1:8765`. The SQLite database is created under `data/` on first run.

### External corpora

Two optional read-only sources are configured by environment variable, and the application never modifies either:

| Variable | What it points at |
| --- | --- |
| `WENKU_BAIGUI_ROOT` | A card library directory containing an `INDEX.yaml` |
| `WENKU_CHINATIMES_ROOT` | A news-analysis corpus directory |

These were previously two hardcoded absolute paths that no longer existed even on the machine that wrote them. Unset, the features that need them report a missing source instead of silently reading nothing.

**The card library is not included and is not distributable.** `src/catalog.py` treats `INDEX.yaml` as the single source of truth for cards, templates and persona cards; on a machine without that library, the card and template features have nothing to load. That is the honest state of it — the counts in the original documentation (282 cards, 32 templates, 11 persona cards) describe one particular private library, not anything shipped here.

## Security boundaries — read before exposing this

The original project documented these plainly and they have not changed:

- **Binds `127.0.0.1` only by default.** Binding anywhere else requires setting `allow_external: true` *and* a password in `data/access.json`; without both, startup fails rather than opening quietly.
- **There is no HTTPS.** Exposed, everything is plaintext HTTP — the password and every manuscript can be read or altered by anyone on the path. Suitable for a LAN or a trusted overlay network. **Never port-forward this to the internet.**
- **One shared password.** No accounts, no MFA, no audit trail. Sessions live in memory and are lost on restart. Once past the login, a user has everything: editing, approval, and creating video-plan drafts.
- **`data/access.json` is gitignored, and must stay that way.** In the original working tree it held a plaintext shared password together with `allow_external: true` and a live tunnel hostname — and the project's own `.gitignore` covered only `data/*.db`, so it was not protected. The `.gitignore` here excludes everything under `data/` except the three shipped configuration files.
- The server does reject non-local `Host` headers, cross-site writes and non-JSON writes. That is hardening, not safety.

## Data

`data/` is gitignored except for three configuration files that ship with the application: `engine.json` (the writer engine command), `media_config.json` (whisper model and upload limit) and `writer_cards.json` (four writer presets). Everything else there — `studio.db`, uploaded media, `access.json`, temporary files — is yours and stays local.

## Tests

```
python -m pytest
```

85 pass, 2 skip. They cover the card adapter, manuscript immutability, review rules, the media server, the stale-revision contracts and the approval-invalidation logic.

## Limitations

- **Only the local rules engine is wired in by default.** Semantic review — source questioning, readability, interest, depth — is marked as awaiting a model, and the interface says so rather than pretending an AI editor approved something.
- **The video plan is a traceable skeleton**, permanently labelled a draft, and never dispatches work.
- **A human can finalise a piece before semantic review exists.** The interface and the risk list make that visible; nothing lets it be passed off as having been reviewed.
- **The interface, review rules and all documentation are Traditional Chinese.** The rule-based review is written for Chinese prose.
- **The card and template features need an external library** that is not shipped, as above.
- **`data/engine.json` invokes the `codex` CLI by name.** That is a real runtime dependency, not a label.

## License

MIT. See [LICENSE](LICENSE).

A Traditional Chinese version of this document is in [README.zh-TW.md](README.zh-TW.md).
