# klok

A command-line time tracker that pulls together the features people actually
use from Timetrap, Timewarrior, Watson, utt, Bartib, Helm and friends — in one
tool, with a plaintext backend and no dependencies beyond Python 3.9.

```console
$ klok start acme +api -n "auth endpoint"
Starting acme +api at 2026-08-31 09:00 [7d2d3734]

$ klok status
acme +api  1h 30m  (started 09:00, an hour ago)
  sheet default  |  today 1h 30m  |  id 7d2d3734

$ klok stop
Stopped acme +api after 1h 30m [7d2d3734]

$ klok report :week
Mon 31 August 2026 -> Sun 06 September 2026

acme - 3h 00m
        +api      1h 30m
        +docs        45m
        +review      45m

internal - 55m
        +meeting     40m
        +email       15m

Total: 3h 55m
```

## Install

```sh
pip install --user .          # or: pipx install .
```

No dependencies. It also runs straight from a checkout:

```sh
PYTHONPATH=. python3 -m klok status
```

## Why this exists

Each of the well-loved terminal time trackers is missing one thing the others
have. Timewarrior has the best interval surgery (`join`, `split`, `fill`,
`gaps`) but no pomodoro. Watson has the nicest project/tag model but no
timesheets. Timetrap has sheets and a pile of export formats but no tags. utt
has the "log it after the fact" flow nobody else does. Helm and arttime are
lovely timers that record nothing.

klok takes the useful parts of each and keeps a single, boring data file.

### Feature map

| From | Feature | In klok |
| --- | --- | --- |
| **Timetrap** | `t in` / `t out` | `klok in` / `klok out` (aliases of `start`/`stop`) |
| | Timesheets you switch between | `klok sheet NAME`, `klok sheets`, `klok sheet -` |
| | `t archive` | `klok archive :lastmonth` |
| | Retroactive `--at` | `klok start --at 9am`, `klok stop --at -15m` |
| | Formatters (csv, ical, json, ledger) | `klok export -f csv\|ical\|json\|ledger\|timeclock\|md\|tsv` |
| | Rounding | `--round 15`, or `general.round` in config |
| **Timewarrior** | `start` / `stop` / `continue` | `klok start` / `stop` / `continue` |
| | Tags on intervals | `klok start acme +api +urgent`, `klok tag ID +x -y` |
| | `annotate` | `klok annotate ID "text"` |
| | `summary` with daily subtotals | `klok summary :week` |
| | Range hints (`:week`, `:lastmonth`) | same hints, plus `last 7 days`, `since monday` |
| | `join` / `split` / `lengthen` / `shorten` / `move` / `fill` | all present, same names |
| | `gaps` | `klok gaps :day --min 15m` |
| | Exclusions (working hours, days off) | `exclusions.hours`, `exclusions.days`, `exclusions.holidays` |
| | `undo` | `klok undo` (100 levels deep) |
| | Charts | `klok chart --by day\|project\|tag\|sheet\|punchcard` |
| **Watson** | `project +tag` model | same |
| | `status`, `cancel`, `restart`, `log`, `report` | same names |
| | `rename project\|tag OLD NEW` | `klok rename` (also renames sheets) |
| | `projects`, `tags` listings | `klok projects`, `klok tags` (with totals) |
| | `edit` in `$EDITOR` | `klok edit` — or change fields inline with flags |
| | `aggregate` daily totals | `klok chart --by day`, `klok stats` |
| **utt** | Name the activity *after* doing it | `klok add acme +api` closes the open stretch |
| | `utt stretch` | `klok stretch` |
| | Break activities | tag them and filter with `-T break` |
| **Bartib** | Plaintext, hand-editable backend | JSONL, one entry per line |
| | `continue`, `last`, `change` | `klok continue`, `klok log --limit`, `klok switch` |
| | `sanity` check | `klok check` (overlaps, negatives, runaway timers) |
| **Helm / arttime** | Minimal countdown timer | `klok timer 25m --big --art coffee` |
| | Desktop notification + bell | automatic; `focus.notify`, `focus.bell` |
| **Focusd** | Focus sessions | `klok pomodoro acme +deep --rounds 4` — recorded as real entries |

## Commands

### Tracking

```sh
klok start acme +api -n "auth endpoint"   # aliases: in, on
klok start acme --at 9am                  # retroactive start
klok stop --at -15m                       # stopped a quarter hour ago
klok cancel                               # discard the running entry
klok status                               # aliases: now, current
klok switch internal +meeting             # stop and start at one instant
klok continue                             # same project and tags as last time
klok track 09:00 to 11:30 acme +api       # record a finished interval
klok track --from 09:00 -d 45m acme       # ...or give a length
klok add internal +email                  # close the gap since the last entry
klok stretch                              # pull an entry back to the previous one
```

`start` stops whatever was running (set `general.autostop = false`, or pass
`--no-stop`, to make that an error instead).

### Fixing what you recorded

Entries are addressed by id prefix (`7d2d`), by position (`@1` is the newest,
`@2` the one before), or by `@` for "the running one, or the last one".

```sh
klok annotate @1 "sprint planning"
klok tag @1 +urgent -meeting          # + adds, - removes
klok edit @1 --set-project acme --tags api docs
klok edit @1                          # opens $EDITOR with the entry as JSON
klok edit --range :day                # edit a whole day at once
klok move @1 08:00                    # keep the length, change the start
klok lengthen @1 30m / klok shorten @1 15m
klok fill @1                          # grow until it touches its neighbours
klok split @1 --at 10:15 / --into 3
klok join 7d2d e57b                   # merge entries, union their tags
klok delete @1 @2
klok undo                             # every change is undoable
```

### Reporting

```sh
klok log :week                        # entries day by day (aliases: display, list)
klok summary :day                     # dense table with daily subtotals
klok report :month --entries          # totals per project, broken down by tag
klok chart :month --by day            # bar chart per calendar day
klok chart :month --by punchcard      # weekday x hour heat map
klok gaps :day --min 15m              # untracked stretches
klok stats :month                     # totals, streak, busiest day
klok projects / klok tags             # with totals
```

Every report takes the same filters:

```sh
-p, --project NAME     repeatable; matches exactly, as a parent (acme matches acme.api), or as a glob
-t, --tag TAG          repeatable; all listed tags must be present
-T, --exclude-tag TAG  repeatable
    --contains TEXT    substring of project, tags or note
    --sheet NAME       a specific sheet
    --all-sheets       every sheet
-f, --format FMT       text (default), json, csv, tsv, ical, ledger, timeclock, md
```

### Time syntax

Anywhere klok takes a time: `now`, `noon`, `midnight`, `today`, `yesterday`,
`monday`, `last friday`, `9am`, `9:30pm`, `09:30`, `-15m`, `+1h30m`,
`2 hours ago`, `2026-08-31`, `2026-08-31 09:00`, `2026-08-31T09:00:00`, or a
unix timestamp.

Durations: `90` (minutes), `45s`, `25m`, `1h30m`, `1:30`, `2 hours`, `1w`.

Ranges: `:day` `:yesterday` `:week` `:lastweek` `:month` `:lastmonth`
`:quarter` `:year` `:lastyear` `:7d` `:30d` `:all`, plus `last 7 days`,
`since monday`, `2026-08-01 to 2026-08-31`, or a bare date for that one day.
`--from` / `--to` work too.

### Sheets

Separate timesheets, as in Timetrap — useful for keeping a client's hours apart
from your own.

```sh
klok sheet client-x        # switch
klok sheet                 # print the active one
klok sheet -               # back to the previous one
klok sheets                # list, with totals
klok archive :lastmonth    # move entries to _default so they leave reports
```

### Focus timers

```sh
klok timer 25m -m "Deep work" --big --art coffee
klok timer 10m -p acme -t review          # also records the time
klok pomodoro acme +deep --rounds 4       # 4 x 25m work with breaks
klok pomodoro --work 50m --break 10m --no-prompt
```

Work rounds longer than a minute are recorded as ordinary entries, so they show
up in `report` alongside everything else. Notifications go through
`notify-send`, `terminal-notifier` or `osascript` when one is available, and
always ring the terminal bell.

### Configuration

```sh
klok config list
klok config set general.round 15
klok config set exclusions.hours 09:00-18:00
klok config edit
```

| Key | Default | Meaning |
| --- | --- | --- |
| `general.default_project` | – | project for a bare `klok start` |
| `general.week_start` | `monday` | drives `:week` and the week charts |
| `general.round` | `0` | round durations up to N minutes in reports |
| `general.time_format` | `24` | `24` or `12` hour clock |
| `general.autostop` | `true` | `start` stops what was running |
| `general.color` | `auto` | `auto`, `always`, `never` (`NO_COLOR` is honoured) |
| `general.editor` | `$VISUAL`/`$EDITOR` | editor for `klok edit` |
| `general.confirm_new_project` | `false` | refuse unknown projects without `--force` |
| `exclusions.hours` | – | e.g. `09:00-18:00`; `gaps` ignores time outside |
| `exclusions.days` | – | e.g. `saturday,sunday` |
| `exclusions.holidays` | – | comma-separated `YYYY-MM-DD` dates |
| `focus.work` / `focus.break` / `focus.long_break` / `focus.rounds` | `25m` / `5m` / `15m` / `4` | pomodoro defaults |
| `focus.notify` / `focus.bell` / `focus.track` | `true` | timer behaviour |

### Where the data lives

```sh
klok where
```

Everything sits in one directory (`$KLOK_HOME`, else `$XDG_DATA_HOME/klok`,
else `~/.local/share/klok`):

```
frames.jsonl   one JSON object per line, sorted by start time
config.ini     configuration
state.json     which sheet is active
undo.jsonl     the last 100 states, for klok undo
```

`frames.jsonl` is the whole database. It diffs well in git, syncs with any file
sync tool, and can be hand-edited:

```json
{"id": "7d2d3734", "start": "2026-08-31T09:00:00+02:00", "stop": "2026-08-31T10:30:00+02:00", "project": "acme", "tags": ["api"], "note": "auth endpoint", "sheet": "default"}
```

Writes are atomic and take a lock file, so a `klok stop` from a shell hook
cannot interleave with one from your prompt. Every mutation snapshots the
previous file first — `klok undo` walks back through them.

### Exports

```sh
klok export :month -f csv -o august.csv
klok export :month -f ical > august.ics
klok export :month -f timeclock | hledger -f - balance   # ledger/hledger native
klok export :month -f md                                 # paste into an invoice
klok import august.json                                  # JSON, JSONL or CSV
```

### Shell completion

```sh
source <(klok completion bash)     # or zsh
klok completion fish > ~/.config/fish/completions/klok.fish
```

## Tests

```sh
cd klok && PYTHONPATH=. python3 -m unittest discover -s tests -t .
```

107 tests covering the time parser, the store (including undo, locking,
overlaps and gaps), the exporters, and every command end to end.

## What it deliberately does not do

- **No automatic screen-time or app-usage capture.** Focusd-style monitoring
  needs per-platform window hooks; klok records what you tell it, plus what its
  own timers observe.
- **No sync server.** `export`/`import` and a synced `frames.jsonl` cover the
  ground Watson's `sync` does, without a service to run.
- **No plugin/extension API.** The JSON and CSV exports are the extension
  point.

## Licence

MIT.
