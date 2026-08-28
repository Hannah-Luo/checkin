# Lab Check-In and Table Allocation

Door check-in for the Lab's paid economics experiment recruitment. A student holds their pass up to a webcam, the server reads the matric number, and the screen tells them a table number.

The roster comes from a Google Form responses sheet. Check-in itself is local: the decision is made in memory, written to SQLite, and only then queued for the sheet.

## Contents

- [What it does](#what-it-does)
- [Running it locally](#running-it-locally)
- [Configuration](#configuration)
- [Google Sheet write-back](#google-sheet-write-back)
- [What is kept, and where](#what-is-kept-and-where)
- [Session day](#session-day)
- [When something goes wrong](#when-something-goes-wrong)
- [Project layout](#project-layout)
- [HTTP API](#http-api)

## What it does

### Identify the student

Two paths, both ending in the same decision function.

**Camera.** The browser grabs a frame, posts it to `POST /api/frame`, and waits for the reply before grabbing the next one, so the frame rate self-limits to whatever the machine sustains. The server then:

1. **Gates** the frame on sharpness only (Laplacian variance of a 320x180 copy, minimum 12.0). A blurred frame cannot be read at any exposure. After 8 consecutive refusals one frame is forced through regardless, so a gate rule can only ever slow reads down, never stop them.
2. **Runs OCR** with RapidOCR on ONNX Runtime, tuned in `app/vision.py`: `det_limit_side_len=416`, angle classifier off, `intra_op_num_threads=6`. `cv2.setNumThreads(1)` at import is worth 9x on this machine and must stay.
3. **Matches** every ID-shaped line against the scan pool using a weighted edit distance where the substitutions OCR actually makes (`5`/`S`, `0`/`O`, `1`/`I`, `6`/`G`) cost 0.3 and everything else costs 1.0.
4. **Votes.** The same student must win 2 of the last 3 frames before check-in fires. A single good read is not evidence. After a check-in the same matric is ignored for 8 seconds, so holding the card up does not re-fire.

Auto-accept needs both a close match (distance at most 1.2) **and** a clear margin over the runner-up (at least 0.8). Two students whose matrics differ by one confusable character are flagged on `/admin` at load time and will need manual confirmation.

The scan pool is this session's roster plus everyone in the Form with no allocated slot, so a walk-in who fills the form at the door scans straight through. Students allocated to a *different* session are excluded from it, because that pool is what decides who gets a **table**.

**A scan that pool cannot place is not refused — it goes on the walk-in list.** Once the same unplaced number has won 2 of the last 3 frames, it is matched a second time against the *whole* Form at the same thresholds. That second pass is necessarily stricter than the first: the wider pool has more competitors, so the margin can only shrink, and nothing it accepts could have been rejected by the pass that seats people. What it produces is a queue position, never a seat:

| What was scanned | Row on the walk-in list |
|---|---|
| someone allocated to session 9 | `allocated to session 9` |
| a card no Form row matches, or one read wrongly | `card not in the Form — check the sheet` |

Both wait for `Admit anyway` like every other walk-in, and both carry that reason in the `why` column, so nobody is turned away at the door and the experimenter can settle it against the sheet afterwards. A number that matched nothing is still queued for the write-back, where the Apps Script finds no row for it, writes nothing, and reports it under *NOT FOUND in the sheet* on `/admin` — which is the list to reconcile from. Nothing is written to the Form that is not already written for a walk-in.

**Manual.** Type into the box on `/operator` or `/admin`: the last 4 digits, a full matric, or a name. One hit checks in immediately; several come back as buttons to tap.

### Decide what happens

`Allocator.check_in` applies these rules in order, and there is no override button on either screen.

| Condition | Outcome |
|---|---|
| Already checked in | re-display their table, no second seat |
| Not in the Form at all | typed: refused, unless `waitlist.allow_not_in_roster`. Scanned: walk-in list |
| `Repeat` column matches `waitlist.block_repeat_values` | refused, repeat participant |
| Allocated to another session | waiting list; a scan puts them there whatever `waitlist.allow_wrong_session` says |
| Release mode is on | identified and stopped, no table, nothing written |
| Confirmed, allocated to this session, a table is free | seated |
| Anything else | waiting list |

### Allocate a table

Seats come off a **seat deck**: a stored permutation mapping slot 1..N to the physical table dealt at that slot. The deck is stored state, never re-derived from a seed and a table count.

- `seat_order: random` shuffles *inside blocks of `group_size`*. With group size 8, tables 1-8 are dealt in random order before any of 9-16, so **the table number is the group** and you can start group 1 as soon as its eight seats are taken. Friends who queue together still do not get adjacent tables.
- `seat_order: sequential` deals 1, 2, 3 in order.
- **Adding tables never moves anyone.** The dealt prefix of the deck is frozen and only undealt seats are re-dealt over the larger room. With grouping on, new tables arrive as the next whole group.
- **Removing tables is refused** once a table at or past that point has been dealt, because renumbering would contradict what a student was already told.

One session can span several **rooms**. Each room is a block on `/admin` with its own table count; the numbering runs straight through, so a 24-table room followed by a 16-table one is tables 1-24 and 25-40, one queue and one seating. A block warns when a group straddles the boundary.

### Run the queue

The waiting list is ordered by tier, then by arrival within the tier:

| Tier | Who |
|---|---|
| `allocated, confirmed` | waiting only because the room was full |
| `allocated, not confirmed` | shortlisted for today, never replied |
| `walk-in` | no slot for today, or not in the roster |

Walk-ins get their own list and their own button on `/operator`. **Admit anyway** takes one walk-in per press. Each press names who is next and the table they would get. If every table is taken the press adds one group of tables to the session's last room and seats the person there, and tells you which number cards to put out. Nothing promotes on a timer. Please always have walk-ins fill out the Google Form and submit their information, so that if you need to repeat the experiment, you can be sure they haven’t participated more than once.

### Release mode

When the room is full and people are still queueing, click the `Release: off` badge in the top bar. From then on every scan identifies the student, reports `CONFIRMED`, `NOT CONFIRMED` or `ANOTHER SESSION` in amber, and stops. No table, no queue position, and nothing written to the sheet. Scanning the same person twice says "already released, do not pay again". This feature is primarily used to verify whether students have confirmed through the confirmation link. If they have, the show-up fee is paid; if not, please have them leave (the show-up fee is paid only for students who arrive on time, confirmed, but are unable to participate in the lab).

### Undo

`Ctrl+Z`, or the red `undo` on any row. The table returns to the pool for the next arrival, the local row is deleted, and `show_up = 0` with an empty `Table` is queued for the sheet so the two records cannot disagree. Undoing a released student sends nothing, because nothing was ever written for them.

### Keep the roster live

The responses sheet is re-read every `roster.refresh_seconds` over the credential-free gviz CSV export, which needs only link sharing. Correct a mis-typed matric in the sheet at the desk and the door accepts that student within a minute.

A refresh that fell back to the cache or came back empty is **discarded**: a stale roster is how someone who is on the list gets turned away. `Reload now` on `/admin` overrides that deliberately. Every successful download is cached to `roster.cache_path`, but only if it parsed to at least one student, so a broken sheet that still answers HTTP 200 cannot destroy the offline fallback.

What lands in that cache is the **parsed** roster, not the download: matric, name, allocated slot, `Confirmed`, `Repeat`, `Shortlist Sent`, `Show-up` and the `Session Selection` text. The importer reads no other column, so email address, phone number and gender never reach the disk. The one cost is that row numbers in a warning raised from a cached read count rows in the cache, not in the sheet.

### Write back to the sheet

Check-ins are queued in SQLite and drained by a background thread to an Apps Script Web App. The sheet write is never on the critical path. See [Google Sheet write-back](#google-sheet-write-back).

### Never show a name in a browser

The operator screen faces the student, so every browser payload carries initials (`M. Y. M.`) and masked matrics (`U252••••H`). Even the camera's OCR echo hides lines that look like a roster name. Full names live in the database and in the sheet.

A roster row is six fields — row, matric, masked matric, name, session, confirmed — and that is the whole of what a search can return. The Form's other columns are not carried on the participant at all, so an email address or a phone number cannot reach a payload by accident.

### The record afterwards

There is no export. The table number is written into the responses sheet as it is dealt, so the seating chart is a column in the sheet you already have, beside `Show-up`, and nothing has to be closed at the end of a session. The group is readable from the table number, because tables fill one `group_size` block at a time. Everything else — full name, arrival time, method, confidence, the anonymous subject id, and the whole event trail — stays in `data\checkin.db`.

## Running it locally

This is how the desk should run. Everything is on the laptop, and the only network call is to the Google Sheet.

```bash
.venv\Scripts\python.exe -m app.server
```

| Screen | URL | Who uses it |
|---|---|---|
| Operator | `http://127.0.0.1:8000/operator` | the desk, and the student across it |
| Admin | `http://127.0.0.1:8000/admin` | session, rooms, roster sheet, write-back |
| API docs | `http://127.0.0.1:8000/api/docs` | the generated OpenAPI page |

`--host` and `--port` change the address, `--reload` restarts on a file change.

**Keep the server on `127.0.0.1`.** `getUserMedia` only hands a page the camera in a secure context. `localhost` and `127.0.0.1` count; `192.168.x.x` does not, and the camera will silently refuse to open. The camera therefore only works on the machine running the server.

**Frames stay on the laptop.** The browser draws the camera onto a canvas, encodes a JPEG at quality 0.8, and posts it as the raw body of `/api/frame`. It asks the camera for 1280x720, but that is a preference rather than a constraint, so the canvas follows whatever size the camera actually hands back. The loop waits for each reply before it grabs the next frame, so the rate is one over the round trip — about four a second at a healthy 250 ms, slower when OCR is slower, and never faster than the server can read. On `127.0.0.1` the post crosses the loopback interface, so the picture never reaches a network adapter. Hosted, that same loop becomes a continuous stream of students' faces to a third-party server: still written to disk nowhere, but transmitted, and sitting in someone else's memory while it is read. [What happens to a frame](#what-happens-to-a-frame) traces one from the canvas to the reply.

**Use the project's own `.venv`,** never a Conda base environment. OpenCV and ONNX Runtime are both compiled against the NumPy C ABI, so they inherit whatever NumPy is already installed. A base environment carrying a NumPy/SciPy version conflict turns that into an import-time crash in native code rather than a readable pip error, and pip resolving NumPy to suit them can break unrelated packages in the same environment. A venv is disposable and leaves the base install untouched.

Fresh install:

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Time the OCR engine in the running process. Healthy is 250-400 ms per frame; seconds per frame means something heavy is running, a second copy of the server is competing for cores, the laptop is on battery saver, or free RAM has run out and it is swapping:

```bash
curl http://127.0.0.1:8000/api/diagnostics
```

## Configuration

Three places, in increasing precedence.

1. `config\session.yaml`, read at startup. The starting point for everything except the sheet id and the write-back credentials, which are blank in it on purpose.
2. `data\settings.json`, written by `/admin`. Holds the connected sheet and a write-back link per sheet id. Wins over the YAML, and survives a restart.
3. `CHECKIN_CONFIG`, `CHECKIN_DB`, `CHECKIN_SETTINGS` environment variables, which override the file paths themselves.

`CHECKIN_SHEET_ID`, `CHECKIN_SHEET_GID`, `CHECKIN_WRITEBACK_URL` and `CHECKIN_WRITEBACK_SECRET` fill in the four values the YAML no longer carries, for a machine set up without touching `/admin`. A sheet connected on `/admin` still wins over them, so they seed a fresh install rather than override a working one.

### `config\session.yaml`

| Key | Meaning |
|---|---|
| `session.number` | the value in the sheet's `Allocated Time Slot` column. Also settable on `/admin` |
| `session.start_time` | shown in the admin settings line only. Not the date on the header, which comes from the Form |
| `session.operator` | stamped on every check-in in the local audit trail |
| `layout.total_tables` | tables in the room, and therefore the capacity. Settable live on `/admin`, which wins |
| `layout.seat_order` | `random` or `sequential` |
| `grouping.group_size` | seats per group. `1` means no grouping and the whole room shuffles as one |
| `grouping.enforce_complete_groups` | at finalise, truncate to a whole multiple of the group size |
| `grouping.group_formation` | `consecutive` or `random_partition`, used only by finalise |
| `waitlist.allow_wrong_session` | `true` sends a student allocated elsewhere to the waiting list instead of refusing them. Governs typed entries; a scan waitlists them either way |
| `waitlist.allow_not_in_roster` | `true` admits someone with no Form row at all. Leave it `false` — it governs typed entries, and a scan already goes to the walk-in list |
| `waitlist.block_repeat_values` | values in the sheet's `Repeat` column that refuse a check-in outright |
| `roster.sheet_id` | the `/d/<ID>/` part of the sheet URL. Needs Viewer link sharing, no credentials. Blank on purpose: connect the sheet on `/admin`, or set `CHECKIN_SHEET_ID` |
| `roster.gid` | tab id, if the responses tab is not the first one |
| `roster.csv_path` | a local CSV to read instead of the sheet. Takes precedence over everything |
| `roster.cache_path` | last good roster, used automatically when the network is down |
| `roster.timeout_seconds` | HTTP timeout for the sheet read |
| `roster.refresh_seconds` | background re-read interval. `0` turns it off, `Reload now` still works |
| `roster.confirmed_true_values` | cell values in the `Confirmed` column that count as confirmed |
| `sheet_writeback.enabled` | `true` drains the queue to the Apps Script |
| `sheet_writeback.dry_run` | legacy. `true` is folded into "off"; use the off mode instead |
| `sheet_writeback.webapp_url` | the Apps Script `/exec` deployment URL. Blank on purpose: paste it on `/admin`, or set `CHECKIN_WRITEBACK_URL` |
| `sheet_writeback.shared_secret` | must match `SHARED_SECRET` in the *deployed* script. Blank on purpose — a secret in a file that goes into a repository is not a secret. Type it on `/admin`, or set `CHECKIN_WRITEBACK_SECRET` |
| `storage.db_path` | SQLite file. Point a new experiment at a new file to keep the old sessions separate |

### Column names in the responses sheet

Headers are matched loosely, on the header with punctuation and case removed, and the most specific spelling wins. The live sheet spells it "Matricution Number" and that is fine.

| Field | Header substrings looked for |
|---|---|
| matric | `matricutionnumber`, `matriculationnumber`, `matricnumber`, `matric` |
| name | `name` |
| session | `allocatedtimeslot`, `allocatedsession`, `allocatedslot` |
| confirmed | `confirmed`, `confirmation` |
| repeat | `repeat` |
| session selection | `sessionselection`, `sessionpreference`, `availability`, `sessionselected` |
| show-up | `showup`, `shownup`, `attended` |

`matric`, `session` and `confirmed` are required; the rest are optional. The header line on `/operator` is not configured anywhere. It is parsed out of each respondent's `Session Selection` cell, and the label for a session is the most common wording naming it, so rewording the Form option changes the screen.

### Changing experiment

For a new Google Form, change nothing in the YAML on a laptop. Paste the new responses sheet on `/admin`, press Connect, then paste that sheet's own write-back link underneath. The write-back deliberately comes back empty when you connect a different sheet, because an Apps Script deployment is bound to the spreadsheet it lives in and keeping the old URL would post the new sheet's check-ins into the old one. Point `storage.db_path` at a new file so the old sessions stay intact.

## Google Sheet write-back

The only network call in the system, and it is off the critical path:

```
check-in -> SQLite (instant, local, cannot fail)
         -> a row in sheet_queue
         -> background thread -> Apps Script -> Show-up = 1, Table = 7
```

It writes **four cells per matched row and nothing else**: `Show-up`, `Table`, and for a walk-in who has just been given a table, `Walk-in` plus the session number keyed into their own `Allocated Time Slot`. Rows are matched by matric number, never by row index, because a Form responses tab renumbers itself whenever a submission arrives. Columns are matched on the exact normalised header, never by position, and the script refuses to run if two columns claim the same role.

`Table` is the one cell the laptop is allowed to empty, and only its own: it holds a number while that student holds that table, and goes blank the moment they do not — an undo, a dismissal at finalise, a walk-in still on the waiting list. That is deliberate. A sheet that says `Show-up = 1, Table = 7` for somebody who is not at table 7 is worse than no column at all. `Show-up`, `Walk-in` and `Allocated Time Slot` are never blanked, so no ordinary student's allocation can be cleared.

Everything else stays local. Arrival times, method, confidence, the subject id and the event trail live in SQLite; duplicating them into a live recruitment sheet would add risk to the one file that must not break.

### Deploying the Apps Script

1. Add a column headed exactly `Table` to the right of `Walk-in`. Without it the check-ins still land, and the reply says how many table numbers went nowhere.
2. Open the responses sheet, then Extensions, then Apps Script.
3. Delete everything in `Code.gs` and paste in the whole of `tools\apps_script.gs`.
4. Change `SHARED_SECRET` at the top to any random string.
5. Run `selfTest` from the editor. It writes nothing and logs exactly which columns it would write to, `Table` included. Read that output before going further.
6. Deploy, then New deployment, type Web app, Execute as **Me**, Who has access **Anyone**.
7. Copy the `/exec` URL and paste it into the Write-back link block on `/admin`, with the secret and mode `off`. Rehearse, then switch to `live`.

`RESPONSES_TAB` at the top of the script selects a tab by name; blank means the first sheet in the workbook.

### Off is the rehearsal mode

There are two modes, `off` and `live`. **Off writes nothing and loses nothing**: every check-in is still queued. Run a whole rehearsal with it off, then set it to live and press `Send now`, and the backlog goes in at once. This is also why switching it on halfway through a real session is safe.

The one gap is a session **restarted** while the write-back was off. The restore deliberately does not re-queue, or every restart would re-send everybody, so `POST /api/sheet/resend` exists to re-queue everyone already checked in. Re-sending is safe: the script matches by matric and sets the same `Show-up = 1` and the same table.

A row queued before the `Table` column existed carries no table at all, and the script leaves that cell exactly as the sheet has it rather than clearing it. An old backlog draining after an upgrade cannot wipe a column it never knew about.

### `bad secret` means redeploy, not retype

If every batch is refused with `bad secret` and the two secrets look identical, they are. **Saving `Code.gs` does not change what the `/exec` URL runs** — the deployment serves a pinned version, so an edited but not redeployed script is still checking the old secret.

> Deploy, then Manage deployments, then the pencil icon, then Version: New version, then Deploy.

The URL does not change. Nothing is lost while it is broken: the check-ins are already in SQLite and the rows stay queued. Press `Send now` afterwards.

## What is kept, and where

**Nothing in `app\` writes an image.** A frame is decoded in memory, read, and dropped. There is no face detection and no recognition of any kind in the system: the OCR reads the number printed on the card, and what the matcher compares is a string.

| Where | What is in it |
|---|---|
| the browser | initials, masked matrics, table numbers, and the OCR echo with roster names blanked out |
| `data\roster_cache.csv` | matric, name, allocated slot, `Confirmed`, `Repeat`, `Shortlist Sent`, `Show-up`, `Session Selection` |
| `data\checkin.db` | matric, full name, table, group, arrival time, method, confidence, and the event trail |
| `data\settings.json` | the connected sheet, and the `/exec` URL and secret per sheet id |
| the responses sheet | the whole Form submission, plus `Show-up`, `Walk-in` and `Table` |

Nothing is written to disk outside `data\`. There is no export directory and no audit file, so there is also no second copy of the participant list to remember to delete — `tools\purge.py` and the sheet are the whole of it.

### What happens to a frame

`POST /api/frame` reads the body into memory. `Service.process_frame` decodes it into an array with `cv2.imdecode` and hands that array to `Pipeline.process`, which measures sharpness and brightness, runs OCR, and returns text. The array is a local variable inside that call. Nothing stores it, and when the call returns the last reference to the pixels is gone. What survives is a `FrameResult` carrying the strings OCR read, the two gate numbers and the timings — and the JSON reply, which is that same thing with roster names blanked out.

Four things would normally leave a copy of an image behind. None of them is in the path:

| What would leak a frame | Why it does not |
|---|---|
| the browser saving it | the blob goes straight into the `fetch` body — no object URL, no download, and no `localStorage`, `sessionStorage` or IndexedDB anywhere in `app\static\` |
| the server spooling the body | it arrives as a raw `image/jpeg`, which Starlette buffers in memory; multipart form uploads are the ones that spool to a temp file, and nothing here posts a form |
| the access log | uvicorn runs with `access_log=False`, and an access log records the request line, never a body |
| the OCR engine | RapidOCR on ONNX Runtime, loaded in-process and used for inference only — no upload, and its draw-the-boxes-and-save helper is never called |

### Deleting it afterwards

```bash
.venv\Scripts\python.exe -m tools.purge
```

With no arguments it lists what is on disk and deletes nothing. `--session N` removes one session's check-ins and events; `--all` removes the database and the roster cache. Neither deletes anything until you add `--yes`.

Both refuse while check-ins are still queued for the sheet, because deleting those would lose a `Show-up` that never arrived; `--force` overrides that once you have decided. It reads `CHECKIN_CONFIG` and `CHECKIN_DB`, so a rehearsal database is purged instead of the real one when those are set, and it never touches the Google Sheet.

## Session day

1. **Set the session on `/admin`.** Each room block has its own session, table count and group size. The desk's own block is the one whose line underneath counts the waiting list; every other block only counts what is seated. Type the number from the sheet's `Allocated Time Slot` column into it, set tables and group size, press Apply. `Add room` puts another block on screen: type the same session number into it to give that session a second room, or a different session number to prepare or watch another one. Apply is all-or-nothing, and a refusal names the tables that are in the way. Every block carries a `remove` except the desk's own first one, which is the block that always exists: on an added session it just hides the block and keeps its data, on an added room it takes that room's tables out of the session and is refused while anyone is dealt in or past them.

When one session has **two or more rooms**, `/operator` grows a **seat in** switch — one button per room, each naming its table range. Pressing `room 2` sends the next arrivals to that room's tables; the tables skipped in the other rooms stay free and are handed out when you switch back, so nothing is stranded. With no room picked the seating fills straight through as usual. The choice is stored against the session and survives a restart. Different sessions never get this switch: you check in for each session on its own, and which session the desk serves is set on `/admin`.
2. **Check the machine** with `curl http://127.0.0.1:8000/api/diagnostics`. 250-400 ms per frame is healthy.
3. **Open `/operator`, start the camera**, and turn the laptop so the student can see it. The line under the picture appears only when there is *no* picture, and names the cause in red. If you see nothing there, nothing is wrong. Whatever it says, typing the last 4 digits always works.
4. **Check people in.** The camera does it by itself. Keyboard fallbacks: start typing to reveal the manual panel, `Enter` to check in, `Esc` to clear and `Esc` again to hide it, `Ctrl+Z` to undo the last one.
5. **Watch the queue.** Press `Promote next` when you are ready to admit one more, and `Admit anyway` for one more walk-in. Each press names who is next before you commit.
6. **More people than tables?** Raise the count on `/admin`, or just press the button and let it add the next group.
7. **Closing the door with people still queueing?** Click `Release: off` in the top bar and keep scanning, then settle the show-up fees yourself.

What the student sees, over the camera picture and on the panel beside it, for 15 seconds or until the next scan:

| Outcome | Message |
|---|---|
| seated | **Checked in, thank you!** TABLE 7, please take a seat at this table and wait there |
| waiting list | **You are on the waiting list**, WAITING LIST #3, please wait to one side |
| already in | **You are already checked in**, TABLE 7, this is your table |
| released | **Thank you for coming**, CONFIRMED, please speak to the experimenter |
| refused | **Sorry, we could not check you in**, please speak to the experimenter |

No name and no matric ever appears on it.

## When something goes wrong

| Symptom | What to do |
|---|---|
| Camera will not open | read the red line under the picture; it names the cause. Manual entry is unaffected |
| Scans feel sluggish | watch the ms figure on the camera stats line. 200 ms and 5 fps is correct, because OCR runs on every frame. High fps with a large *skipped* count is the bad sign. Check `/api/diagnostics`, check for a second copy of the server, and check free RAM: a machine that has started swapping shows slow OCR with an idle CPU, and a fresh process is fast again |
| Reads keep failing | ask for the card **closer**, until it fills the frame, and held still for a beat. Brightness does not matter; `blurred` is the only thing the gate refuses. To watch what the pipeline makes of it, stop the camera on `/operator` and run `python -m app.vision --truth <matric>`, which prints every gate decision, read and wrong accept and saves nothing |
| "allocated to session 9, not this one" | a correct read of someone else's session. They are already on the walk-in list with that reason on the row; press `Admit anyway` when you decide to |
| A walk-in row says "card not in the Form" | the number on the row is what the camera read, and it matched no Form row — either the student mistyped their matric in the Form, or the card was read wrongly. Admit them or not as you choose; the number is on the row, in the database, and under *NOT FOUND in the sheet* on `/admin` to settle afterwards |
| A student says the sheet has their matric wrong | fix the cell in the sheet. The door picks it up within a minute, or press `Reload now` |
| Roster panel says "kept the roster already loaded" | a refresh failed and was discarded on purpose. You are still running on the last good download. Check the network |
| Check-ins are not reaching the sheet | open the write-back panel on `/admin`. *no link set for this sheet* means you connected a different sheet and it needs its own `/exec` URL. `bad secret` means redeploy |
| Network dies | nothing happens. The roster is cached, check-in is local, sheet writes queue and drain later |
| Laptop reboots mid-session | restart the server. Seating, waiting list, table counter, seat deck and release mode all rebuild from SQLite, and everyone comes back at the same table |
| Wrong person checked in | `Ctrl+Z`. The table goes back into the pool and `Show-up = 0` is queued for the sheet |
| A change to the screens does not appear | the browser cached a page. `Ctrl+Shift+R` once. The server sends `no-cache` on the pages and `/static` |
	
## Project layout

Eleven modules, six static files, three tools, one config file.

| Path | What it is |
|---|---|
| `README.md` | this file, the only documentation in the working tree |
| `requirements.txt` | dependencies |
| `.gitignore` | keeps the venv, the runtime data and `backup\` out of a repository |
| `config\session.yaml` | per-session configuration |
| `app\server.py` | FastAPI routes and the two screens |
| `app\service.py` | config, roster, allocator and storage, and the order they run in |
| `app\allocation.py` | seating, waiting list, promotion, finalise. Pure logic, no IO |
| `app\matcher.py` | OCR-aware weighted edit distance and the accept rules |
| `app\roster.py` | responses-sheet import, column mapping, session labels, caching |
| `app\vision.py` | gate, OCR engine, temporal voting. Also a CLI camera harness |
| `app\store.py` | SQLite: entries, events, meta, sheet queue |
| `app\sheets.py` | background drainer for the write-back queue |
| `app\settings.py` | what `/admin` writes: connected sheet, write-back per sheet id |
| `app\config.py` | `session.yaml` into typed dataclasses with safe defaults |
| `app\static\` | `operator.html`, `admin.html`, `app.css`, `common.js`, `operator.js`, `admin.js` |
| `tools\apps_script.gs` | paste into the sheet, deploy as a Web App |
| `tools\make_test_card.py` | render fake NTU Pass cards. Also used for the engine warm-up |
| `tools\purge.py` | delete the check-ins and roster cache this laptop is holding |
| `data\` | `checkin.db`, roster cache, `settings.json`. Runtime, gitignored |
| `.venv\` | the local runtime |

## HTTP API

`/api/docs` serves the generated OpenAPI page.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | redirect to `/operator` |
| GET | `/operator` | the desk screen |
| GET | `/admin` | the admin screen |
| GET | `/api/state` | everything both screens render, polled every 3 s |
| GET | `/api/display` | the student-facing message only, with its age |
| GET | `/api/search?q=` | roster lookup by matric fragment or name |
| GET | `/api/diagnostics?frames=` | time the OCR engine in this process |
| POST | `/api/checkin` | `{query, method, name}`, the manual path |
| POST | `/api/scan` | `{lines}`, OCR text in, the same decision out |
| POST | `/api/frame` | one JPEG as the raw body, from the camera |
| POST | `/api/scanner/reset` | clear the temporal voter |
| POST | `/api/undo` | `{matric}` |
| POST | `/api/promote` | `{limit, pool, grow}`, `pool` is `allocated` or `walk_in` |
| POST | `/api/release-mode` | `{on}` |
| POST | `/api/session` | `{number}`, move the desk |
| POST | `/api/settings` | `{session, room, total_tables, group_size}` |
| POST | `/api/rooms/add` | another room block |
| POST | `/api/rooms/remove` | `{session, room}` |
| POST | `/api/rooms/rekey` | `{session, room, to, total_tables, group_size}` |
| POST | `/api/rooms/desk` | `{room}` — which room of this session the next arrivals are seated in; `null` for any |
| POST | `/api/roster/reload` | force a re-read now |
| POST | `/api/sheet/connect` | `{url}`, point the roster at a responses sheet |
| POST | `/api/sheet/writeback` | `{url, mode, secret}`, stored against the connected sheet |
| POST | `/api/sheet/flush` | un-park and send the queue now |
| POST | `/api/sheet/resend` | re-queue everyone already checked in |
| POST | `/api/finalise` | compact holes, truncate to whole groups, label groups, and re-queue every table that changed |
