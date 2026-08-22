# musicdownload

`musicdownload` is a safety-first macOS command-line workflow for archiving the
music in your Spotify library as reviewed, tagged MP3 files. It syncs Spotify
metadata into SQLite, CSV, and Excel; finds likely official YouTube audio;
checks the runtime and version; downloads approved matches with `yt-dlp`; and
can compare the resulting files with audio already installed in the macOS
Music app.

The project is checkpointed throughout. You can stop and restart matching,
downloading, Music comparison, playlist creation, genre work, or metadata work
without beginning again.

> [!IMPORTANT]
> This tool reads Spotify metadata; it does not download Spotify audio. Download
> only media you own or are permitted to save, and follow the applicable service
> terms and laws.

## Features

- Sync Spotify liked songs and optionally every track from saved albums.
- Store the catalog in SQLite and export reviewable CSV and Excel files.
- Rank YouTube candidates using auditable rules in `matching_rules.py`.
- Prefer artist Topic uploads, official audio, and official lyric videos.
- Reject unexpected music videos, live takes, covers, karaoke, remixes, slowed,
  sped-up, remastered, clean, instrumental, and other altered versions.
- Make runtime a hard approval gate, not merely a small scoring bonus.
- Download approved matches as album-organized, Spotify-tagged MP3 files with
  track/disc numbers, ISRC, source IDs, explicit status, and album artwork.
- Download standalone videos with `yt-dlp` and convert them to QuickTime MOV
  files using H.264 video and AAC audio.
- Resume batches and retry failed tracks without blocking the remaining queue.
- Review only recent Spotify additions, score them, and download only that
  focused cohort.
- Compare downloaded MP3s with local files in Music and prefer the downloaded
  copy for exact or explicitly allowed close matches.
- Recreate Spotify playlists using only downloaded tracks already in Music.
- Build `Spotify - Liked Songs` in Spotify's exact newest-first order.
- Prepare an additive-only Music playlist for manual iPhone sync in Finder.
- Audit/apply genres and Spotify metadata with reversible change records.
- Merge duplicate collaboration artist entries (`Laufey/Los Angeles
  Philharmonic` shown as many artists) with a reversible credit cleanup.
- Use an explicitly confirmed `delete me pls` queue to remove Music entries,
  move unique files to macOS Trash, and block automatic re-download.

## Safety guarantees

- Report-only is the default for every Music-library operation.
- Normal sync, match, download, replacement, playlist, genre, and metadata
  commands never delete a Music entry or audio file.
- The separately named deletion queue is report-only unless both `--apply` and
  an exact `--confirm` value are supplied. It removes queued Music entries but
  moves unique files to macOS Trash rather than permanently erasing them.
- Existing downloaded files, database history, review sheets, and Spotify
  tokens are preserved across resumable runs.
- Albums containing `(VINYL)` are protected from Music replacement and metadata
  changes.
- Older duplicate entries are disabled, never deleted, when an exact downloaded
  replacement is explicitly applied.
- Apply operations write restoration plans or reversible before-state records.
- `.env`, `data/`, `downloads/`, `videos/`, virtual environments, and generated dependency
  trees are excluded from Git.

## Requirements

- macOS with the Music app
- Python 3.11 or newer
- FFmpeg
- A Spotify developer app using PKCE (no client secret required)
- `yt-dlp` and the Python dependencies in `requirements.txt`
- Microsoft Excel, Apple Numbers, or another `.xlsx` viewer for manual review

## Quick start

```bash
git clone --recurse-submodules https://github.com/ryleyp/musicdownload.git
cd musicdownload

brew install python ffmpeg
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install --no-deps --no-build-isolation -e .

cp .env.example .env
# Put your Spotify Client ID in .env, then:
music-library doctor
music-library guide
```

Create a Spotify developer app and register this exact redirect URI before the
first sync:

```text
http://127.0.0.1:8888/callback
```

The normal end-to-end workflow is:

```bash
# 1. Sync Spotify liked songs and saved albums.
music-library sync --include-albums

# 2. Find and automatically approve only strong, runtime-safe matches.
music-library match --auto-approve 95

# 3. Review data/music_library_review.xlsx, save it, then import decisions.
music-library review

# 4. Preview and download every eligible score-95+ match.
music-library download --min-score 95 --all --dry-run
music-library download --min-score 95 --all

# 5. Compare with local Music files (report-only first).
music-library local-music

# 6. After reviewing the reports, prefer exact copies and import new downloads.
music-library local-music --apply --import-new

# 7. Confirm the current checkpoint totals at any time.
music-library status
```

For a slower, fully guided setup, see [START_HERE_MAC.md](START_HERE_MAC.md).

## Command map

| Command | Purpose | Changes Music by default? |
| --- | --- | --- |
| `music-library doctor` | Check the environment, dependencies, Spotify setup, and AppleScript | No |
| `music-library guide` | Print the recommended safe workflow | No |
| `music-library status` | Show synced, matched, approved, downloaded, failed, and Music totals | No |
| `music-library sync --include-albums` | Sync liked songs and saved albums | No |
| `music-library match --auto-approve 95` | Search, score, and approve high-confidence sources | No |
| `music-library review` | Import decisions from the review workbook | No |
| `music-library download --min-score 95 --all` | Download and tag every eligible match | No |
| `music-library video URL` | Download a standalone video and convert it to MOV | No |
| `music-library recent` | Report recent liked/saved-album additions | No |
| `music-library recent --match` | Score recent additions needing matches | No |
| `music-library recent --download` | Download only eligible recent additions | No |
| `music-library local-music` | Report local Music duplicates and close matches | No |
| `music-library local-music --apply --import-new` | Prefer approved MP3s and import new files | **Yes** |
| `music-library playlists --sync` | Snapshot Spotify playlists and create a report | No |
| `music-library playlists --apply` | Create/resume Spotify-named Music playlists | **Yes** |
| `music-library liked-playlist` | Report the newest-first liked-song playlist | No |
| `music-library liked-playlist --apply` | Rebuild the exact-order liked playlist; retain the old one as backup | **Yes** |
| `music-library iphone` | Build an iPhone sync manifest | No |
| `music-library iphone --apply --open-finder` | Create/update the additive-only sync playlist | **Yes** |
| `music-library genres` | Audit missing Music genres | No |
| `music-library genres --apply` | Apply reviewed genre proposals | **Yes** |
| `music-library metadata` | Audit Spotify metadata on project imports | No |
| `music-library metadata --apply` | Apply and verify Spotify metadata | **Yes** |
| `music-library cleanup-hadestown` | Report inconsistent Hadestown album artists | No |
| `music-library cleanup-hadestown --apply` | Normalize cast-album grouping with a reversible run | **Yes** |
| `music-library cleanup-library-artists` | Audit all high-confidence album grouping splits | No |
| `music-library cleanup-library-artists --apply` | Normalize reviewed grouping splits with a reversible run | **Yes** |
| `music-library audit-library-artists` | Create an explainable full-library review queue and track detail data | No |
| `music-library artist-credits` | Audit duplicate multi-artist credit variants | No |
| `music-library artist-credits --apply` | Merge collaboration credit variants with a reversible run | **Yes** |
| `music-library delete-queue` | Audit `delete me pls` without deleting | No |
| `music-library delete-queue --apply --confirm "delete me pls"` | Remove queued Music entries and move unique files to Trash | **Yes** |
| `music-library phone-delete` | Audit the phone-synced `delete me pls` queue | No |
| `music-library phone-delete --apply --confirm "delete me pls"` | Apply the explicitly queued phone deletions | **Yes** |
| `music-library history --limit 50` | Show immutable workflow history | No |

## How YouTube matching works

It prefers:

1. Artist `Topic` uploads
2. Official audio
3. Official lyric videos
4. Other lyric videos when the title, artist, and duration closely match

It rejects music videos and unexpected live, cover, karaoke, remix, slowed,
sped-up, remastered, clean, instrumental, and other altered versions unless
Spotify names that exact version. Automatic approval always requires a trusted
source, a score at or above the chosen threshold, and runtime within 5 seconds.
Nothing is downloaded until the match is approved, unless you intentionally
enable high-confidence auto-approval.

The adjustable matching policy is centralized in `matching_rules.py`. The
score is 0–100, and runtime contributes up to 20 points with steep penalties
for mismatches. Edit the policy only with the scoring tests passing.

## What gets created

- `data/music_library.sqlite`: the main database
- `data/music_library.csv`: Excel and Numbers-compatible flat export
- `data/music_library_review.xlsx`: formatted review workbook
- `downloads/Artist/Year - Album/`: downloaded and tagged MP3 files

The MP3 tags include title, artists, album artist, album, release date, genre
when Spotify still provides it, track and disc numbers, ISRC, explicit flag,
Spotify ID, YouTube ID, source links, and Spotify album art.

## 1. Install the Mac requirements

Open Terminal, move into this project folder, and run:

```bash
xcode-select --install
```

If Homebrew is not installed, install it from
[brew.sh](https://brew.sh/). Then run:

```bash
brew install python ffmpeg
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install --no-deps --no-build-isolation -e .
```

The editable install adds the `music-library` command. Every original Python
script remains supported.

Whenever you open a new Terminal window, return to this folder and reactivate
the environment:

```bash
source .venv/bin/activate
```

## 2. Create the Spotify connection

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Create an app for personal use.
3. Add this exact Redirect URI in the app settings:
   `http://127.0.0.1:8888/callback`
4. Copy the app's Client ID.
5. In Terminal, run:

```bash
cp .env.example .env
nano .env
```

Replace `paste_your_client_id_here` with the Client ID. Press `Control+O`,
Return, then `Control+X`.

The app uses Spotify's PKCE login and does not need a Client Secret. New Spotify
development-mode apps currently require the app owner to have Spotify Premium.
Spotify may also require your account to be added in the app's Users Management
section.

## 3. Sync your liked songs and optionally saved albums

```bash
python spotify_sync.py
```

To also include every track from albums saved in Spotify's **Your Library**:

```bash
music-library sync --include-albums
```

Run future syncs with `--include-albums` to keep saved-album membership active.
Tracks that are both liked and on a saved album are stored and downloaded only
once. The CSV and workbook `library_source` column shows `liked_song`,
`saved_album`, or both. Matching, scoring, downloads, tagging, and local Music
comparison then include the album tracks automatically.

A sync without `--include-albums` returns the active catalog to liked songs
only. It preserves album-track database history and never deletes an existing
download.

Your browser opens once so you can grant read-only access to your Spotify
library. The token is stored locally in `data/.spotify_token.json`.

Genre is an artist-level Spotify field, not a song-level field, and Spotify has
deprecated it. The script saves it when available. To skip genre lookup:

```bash
python spotify_sync.py --skip-genres
```

Repeated syncs update changed metadata, add new library songs, and mark removed
memberships without deleting their history.

## 4. Find YouTube sources

Start with a small test:

```bash
python youtube_match.py --limit 20
```

Then continue with the rest:

```bash
python youtube_match.py
```

Matching is resumable. To search existing matches again:

```bash
python youtube_match.py --refresh
```

The matcher fetches full metadata for the best eight search results before
final scoring, improving runtime, availability, verification, and channel
evidence. Use `--hydrate-count` to adjust that number.

Candidates are ranked by an auditable source tier before their numeric score:
artist Topic/YouTube-provided audio, verified or exact official artist
channels, exact artist channels, loose artist-name channels, and unrelated
uploaders. The tier is stored in SQLite and shown on the workbook's
`Candidates` sheet. A channel merely containing the artist's name is never
enough for automatic approval.

To refresh one known track without changing the rest of the queue:

```bash
music-library match --spotify-id SPOTIFY_ID --auto-approve 95
```

Repeat `--spotify-id` to target several tracks.

For automatic approval of only very strong, non-rejected matches:

```bash
python youtube_match.py --auto-approve 95
```

Score alone cannot approve a track. The YouTube runtime must be available and
within 5 seconds, the artist must be confirmed, and the source must be an
artist Topic upload, official audio/lyric video, or verified artist channel.
Failed searches are checkpointed and do not block later songs:

```bash
python youtube_match.py --retry-errors
python youtube_match.py --retry-due
python youtube_match.py --retry-unmatched
```

### Undo all current matches

Preview a reset without changing anything:

```bash
music-library reset-matches
```

Apply it only after checking the totals:

```bash
music-library reset-matches --apply
```

To reset only matches scoring below 95 (a score of exactly 95 is kept):

```bash
music-library reset-matches --below-score 95
music-library reset-matches --below-score 95 --apply
```

The applied command creates a full SQLite backup, clears current YouTube
sources, candidates, scores, approvals, and incomplete download errors, then
returns those matched rows to `not_searched`. Searched rows that have no
selected YouTube source are not affected. Immutable matching history, Spotify
metadata, downloaded MP3 files, and local Music state are preserved. Rows with
completed downloads or a saved download path are protected by default.
`--include-downloaded` can reset their matching metadata too, but still never
deletes their audio files.

Failures have structured error codes and exponential-backoff retry times.
`--retry-due` follows the schedule; `--retry-errors` explicitly retries all
matching failures immediately.

## 5. Review in Excel or Numbers

Open `data/music_library_review.xlsx` and use the `Tracks` sheet.

- Put `approve` in `decision` to approve the suggested YouTube link.
- Put `skip` to exclude the track.
- Put a different full YouTube URL in `manual_youtube_url`, then set the
  decision to `approve`.
- Put `reset` to return a previous decision to suggested or not searched.
- The `Candidates` sheet shows the other ranked choices and score reasons.
- Both sheets show Spotify runtime, YouTube runtime, numeric seconds, and the
  absolute difference in seconds.

Save and close the workbook. Then run:

```bash
python import_review.py
```

The importer validates manual YouTube URLs and updates the database. It then
regenerates the workbook with the decisions reflected in `match_status`.
Every match and approval is appended to an immutable audit table:

```bash
music-library history --limit 50
music-library history --spotify-id SPOTIFY_ID
```

CSV and Excel replacements are atomic. Previous exports are preserved under
timestamped `data/backups/` folders before replacement.

## 6. Download approved MP3s

The downloader processes the next 100 unfinished approved songs per run. The
SQLite database is the checkpoint, so closing Terminal, losing internet, or
restarting the Mac does not make it start over. Each successful track is marked
complete immediately. Interrupted tracks return to the pending queue on the
next run. Failed tracks are recorded separately so they do not block later
batches.

Preview the exact files and sources first:

```bash
python download_mp3.py --dry-run
```

Download:

```bash
python download_mp3.py --min-score 95
```

`--min-score 95` also considers matches that were already searched and remain
`suggested`. It re-scores their saved metadata with the current rules, so
`youtube_match.py --refresh` is not required. The same trusted-source and
5-second runtime gates still apply. Use `--dry-run` first.

Run the same command again to download the next 100. Check progress at any time:

```bash
python download_mp3.py --status
```

Useful options:

```bash
python download_mp3.py --batch-size 25
python download_mp3.py --all
python download_mp3.py --retry-errors
python download_mp3.py --retry-due
python download_mp3.py --min-score 95
python download_mp3.py --output "/Users/yourname/Music/Spotify Archive"
python download_mp3.py --redownload
```

`--limit 25` still works as an alias for `--batch-size 25`.
Pending songs are processed before retried errors, so repeatedly failing files
cannot prevent later pending songs from being attempted.

The resulting MP3 runtime is independently read and compared with Spotify
before tagging or completion. A difference over 5 seconds is classified as a
non-automatic-retry runtime failure.

## Download videos as MOV files

The video command is separate from Spotify matching and MP3 downloads. It can
download any URL supported by `yt-dlp`, then uses FFmpeg to create a broadly
QuickTime-compatible `.mov` containing H.264 video, AAC audio, `yuv420p` pixel
format, and fast-start metadata.

Preview the exact command first:

```bash
music-library video "https://www.youtube.com/watch?v=VIDEO_ID" --dry-run
```

Download and convert at up to 1080p:

```bash
music-library video "https://www.youtube.com/watch?v=VIDEO_ID"
```

Files are saved under `videos/` using the video title and source ID. Partial
downloads are resumed automatically. Completed source IDs are checkpointed in
`data/video_download_archive.txt`, so repeating the command safely skips a
finished video.

Useful options:

```bash
# Download several URLs; a failure does not block later URLs.
music-library video "URL_1" "URL_2" "URL_3"

# Limit resolution or use the best available resolution.
music-library video "URL" --max-height 720
music-library video "URL" --max-height 0

# Choose another destination.
music-library video "URL" --output "/Users/yourname/Movies/YouTube"

# Explicitly allow a playlist URL to expand.
music-library video "PLAYLIST_URL" --playlist

# Keep the original pre-conversion download too.
music-library video "URL" --keep-source

# Ignore the completion archive and replace an existing result.
music-library video "URL" --redownload
```

Conversion defaults to CRF 20, the `medium` H.264 preset, and 192 kbps AAC.
Advanced users can adjust these with `--crf`, `--preset`, and
`--audio-bitrate`. MOV conversion re-encodes the video for compatibility, so it
can take approximately the video's runtime on slower Macs. Playlist expansion
is disabled unless `--playlist` is supplied.

## Main command, audit history, and structured logs

The original scripts remain available. The optional main entry point forwards
to them and adds environment diagnostics, a guide, and a combined status:

```bash
python music_library.py doctor
python music_library.py guide
python music_library.py status
python music_library.py sync
python music_library.py match --auto-approve 95
python music_library.py review
python music_library.py download --min-score 95
python music_library.py local-music
```

After installation, use the shorter command:

```bash
music-library doctor
music-library guide
music-library status
music-library match --auto-approve 95
music-library download --min-score 95
music-library history --limit 50
```

The status command reports synced, matched, approved, downloaded, failed, and
added-to-Apple-Music totals. `python library_status.py` is the shorter
standalone equivalent.

Workflow events are stored in SQLite and appended as JSON objects to
`data/activity.jsonl`. Tokens and Spotify credentials are never logged.

The downloader asks `yt-dlp` for the best available audio stream, converts it
to a high-quality VBR MP3 with FFmpeg, removes YouTube metadata, and writes the
Spotify metadata and album art. YouTube audio is already compressed, so MP3
conversion cannot improve its original quality. It only provides the requested
MP3 compatibility.

## Updating later

Run these commands whenever you want to add newly liked songs or saved albums:

```bash
source .venv/bin/activate
music-library sync --include-albums
music-library match --auto-approve 95
music-library download --min-score 95 --all
```

Review, import, and download as described above. Already downloaded tracks are
skipped.

### Review and process only recent additions

Create focused CSV and Excel reports for songs liked—or albums saved—during
the last 30 days:

```bash
music-library recent
```

The reports are `data/recent_spotify_additions.csv` and
`data/recent_spotify_additions.xlsx`. They show liked/saved-album membership,
Spotify added time, current YouTube score/status, Spotify and YouTube runtimes,
the runtime difference, and download state.

Refresh Spotify liked songs and saved albums, then score only recent tracks
that still need a match:

```bash
music-library recent --sync --match --auto-approve 95 --open-review
```

After reviewing the focused workbook, preview and download only that recent
cohort:

```bash
music-library recent --download --min-score 95 --download-dry-run
music-library recent --download --min-score 95
```

Choose another window with `--days 7` or an exact start date with
`--since 2026-08-01`. Use `--refresh` with `--match` only when existing recent
matches should be searched again. Failed tracks remain checkpointed; add
`--retry-errors` when ready to retry them. Locally deleted and blocked Spotify
IDs are excluded.

## Local Mac Music-library duplicate preference

Create reports comparing downloaded MP3s with audio files installed in your
local macOS Music library (not the Apple Music streaming catalog):

```bash
python apple_music_duplicates.py
```

The script requires an exact normalized title, album, and artist match, plus a
runtime difference of no more than 5 seconds. It creates:

- `data/apple_music_duplicate_report.csv` for exact matches and overall status.
- `data/local_music_close_matches.xlsx` for runtime mismatches and conservative
  close metadata matches. Every row in this workbook is review-only and left
  unchanged.

Review both reports, then apply only the exact safe matches:

```bash
python apple_music_duplicates.py --apply
```

To also import downloads that have no existing Music match:

```bash
python apple_music_duplicates.py --apply --import-new
```

Repeat `--spotify-id ID` to limit either the report-only or `--apply` run to an
exact set of downloaded Spotify tracks. This keeps a focused recent-import run
from reconsidering the rest of the downloaded archive.

The downloaded copy is imported, enabled, and added to the
`Spotify Archive Preferred` playlist. Exactly matching older local copies are
disabled but never deleted. Close matches in the Excel report are not changed.

Each `--apply` creates a restoration plan in SQLite and
`data/apple_music_restore_plans/`. It records old persistent IDs, enabled
states, paths, the target playlist state, and the preferred entry's prior
state:

```bash
python apple_music_duplicates.py --list-plans
python apple_music_duplicates.py --restore-plan PLAN_ID
```

Restore keeps newly imported library entries but disables them, restores prior
enabled states, and reverses only an added playlist reference. It never deletes
a Music library entry or audio file.

The local Music-library comparison is always report-only unless you explicitly
add `--apply`.
`osacompile` validates the helper before any library scan or apply operation.
No duplicate-comparison code path permanently deletes a Music entry or audio
file. The separately documented deletion queue is the only feature allowed to
remove a Music entry.

## Prepare local music for an iPhone

Modern macOS performs iPhone music syncing in Finder; Music's AppleScript
interface does not expose a safe device-sync command. This project can prepare
and audit an additive-only playlist, then you select that playlist in Finder.

Create a report without changing Music:

```bash
music-library iphone
```

This reads `Spotify Archive Preferred` and writes
`data/iphone_sync_manifest.csv`, including readiness, local path, and estimated
file size. To create or update `iPhone Offline - Spotify Archive`:

```bash
music-library iphone --apply --open-finder
```

The command only adds missing playlist references. It never removes a track
from either playlist, changes Finder sync settings, clicks Sync, deletes a
Music entry, or deletes an audio file. In Finder, connect and unlock the
iPhone, select it in the sidebar, open the **Music** tab, choose selected
playlists, and select `iPhone Offline - Spotify Archive`. Review Finder's
storage summary before clicking **Apply** or **Sync**.

To prepare every enabled local file track in the Mac Music library instead of
only the Spotify archive playlist, first run the report and check its estimated
size:

```bash
music-library iphone --all-local
music-library iphone --all-local --apply --open-finder
```

Finder may remove music from the iPhone when its existing sync configuration
is changed. This tool deliberately does not automate that potentially
destructive choice.

## Recreate Spotify playlists in Music

Playlist syncing uses a separate `.spotify_playlist_token.json`, so the
existing liked-songs token is not overwritten. First fetch an immutable
Spotify playlist snapshot and create a report only:

```bash
music-library playlists --sync
```

The first run opens Spotify authorization for `playlist-read-private` and
`playlist-read-collaborative`. It writes
`data/spotify_playlist_music_report.csv` with every playlist position and
whether the downloaded Music copy was found. Spotify URLs are retained in the
report for attribution.

After reviewing the report, create/resume the Music playlists:

```bash
music-library playlists --apply --open-music
```

Music playlist names use the prefix `Spotify - `. Track order is preserved.
Music may refuse duplicate-equivalent library entries; those are skipped while
later songs continue. A resumed run only appends after the existing playlist is
verified as an ordered subsequence of the downloaded Spotify order. If somebody
reordered it incompatibly, the command reports a conflict and leaves it
unchanged. It never deletes a playlist, removes a playlist member, changes a
library entry, or deletes an audio file.

To process only one playlist:

```bash
music-library playlists --sync --playlist "Road Trip"
music-library playlists --apply --playlist "Road Trip" --open-music
```

Only tracks already present in Music with this project's Spotify marker are
added. Unavailable, podcast, Spotify-local, and not-yet-downloaded items remain
in the CSV report. Each `--sync` creates a new immutable SQLite snapshot rather
than overwriting earlier playlist history.

### Create a newest-first liked-songs playlist

Spotify liked songs are not exposed as an ordinary Spotify playlist. This
command uses the liked-song `added_at` order stored by the regular sync and
matches it to project-imported Music entries:

```bash
music-library liked-playlist --sync
```

Review `data/spotify_liked_songs_music_report.csv`, then create the playlist:

```bash
music-library liked-playlist --apply --open-music
```

The target is named `Spotify - Liked Songs`. Only downloaded tracks already in
Music are included, newest liked first. When the desired order changes, the
previous playlist is retained under a unique backup name and a fresh exact-order
playlist is built. No library track or audio file is removed. Use `--name` to
choose another target name.

## Audit and apply Music genres

Scan every local file track and create a report without changing Music:

```bash
music-library genres
```

Review `data/music_genre_report.csv`. Existing genres are preserved by default.
For blank genres, the report uses a single consistent genre already present on
the same Music album, then a single consistent embedded file tag. Albums named
`(VINYL)`, disabled entries, unavailable files, and ambiguous albums are always
skipped.

Optionally cache exact album-and-artist genre matches from Apple's catalog in
resumable batches of 100:

```bash
music-library genres --lookup --batch-size 100
# Repeat until the command says the cache is current, or process all albums:
music-library genres --lookup --all
```

Apple documents an approximate Search API limit of 20 calls per minute, so the
lookup deliberately waits at least three seconds between requests. Results are
cached in SQLite; failed lookups do not block later albums and can be retried
with `--retry-errors`.

After reviewing the CSV, fill only missing genres:

```bash
music-library genres --apply
```

Every applied value is stored as a reversible run. List or restore runs with:

```bash
music-library genres --list-runs
music-library genres --restore-run RUN_ID
```

`--overwrite` is required to replace a nonblank genre. The command never
deletes a Music entry or audio file, and both Python and AppleScript independently
protect albums containing `(VINYL)`.

## Apply Spotify metadata to imported Music entries

Audit only the local Music entries imported by this project:

```bash
music-library metadata
```

The project marker (`SPOTIFY_ARCHIVE_ID`) prevents unrelated Music entries from
being selected. Review `data/music_metadata_report.csv`, then apply and verify
the supported Spotify fields:

```bash
music-library metadata --apply
```

This updates title, track artists, album artist, album, release year, track and
disc numbering, track count, compilation status, and the Music comment. The
comment retains compact Spotify track/album and YouTube IDs, ISRC, explicit
flag, full Spotify release date, and Spotify-library added date within Music's
255-character limit. Existing custom comment text is preserved when space
allows. Spotify currently does not reliably return genre
data, so a blank Spotify genre never erases a Music genre. Album artwork remains
embedded in the downloaded MP3 and is read by Music when the file is imported.

Each apply starts a reversible, verified run:

```bash
music-library metadata --list-runs
music-library metadata --restore-run RUN_ID
```

Albums containing `(VINYL)` are independently protected in both Python and the
Music AppleScript. This stage never deletes, disables, imports, or relocates a
track or audio file.

### Normalize Hadestown artist grouping

If Music shows the Hadestown cast albums as several artists, create a report:

```bash
music-library cleanup-hadestown
```

Review `data/hadestown_artist_cleanup.csv`, then normalize each cast release to
one album artist and mark it as a compilation while preserving every track's
individual performer credits:

```bash
music-library cleanup-hadestown --apply
```

The command changes only album artist and compilation metadata. It never moves
or deletes audio. Restore a run with:

```bash
music-library cleanup-hadestown --list-runs
music-library cleanup-hadestown --restore-run RUN_ID
```

For the same audit across the entire local library:

```bash
music-library cleanup-library-artists
music-library cleanup-library-artists --apply
```

This automatically applies only high-confidence cases: separator/capitalization
variants, multiple runtime-matching copies within one release, and clear
artist-versus-`Various Artists` compilation splits. Ambiguous same-named covers
remain unchanged. Review `data/music_library_consistency_cleanup.csv`; restore
with `music-library cleanup-library-artists --restore-run RUN_ID`.

Create a broader review queue—including ambiguous cases that are intentionally
excluded from automatic cleanup—with:

```bash
music-library audit-library-artists
```

It writes a group summary and track-level detail CSV. Each group includes a
decision tier, evidence score, issue types, duplicate/runtime evidence,
suggested reference metadata, file formats, Spotify coverage, and blank review
decision/notes fields. The audit is always read-only.

## Merge duplicate collaboration artist entries

If the Music app on iPhone or Mac lists one collaboration many times—for
example a library search that shows ten separate `Laufey/Los Angeles
Philharmonic` artist rows—the cause is metadata variance across the tracks
that share the credit: mixed separator styles (`Laufey/Los Angeles
Philharmonic` versus `Laufey; Los Angeles Philharmonic`), album artists that
repeat the full joined credit differently per single, and hidden per-track
sort artist values left behind by earlier imports. Create a report first:

```bash
music-library artist-credits
```

Review `data/music_artist_credit_cleanup.csv`, then normalize:

```bash
music-library artist-credits --apply
```

For every group of tracks whose artist text names the same set of performers,
the cleanup:

- rewrites each variant to one canonical credit, preferring the Spotify
  catalog's `Artist; Artist` text when the collaboration exists there;
- promotes the album artist to the primary performer (for example `Laufey`)
  when the album artist merely repeats the full joined credit, so the songs
  group under the lead artist while track credits keep every performer;
- clears mismatched hidden sort artist and sort album artist values so Music
  derives one consistent sort key.

Single-artist names that contain a separator, such as `AC/DC`, are recognized
from the Spotify catalog and never split; without catalog confirmation an
unvaried credit is left as-is and only sort-field variance is repaired.
Albums marked `(VINYL)` are protected, audio files are never touched, and
each apply is a verified, reversible run:

```bash
music-library artist-credits --list-runs
music-library artist-credits --restore-run RUN_ID
```

After an apply, re-sync the iPhone (or let iCloud Music Library update) so
the merged artist entries collapse on the phone as well.

## Explicit `delete me pls` queue

Add local file tracks you intentionally want removed to a Music playlist named
exactly `delete me pls`. Always create the report first:

```bash
music-library delete-queue
```

For an iPhone-driven workflow, make this playlist visible on the phone, add
unwanted songs to it, allow the playlist change to sync back to the Mac, and
run the equivalent phone-oriented alias:

```bash
music-library phone-delete --create-playlist
music-library phone-delete
```

Run `--create-playlist` once if the queue does not exist yet; it only creates
an empty Music playlist and then writes a zero-item report.

Do not treat **Remove Download** on the phone as a deletion request: it only
frees phone storage. The tool intentionally does not infer deletion from a song
being absent on the device, because Finder sync and storage optimization can
also remove a local phone copy. Only explicit membership in `delete me pls` is
actionable.

Review `data/music_delete_queue_report.csv`. It distinguishes eligible local
files, cloud/non-file entries, shared files, and protected `(VINYL)` albums.
Report mode does not alter Music or the filesystem.

Apply only with the exact second confirmation:

```bash
music-library delete-queue --apply --confirm "delete me pls"
music-library phone-delete --apply --confirm "delete me pls"
```

For eligible entries, Music removes the library entry, which also removes its
references from dependent Music playlists. Unique Music/download files are
moved to macOS Trash, never permanently erased by this command. Shared files
are preserved. SQLite history is retained, and project-imported Spotify IDs are
marked locally deleted so matching, downloads, duplicate replacement, and
playlist recreation do not bring them back after the next Spotify sync.

Audit runs or explicitly allow a Spotify ID again:

```bash
music-library delete-queue --list-runs
music-library delete-queue --unblock SPOTIFY_ID
```

Unblocking does not restore the Music entry or file automatically; recover a
trashed file through Finder first if wanted. The tool never empties Trash.

### Environment or dependency errors

Run:

```bash
python music_library.py doctor
```

It checks whether this project's virtual environment is in use, required
Python packages and commands are available, Spotify's Client ID is configured,
and the AppleScript compiles. It does not access or modify the Music library.

## Troubleshooting

### Spotify says the redirect URI is invalid

Use the exact IP address, port, and path:
`http://127.0.0.1:8888/callback`. Spotify no longer allows `localhost` for this
flow.

### Spotify returns 403

Check that the app owner has Spotify Premium and that the Spotify account is
listed in the app's Users Management allowlist.

### YouTube asks for sign-in or reports bot detection

Update `yt-dlp` first:

```bash
python -m pip install --upgrade yt-dlp
```

YouTube changes often. Do not paste account cookies or passwords into this
project.

For large no-cookie runs, initialize and build the pinned PO-token provider.
Node.js 20 or newer is required for this optional step:

```bash
git submodule update --init --recursive
cd tools/bgutil-ytdlp-pot-provider/server
npm ci
npx tsc
cd ../../..
```

Then start it in a separate Terminal window:

```bash
bash start_po_token_provider.sh
```

Test one retry with no delay:

```bash
music-library download --min-score 95 --retry-errors --limit 1 \
  --po-token-provider --sleep-min-seconds 0 --sleep-max-seconds 0
```

Only if that succeeds, resume the full queue with rate limiting, persisted
adaptive cooldowns, and a final authentication circuit breaker:

```bash
music-library download --min-score 95 --all --retry-errors \
  --po-token-provider --sleep-min-seconds 20 --sleep-max-seconds 30 \
  --auth-cooldown-min-seconds 600 --auth-cooldown-max-seconds 3600 \
  --auth-retries-per-track 3 --max-consecutive-auth-errors 12
```

To retry only failed score-95+ tracks before untouched downloads, run:

```bash
music-library download --min-score 95 --all --retry-errors --errors-only \
  --po-token-provider --sleep-min-seconds 20 --sleep-max-seconds 30 \
  --auth-cooldown-min-seconds 600 --auth-cooldown-max-seconds 3600 \
  --auth-retries-per-track 3 --max-consecutive-auth-errors 12
```

With authentication cooldown enabled, the downloader stores the cooldown in
SQLite, waits 10 minutes after the first bot check, doubles later consecutive
waits up to one hour, and requeues the affected track. A final ceiling of 12
consecutive authentication failures still stops a persistently blocked run
without marking the rest of the library failed.

### FFmpeg is missing

```bash
brew install ffmpeg
```

## Responsible use

Download only audio you own or have permission to save, and follow the terms
and copyright rules that apply to the media and your location. This tool reads
Spotify metadata only. It does not download Spotify audio.

Official references:

- [Spotify saved tracks API](https://developer.spotify.com/documentation/web-api/reference/get-users-saved-tracks)
- [Spotify PKCE authorization](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow)
- [Spotify redirect URI rules](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)
- [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp)
