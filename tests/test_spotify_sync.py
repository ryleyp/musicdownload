from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from common import AppError, connect_db
from library_status import progress_counts
from spotify_sync import fetch_saved_album_tracks, upsert_tracks


def spotify_track(spotify_id: str, album_id: str = "album1") -> dict[str, Any]:
    return {
        "id": spotify_id,
        "name": f"Song {spotify_id}",
        "artists": [{"id": "artist1", "name": "Test Artist"}],
        "album": {
            "id": album_id,
            "name": "Test Album",
            "artists": [{"id": "artist1", "name": "Test Artist"}],
            "images": [],
            "release_date": "2025-01-01",
            "total_tracks": 2,
            "external_urls": {"spotify": f"https://spotify.test/{album_id}"},
        },
        "duration_ms": 180000,
        "disc_number": 1,
        "track_number": 1,
        "external_ids": {"isrc": f"ISRC{spotify_id}"},
        "external_urls": {"spotify": f"https://spotify.test/{spotify_id}"},
    }


class SavedAlbumSyncTests(unittest.TestCase):
    def test_album_membership_deduplicates_liked_track_and_preserves_liked_date(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "music.sqlite"
            with connect_db(db) as connection:
                upsert_tracks(
                    connection,
                    [{"added_at": "2026-01-02", "track": spotify_track("both")}],
                )
                upsert_tracks(
                    connection,
                    [
                        {"added_at": "2025-01-01", "track": spotify_track("both")},
                        {
                            "added_at": "2025-01-01",
                            "track": spotify_track("album-only"),
                        },
                    ],
                    membership_column="is_saved_album",
                    label="saved-album tracks",
                )
                rows = {
                    row["spotify_id"]: row
                    for row in connection.execute("SELECT * FROM tracks")
                }
                counts = progress_counts(connection)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows["both"]["is_liked"], 1)
            self.assertEqual(rows["both"]["is_saved_album"], 1)
            self.assertEqual(rows["both"]["added_at"], "2026-01-02")
            self.assertEqual(rows["album-only"]["is_liked"], 0)
            self.assertEqual(rows["album-only"]["is_saved_album"], 1)
            self.assertEqual(counts["synced"], 2)
            self.assertEqual(counts["liked"], 1)
            self.assertEqual(counts["saved_album_tracks"], 2)
            self.assertEqual(counts["saved_albums"], 1)

    def test_saved_album_pagination_and_full_track_hydration(self) -> None:
        class FakeClient:
            def get(
                self, url: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                if url == "/me/albums":
                    return {
                        "items": [
                            {
                                "added_at": "2026-01-01",
                                "album": {
                                    "id": "album1",
                                    "name": "Test Album",
                                    "tracks": {
                                        "items": [{"id": "one", "name": "One"}],
                                        "next": "next-tracks",
                                    },
                                },
                            }
                        ],
                        "total": 1,
                        "next": None,
                    }
                if url == "next-tracks":
                    return {
                        "items": [{"id": "two", "name": "Two"}],
                        "next": None,
                    }
                if url == "/tracks":
                    self.requested_ids = params["ids"] if params else ""
                    return {"tracks": [spotify_track("one"), spotify_track("two")]}
                raise AssertionError(f"Unexpected URL: {url}")

        client = FakeClient()
        tracks, album_count = fetch_saved_album_tracks(client)  # type: ignore[arg-type]
        self.assertEqual(album_count, 1)
        self.assertEqual({item["track"]["id"] for item in tracks}, {"one", "two"})
        self.assertEqual(client.requested_ids, "one,two")

    def test_restricted_track_hydration_keeps_embedded_album_tracks(self) -> None:
        class RestrictedClient:
            def get(
                self, url: str, params: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                if url == "/me/albums":
                    return {
                        "items": [
                            {
                                "added_at": "2026-01-01",
                                "album": {
                                    "id": "album1",
                                    "name": "Test Album",
                                    "artists": [
                                        {"id": "artist1", "name": "Test Artist"}
                                    ],
                                    "images": [],
                                    "release_date": "2025-01-01",
                                    "total_tracks": 1,
                                    "tracks": {
                                        "items": [
                                            {
                                                "id": "one",
                                                "name": "One",
                                                "artists": [
                                                    {
                                                        "id": "artist1",
                                                        "name": "Test Artist",
                                                    }
                                                ],
                                                "duration_ms": 180000,
                                                "disc_number": 1,
                                                "track_number": 1,
                                            }
                                        ],
                                        "next": None,
                                    },
                                },
                            }
                        ],
                        "total": 1,
                        "next": None,
                    }
                if url == "/tracks":
                    raise AppError("Spotify returned 403 Forbidden")
                raise AssertionError(f"Unexpected URL: {url}")

        tracks, album_count = fetch_saved_album_tracks(  # type: ignore[arg-type]
            RestrictedClient()
        )
        self.assertEqual(album_count, 1)
        self.assertEqual(tracks[0]["track"]["id"], "one")
        self.assertEqual(tracks[0]["track"]["album"]["id"], "album1")


if __name__ == "__main__":
    unittest.main()
