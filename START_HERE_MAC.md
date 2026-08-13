# Start-to-finish Mac instructions

These instructions use the current project location.

## 1. Extract and open the project

Open Terminal and run:

```bash
cd "/Users/ryleypriddy/Documents/spotify-youtube-library-mac"
```

If Finder gave the folder a different name, type `cd ` with a space, drag the
folder into Terminal, and press Return.

## 2. Install the requirements

Install Homebrew from [brew.sh](https://brew.sh/) if you do not already have
it. Then run:

```bash
bash setup_mac.command
```

Keep Terminal open when it finishes.
Setup also installs the `music-library` command while preserving the original
scripts.

## 3. Create your personal Spotify app

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Sign in and select **Create app**.
3. Use any name, such as `My Music Archive`.
4. Add this exact Redirect URI:
   `http://127.0.0.1:8888/callback`
5. Save the app and copy its **Client ID**.

Back in Terminal, run:

```bash
nano .env
```

Replace `paste_your_client_id_here` with your Client ID. Do not add a Client
Secret. Press `Control+O`, Return, then `Control+X`.

## 4. Activate the project

Run this whenever you open a new Terminal window:

```bash
cd "/Users/ryleypriddy/Documents/spotify-youtube-library-mac"
source .venv/bin/activate
```

You should see `(.venv)` at the beginning of the Terminal line.

## 5. Copy your Spotify library into the database

```bash
python spotify_sync.py
```

To include all tracks from albums saved in **Your Library** too:

```bash
music-library sync --include-albums
```

Use `--include-albums` on later syncs to keep those album tracks active.
Liked/album duplicates are stored once, and no existing downloads are deleted.

Spotify opens in your browser. Approve read-only access and return to Terminal.

This creates:

- `data/music_library.sqlite`
- `data/music_library.csv`
- `data/music_library_review.xlsx`

## 6. Test YouTube matching on 20 songs

```bash
python youtube_match.py --limit 20
```

The top eight results receive a full metadata lookup before final scoring.

Open `data/music_library_review.xlsx`. The suggested source should normally be
an artist Topic upload, official audio, or official lyric video. The runtime is
compared with Spotify and strongly affects the score.

If the test looks good, match the rest:

```bash
python youtube_match.py
```

You can stop with `Control+C`. Running the same command later continues with
unsearched songs.

To safely recheck one specific song, copy its Spotify ID from the workbook:

```bash
music-library match --spotify-id SPOTIFY_ID --auto-approve 95
```

You can repeat `--spotify-id` for several songs. This targets only those rows,
even if they were already searched.

To start YouTube matching over, preview the affected rows first:

```bash
music-library reset-matches
```

If the totals are correct, apply the reset:

```bash
music-library reset-matches --apply
```

This makes a full database backup and protects completed downloads by default.
It does not delete MP3s, Spotify metadata, matching history, or local Music
data.

## 7. Approve YouTube matches

In the Excel workbook's `Tracks` sheet:

- Enter `approve` to use the suggested YouTube source.
- Enter `skip` to exclude the song.
- Paste a different link in `manual_youtube_url`, then enter `approve`.

Save and close the workbook. Run:

```bash
python import_review.py
```

For conservative automatic approval:

```bash
python youtube_match.py --auto-approve 95
```

Automatic approval also requires a trusted Topic/official/verified-artist
source and a YouTube runtime within 5 seconds of Spotify. A high score by
itself is not enough.

## 8. Download the next 100 songs

Preview:

```bash
python download_mp3.py --dry-run
```

Download only current-policy matches scoring at least 95:

```bash
python download_mp3.py --min-score 95
```

This includes previously searched suggestions and does not require
`youtube_match.py --refresh`.

Each run processes the next 100 unfinished songs and saves its checkpoint after
every track. Run it again for the next 100.

```bash
python download_mp3.py --status
```

Failed songs are set aside. Retry them later with:

```bash
python download_mp3.py --retry-errors
```

To honor scheduled backoff and retry only failures that are due:

```bash
python download_mp3.py --retry-due
python youtube_match.py --retry-due
```

The completed MP3 itself must also be within 5 seconds of Spotify before it is
tagged and marked complete.

Pending tracks run before retried failures, so failed tracks do not block later
songs.

## 9. Compare with your local Mac Music library

Quit Excel and leave the Music app available. This scans audio files installed
in the macOS Music app, not the Apple Music streaming catalog. First create
reports only:

```bash
python apple_music_duplicates.py
```

The first run asks whether Terminal can control Music. Select **Allow**. If it
does not ask, open **System Settings > Privacy & Security > Automation** and
allow Terminal to control Music.

Open:

`data/apple_music_duplicate_report.csv`

Also open:

`data/local_music_close_matches.xlsx`

The Excel workbook contains possible duplicates that are close but not exact.
They are always left unchanged for manual review.

An automatic replacement requires:

- Exact normalized title
- Exact normalized album
- Exact normalized artist
- Runtime within 5 seconds

When the report looks correct, apply the duplicate preferences:

```bash
python apple_music_duplicates.py --apply
```

For each eligible duplicate, this:

1. Imports the newly downloaded MP3.
2. Enables the new copy.
3. Disables the older matching Music copy.
4. Adds the new copy to `Spotify Archive Preferred`.
5. Keeps the old entry and file so the change is reversible.

Only exact normalized title, album, and artist matches with runtime within 5
seconds are eligible. The downloaded copy becomes preferred; close matches are
never disabled automatically.

To import downloaded songs that are not already in Music while also preferring
new files over exact duplicates:

```bash
python apple_music_duplicates.py --apply --import-new
```

The script never deletes an old Music entry or audio file.

Every apply prints a restore-plan ID:

```bash
python apple_music_duplicates.py --list-plans
python apple_music_duplicates.py --restore-plan PLAN_ID
```

Restore keeps newly imported entries but disables them and restores previous
enabled states and preferred-playlist membership.

## One-command guide and totals

The individual commands above remain supported. You can also use:

```bash
python music_library.py doctor
python music_library.py guide
python music_library.py status
```

The installed equivalents are:

```bash
music-library doctor
music-library guide
music-library status
music-library history --limit 50
```

`doctor` checks the virtual environment, dependencies, Spotify configuration,
and AppleScript compilation without reading or changing Music. `status` reports
synced, matched, approved, downloaded, failed, and added-to-Apple-Music totals.
Matching history is retained in SQLite, structured events are appended to
`data/activity.jsonl`, and previous CSV/Excel exports are backed up under
`data/backups/`.

## 10. Prepare an additive-only iPhone sync playlist

First create a report only:

```bash
music-library iphone
```

Review `data/iphone_sync_manifest.csv`, especially `ready`, `reason`, and the
printed estimated size. Then create/update the playlist and open Finder:

```bash
music-library iphone --apply --open-finder
```

Connect and unlock the iPhone. In Finder, select the iPhone, choose **Music >
Selected artists, albums, genres, and playlists**, select `iPhone Offline -
Spotify Archive`, review the storage summary, and click **Apply** or **Sync**.

For all enabled local file tracks rather than only `Spotify Archive Preferred`:

```bash
music-library iphone --all-local
music-library iphone --all-local --apply --open-finder
```

The script never changes Finder's sync settings, clicks Sync, removes playlist
members, deletes Music entries, or deletes audio files. Finder can remove
device music when you change its sync selection, which is why that final choice
remains manual.

## 11. Recreate Spotify playlists with downloaded Music tracks

Fetch a fresh immutable snapshot and make a report without changing Music:

```bash
music-library playlists --sync
```

The first run requests Spotify playlist-read permission in a browser and saves
it separately at `data/.spotify_playlist_token.json`. Your existing Spotify
liked-song token is preserved. Review:

`data/spotify_playlist_music_report.csv`

Then create or safely resume the prefixed Music playlists:

```bash
music-library playlists --apply --open-music
```

The Music playlists are named `Spotify - PLAYLIST NAME`. Only downloaded tracks
found by Spotify ID are added, in Spotify order. Music can refuse
duplicate-equivalent entries; those are skipped without blocking later songs.
Existing playlists are resumed only if their entries remain an ordered
subsequence of the desired order. Conflicts and unavailable tracks are reported
and left unchanged; nothing is removed or deleted.

Limit either step to one playlist with `--playlist "PLAYLIST NAME"`.

## 12. Audit and fill genres on local Music files

Create a report without changing Music:

```bash
music-library genres
```

Review `data/music_genre_report.csv`. Existing genres stay unchanged. Missing
genres are proposed only from an unambiguous genre already used on the same
Music album or embedded consistently in that album's files. `(VINYL)` albums,
disabled entries, missing files, and ambiguous evidence are skipped.

For remaining blank albums, look up exact album-and-artist matches in Apple's
catalog. The cache and 100-album default batch make this resumable:

```bash
music-library genres --lookup --batch-size 100
# Repeat, or use --all for the full remaining catalog lookup:
music-library genres --lookup --all
```

After reviewing the updated CSV, apply only the proposed blank genres:

```bash
music-library genres --apply
```

List the reversible apply runs or restore one:

```bash
music-library genres --list-runs
music-library genres --restore-run RUN_ID
```

Use `--retry-errors` to retry failed catalog requests. Replacing an existing
genre requires the explicit `--overwrite` option. Nothing in this stage deletes
a Music entry or audio file.

## 13. Apply Spotify metadata to imported Music entries

Create a report for only entries carrying this project's Spotify marker:

```bash
music-library metadata
```

Review `data/music_metadata_report.csv`, then apply the supported fields:

```bash
music-library metadata --apply
```

This verifies title, track artists, album artist, album, release year, track and
disc numbering, track count, compilation status, and a structured source
comment containing compact Spotify track/album and YouTube IDs, ISRC, explicit status,
full release date, and Spotify-added date. Existing genres are preserved when
Spotify has no genre value. Embedded MP3 artwork is left intact.

List or restore the exact before-state from an apply run:

```bash
music-library metadata --list-runs
music-library metadata --restore-run RUN_ID
```

Unrelated tracks and `(VINYL)` albums are never changed, and no entry or file is
deleted, disabled, imported, or moved.

## Optional: download a standalone video as MOV

This command does not use the Spotify catalog. It downloads a video URL with
`yt-dlp` and converts it to a QuickTime-compatible H.264/AAC `.mov` file:

```bash
music-library video "https://www.youtube.com/watch?v=VIDEO_ID" --dry-run
music-library video "https://www.youtube.com/watch?v=VIDEO_ID"
```

The default destination is `videos/`, the default limit is 1080p, partial
downloads resume, and completed IDs are checkpointed. Use `--max-height 0` for
the best available resolution, `--output PATH` for a different folder, or
`--playlist` to explicitly download all items from a playlist URL.

## 14. Review only recent Spotify additions

Refresh liked songs and saved albums, score recent additions, and open a focused
30-day workbook:

```bash
music-library recent --sync --match --auto-approve 95 --open-review
```

Then preview and download only that recent cohort:

```bash
music-library recent --download --min-score 95 --download-dry-run
music-library recent --download --min-score 95
```

Use `--days 7` or `--since YYYY-MM-DD` to change the window.

## 15. Create the ordered liked-songs playlist

Create the report, then build `Spotify - Liked Songs` newest-first:

```bash
music-library liked-playlist --sync
music-library liked-playlist --apply --open-music
```

Only downloaded local entries are included. If rebuilding is necessary, the
old playlist is retained under a backup name.

## 16. Explicit deletion queue

Place intentionally unwanted local files in the Music playlist `delete me pls`,
then run the report only:

```bash
music-library delete-queue
```

After reviewing `data/music_delete_queue_report.csv`, deletion requires both
flags:

```bash
music-library delete-queue --apply --confirm "delete me pls"
```

This removes eligible Music entries and dependent playlist references, moves
unique files to macOS Trash, protects shared and `(VINYL)` files, retains audit
history, and blocks the Spotify IDs from automatic re-download. Testing never
runs this apply command.

## Optional: consolidate Hadestown artist entries

```bash
music-library cleanup-hadestown
music-library cleanup-hadestown --apply
```

This preserves track performer credits and audio files, but gives each cast
release one album artist and checks the compilation flag. The CSV report and
SQLite restore run make the change reversible.

Audit and normalize the same high-confidence grouping issue library-wide:

```bash
music-library cleanup-library-artists
music-library cleanup-library-artists --apply
```

Ambiguous same-named songs and covers remain report-only and unchanged.

For a detailed manual-review queue with the exact supporting tracks:

```bash
music-library audit-library-artists
```

This audit never applies changes.

## Normal repeat workflow

```bash
source .venv/bin/activate
music-library sync --include-albums
music-library match --auto-approve 95
music-library review
music-library download --min-score 95 --all
music-library local-music
music-library local-music --apply --import-new
music-library liked-playlist --apply
music-library status
```

Always review the exact-match CSV and close-match Excel reports before applying
changes.

If YouTube reports a bot/automation check, start the local no-cookie provider
with `bash start_po_token_provider.sh`, test one retry, and then use:

```bash
music-library download --min-score 95 --all --retry-errors \
  --po-token-provider --sleep-min-seconds 20 --sleep-max-seconds 30 \
  --auth-cooldown-min-seconds 600 --auth-cooldown-max-seconds 3600 \
  --auth-retries-per-track 3 --max-consecutive-auth-errors 12
```

The downloader stores the cooldown in SQLite, waits it out, and requeues the
affected track.
