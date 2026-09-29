# Developing Balatro Mod Loader (`bml`)

The [README](../README.md) is for players. This is for running from source,
the command line, and how the pieces fit.

The vault ("mod library" in the window) holds every downloaded mod, sorted
into category folders. Profiles name mods; applying one links them into the
game's Mods folder.

## Setup

```
py -m pip install -r requirements.txt
copy .env.example .env      # optional: GITHUB_TOKEN raises the rate limit
```

Nothing in `.env` is required. The first time the window opens, the setup
wizard asks where Balatro is, where to keep the app's data and the mod
library, and which Mods folder the game reads. The answers go to
`%LOCALAPPDATA%\Balatro Mod Loader\settings.json`. The CLI reads the same
file, so run `py -m bml.main gui` once before using the commands below, or set
paths yourself: `BML_VAULT`, `BML_GAME_MODS` and `BML_GAME_DIR` beat the
settings file, and the settings file beats the defaults (`<app>\Mod Library`,
`%APPDATA%\Balatro\Mods`, and a Steam library search for the game).

## Use

```
py -m bml.main sync                 # refresh the catalog from all sources
py -m bml.main fetch --all          # download + categorise into the vault
py -m bml.main fetch --limit 5      # or work through it in batches
py -m bml.main fetch --mod Cryptid   # substring match on title or key

py -m bml.main search talisman      # what's in the catalog
py -m bml.main report               # dependency graph -> data/reports/
py -m bml.main verify               # re-hash the vault, detect drift

py -m bml.main recategorise --dry-run   # preview a re-sort after a rule change
py -m bml.main recategorise             # apply it (no downloads)
py -m bml.main import-installed         # snapshot the live Mods folder (READ-ONLY)

py -m bml.main add <github-url>         # index a mod no source carries
py -m bml.main capture                  # index installed mods no source carries
```

Everything above leaves the game alone. The commands that don't are below.

`fetch` is resumable and idempotent — interrupt it and re-run, and it picks
up where it stopped. A dead download URL fails that one mod and the run
continues.

## The window

```
py -m bml.main gui          # or double-click bml-gui.pyw
py tools/build_exe.py --shortcut     # …or build a real exe, see below
```

tkinter, so there is nothing extra to install. It is a UI over the *resolver*
specifically — pick mods, watch their dependencies arrive, see what's wrong,
apply. Downloading happens in three places only: **Add mod…** and **Capture
installed**, which each fetch one named thing, and **Setup…**, which reopens
the wizard with its two optional buttons (Lovely plus Steamodded, and the
whole community collection, with a Stop button). `sync` and `fetch` in the
terminal cover everything finer-grained.

Two columns that the CLI can't show together: **In profile** (what you've
asked for) and **In game** (what's actually linked right now). Green rows came
in as dependencies, red rows are part of a problem. Double-click or Space
toggles; the set re-resolves on every click. **New…** starts empty — nothing
enabled — because the reason to make a profile is to start clean; only the
Steamodded slot carries over.

`Preview changes` writes nothing. `Apply to game…` shows the plan and asks
first, and warns by name when adopting would move hand-installed folders.

Discord's greys, with a 1px green edge on every field. That needs ttk's `clam`
theme: `vista` and `xpnative` paint native bitmaps and drop every colour you
give them, so a dark Combobox comes out white.

### The Page tab — what a mod actually is

A category and a version number are not enough to decide whether you want a
mod. The **Page** tab answers the rest: the mod's GitHub README rendered in the
window, screenshots and all, above a count of what it puts in the game.

```
Cryptid
244 jokers · 5 tarots · 16 planets · 18 spectrals · 25 decks · 33 blinds …
```

Those counts are read from the mod's own `localization/en-us.lua`, not from a
description someone wrote once. Every object that shows a name in game has
exactly one key there, so the file cannot be out of date with the mod. (The
categoriser's `SMODS.Joker` call count is a different number and a worse one
here — Cryptid registers its jokers through its own helper and scores zero.)
350 of the 468 vaulted mods report something.

**It renders markdown; it does not embed a browser.** tkinter has no browser
widget, and the two ways to get one are pywebview (an OS window that cannot be
a child of a Tk pane) or cefpython (~100 MB of bundled Chromium, unmaintained
past Python 3.9). GitHub serves the README as markdown in one API call, so the
pane parses that and draws it in the app's own colours, with images downloaded
and shown inline and links opening in your real browser. Pages are cached for
7 days under `data/cache/pages/`; **Refresh** forces a re-fetch. Anonymous
GitHub allows 60 requests an hour — a `GITHUB_TOKEN` in `.env` raises it to
5000, and is the fix if the tab starts saying it is rate-limited.

**Right-click any mod** for: show its page, open it on GitHub, open its vault
folder, copy its mod id, and **Set the GitHub URL…** — which is how a mod whose
index entry has no link, or the wrong one, gets a page at all. That goes in
`data/repo-overrides.json`, deliberately *not* in `local-index/`: the two look
similar, and a typo in local-index changes which mod gets downloaded, while
this only changes what the Page tab reads.

### The desktop executable

```
py -m pip install pyinstaller
py tools/build_exe.py --shortcut
```

Produces `dist/Balatro Mod Loader.exe` — one file, ~32 MB, no console window,
icon embedded — and optionally a Desktop shortcut. The icon is
`assets/bml.ico`, drawn by `tools/make_icon.py` in the window's own palette
(each size drawn at its own scale, because a 256px render squeezed to 16px
turns the border into a grey smear).

The exe bundles the *code* and nothing else. The vault, the catalog, your
profiles and `.env` stay on disk and are found exactly as before — freeze those
in and every profile you saved would vanish on the next rebuild. So the exe is
disposable and your data is not. Rebuild it after changing the code.

Which means **the exe has to be told where the data is**, and the first build
was not. `config.ROOT` was `Path(__file__).parent.parent`, which under
`--onefile` is the temp directory PyInstaller unpacks into — so the exe made
itself an empty `data/` and `profiles/` in there, opened a catalog of zero
mods, showed `(nothing matches)`, and dropped every profile saved during the
session when Windows cleaned the directory up. Nothing errored; it just looked
like an empty vault. There are now two paths, and the split is the fix:
`config.BUNDLE` is where the code is (the icon, and nothing else),
`config.ROOT` is where the data is. Frozen, `ROOT` resolves in order:
`BML_ROOT`; the app folder saved by the setup wizard; a `data/catalog.db`
sitting beside the exe; the source tree recorded at build time in
`bml_home.txt`; else `%LOCALAPPDATA%\Programs\Balatro Mod Loader`. So the exe
runs from the Desktop shortcut, from `dist/`, or from anywhere, and `BML_ROOT`
moves the whole install.

`py tools/build_exe.py --release` is the build for other people. It leaves
`bml_home.txt` out, so a first launch lands in the setup wizard instead of in
a folder that only exists on the machine that built it. On that first launch
the wizard copies the exe into the app folder the user picked and makes the
shortcuts there.

Close the window before rebuilding. Windows will not overwrite a running
executable, and PyInstaller reports that as `PermissionError [WinError 5]` at
the very end of a two-minute build, which reads like a build failure; the
script now checks up front and says so instead.

Running from source gets the same icon and the same taskbar identity: the
window calls `SetCurrentProcessExplicitAppUserModelID`, without which Windows
groups it under python.exe and shows Python's icon there.

## Profiles — enabling and disabling mods

```
py -m bml.main import-installed              # first, snapshot what you have
py -m bml.main profile new live --from-installed
py -m bml.main profile add live Cryptid Talisman
py -m bml.main profile set live --steamodded "Steamodded"

py -m bml.main resolve live                  # what it actually needs
py -m bml.main apply live --dry-run          # what would change
py -m bml.main apply live --adopt
py -m bml.main status                        # what the game loads right now
py -m bml.main launch
```

A profile names mods; `resolve` adds their dependencies. `apply` makes
`%APPDATA%\Balatro\Mods` match by creating directory junctions into the vault —
so enabling costs nothing, disabling deletes only the link, and the vault stays
the one real copy.

**Nothing can be disabled until it has been adopted once.** `apply` never
deletes a directory it did not create, so a hand-installed mod keeps loading no
matter which profile is active, so a two-mod profile still launches with
every hand-installed mod running. Apply a profile containing them once with
`--adopt`; that turns them into junctions, and from then on profiles switch
them off. Until then the plan says `STILL LOADS <folder>` for each one.

**`apply` is the only command that writes to the game, and it will not delete
anything it did not create.** A hand-installed mod folder blocks the plan until
you pass `--adopt`, which *moves* it to `%APPDATA%\Balatro\Mods-bml-backups\`
(a sibling of `Mods`, so Steamodded's recursive scan doesn't keep loading it)
and then links the vault copy in its place. Junctions pointing outside the
vault, and Lovely's own `lovely\` directory, are never touched at all.

`apply` refuses to run on a profile that resolves with missing mods, version
conflicts or declared incompatibilities. `--force` overrides it; `--dry-run`
always shows the plan regardless.

## Running several Steamodded or Lovely versions

You can *store* as many as you like. Only one can ever be **active**: Lovely is
`version.dll` in the game folder and Windows loads one DLL of a name per
process, and Steamodded is a single folder defining the global `SMODS` table.
So multiple versions are a per-profile choice, not a wider-compatibility trick:

```
py -m bml.main profile set live --steamodded "Steamodded@Steamodded BETA-1925a"
py -m bml.main profile set old  --steamodded "Steamodded@Steamodded"
```

The slot matches a **vault folder name**, not a mod id, because the dependency
graph keys nodes by mod id and two builds of Steamodded are one node there.
Drop a second build at `MODLOADERS\Steamodded@<any name>\mod\` — by hand is
fine, no catalog entry needed — and name that folder.

Before reaching for it: across all 449 vaulted manifests, every declared
framework requirement is a floor (`>=`) except exactly one. `bundlesoffun`
wants `Steamodded (<<1.0.0~BETA-1827b)`. **One sufficiently new Steamodded
satisfies the other 448** — so the fix for broad compatibility is a newer
Steamodded, not several. The slot earns its keep for that one mod, for rolling
back a beta that broke something, and for testing a mod against the version it
was written for.

`--lovely` on `apply` swaps the game's `version.dll` for the profile's, keeping
the old one as `version.dll.bml-bak.<timestamp>` next to it.

## Tests

```
py tests/run_all.py           # every module, each in its own process
py -m tests.test_setup        # or one at a time
```

`test_setup` covers the wizard's plumbing without opening it: the settings
file, Steam library parsing, the folder checks (no library inside the Mods
folder, no network drives), and that Stop halts a download between mods.
`test_manifest` runs read-only against whatever mods are installed on the
machine running it, and finds them wherever they currently are: `apply --adopt` moved
them out of `%APPDATA%\Balatro\Mods` into `Mods-bml-backups\<timestamp>\`, so
the test picks whichever directory holds the most mod folders. It skips
cleanly if there are none. `test_modinfo` is offline — the markdown cases are
the shapes real mod READMEs use (`<img>` wrappers, badge rows, `blob/` links
that serve HTML instead of an image), and the content counter runs against a
miniature of Cryptid's `en-us.lua`. `test_links` and `test_apply`
create real junctions, but under `tempfile` with `BML_VAULT`/`BML_GAME_MODS`
redirected — they cannot reach the game or the vault. `test_gui` opens a real
Tk window, withdrawn, with `config.PROFILES` and `BML_GAME_MODS` redirected the
same way. It runs over the real catalog on purpose, so it skips cleanly with
no tkinter, no display, or an empty mod library.

## Adding a mod

Most mods need nothing — `sync` pulls 419 from the community index and 44 from
Thunderstore. The rest are the two cases below.

**It's on GitHub but no index carries it.** Give `add` the URL — the repo page,
a release, or a tag archive. A tag in the URL is kept as the pin:

```
py -m bml.main add https://github.com/someone/some-mod
py -m bml.main add https://github.com/someone/some-mod/releases/tag/v2.1 --category JOKERS+
py -m bml.main sync
py -m bml.main fetch --mod "some mod"
```

In the window this is the **Add mod…** button, which does all four steps.

**It has no public download at all.** Most of what circulates in the Balatro
Discord is an attachment and nothing else. Install it by hand once, then let
the installed copy *be* the source:

```
py -m bml.main capture        # index every installed mod no source carries
py -m bml.main sync
py -m bml.main fetch --all
```

`capture` reads the game folder and writes only to `local-index/`. Each entry
gets a `localSource` pointing at the installed folder, and `fetch` zips that
folder instead of downloading. From there it is an ordinary vault mod: hashed,
archived for rollback, categorised, and visible to `verify` and the resolver.
In the window this is **Capture installed**.

**By hand**, if you want to control every field — create
`local-index/<Author>@<ModName>/meta.json`:

```json
{
  "title": "Some Mod",
  "author": "Someone",
  "repo": "https://github.com/someone/some-mod",
  "downloadURL": "https://github.com/someone/some-mod/archive/refs/tags/v1.0.zip",
  "version": "1.0.0",
  "categories": ["Content"],
  "bmlCategory": "JOKERS+"
}
```

`bmlCategory` is optional and pins the vault folder, overriding every
heuristic. `localSource` (an absolute path to a folder) replaces `downloadURL`.
Local entries win over both the community index and Thunderstore, which also
makes this the way to **pin a version the index gets wrong** — see
`local-index/MathIsFun0@Cryptid/meta.json`, which holds Cryptid at its released
v0.5.16d because the index names a bare commit SHA that resolves to an
unreleased 0.5.17~dev.

**Copying folders around by hand does not work, and here is why.** The obvious
guess — drop the mod's folder into `local-index/`, drop a copy into the right
vault category, press *Reload vault* — produces nothing at all, twice over. A
`local-index/` entry is a *note about* a mod: `sync` reads `meta.json` there and
nothing else, so a folder holding the mod's own files is skipped. And *Reload
vault* does not read the vault; it reads `data/catalog.db`, which only `fetch`
writes. A vault folder that `fetch` did not create has no `_bml.json`, no
recorded hash and no catalog row, so nothing in the window can see it.

The route in is always the same one: keep the mod's files somewhere that is
*not* `local-index/` and not the vault, write an entry whose `localSource`
points at them, then `sync --source local` and `fetch --mod "<name>"`. `fetch`
does the rest — zip, hash, manifest, category, `_bml.json`, catalog row — and
the mod appears on the next *Reload vault*. `sync` now says so out loud when it
finds a mod where an entry should be, instead of skipping in silence.
