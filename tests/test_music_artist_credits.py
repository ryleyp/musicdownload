from __future__ import annotations

import unittest
from unittest.mock import patch

import music_artist_credits
from music_artist_credits import (
    build_plan,
    canonical_artist,
    sort_target,
    split_credits,
)
from music_library_consistency import credit_parts


def music_track(**values):
    defaults = {
        "persistent_id": "PID1",
        "title": "From The Start",
        "artist": "Laufey/Los Angeles Philharmonic",
        "album_artist": "Laufey/Los Angeles Philharmonic",
        "sort_artist": "",
        "sort_album_artist": "",
        "album": "A Night At The Symphony",
        "compilation": True,
        "comment": "",
    }
    defaults.update(values)
    return defaults


LAUFEY_KEY = credit_parts("Laufey/Los Angeles Philharmonic")
LAUFEY_CATALOG = {
    LAUFEY_KEY: [("Laufey; Los Angeles Philharmonic", "Laufey")] * 3,
}


class SplitCreditTests(unittest.TestCase):
    def test_split_credits_handles_every_separator(self) -> None:
        self.assertEqual(
            split_credits("Laufey / Los Angeles Philharmonic"),
            ["Laufey", "Los Angeles Philharmonic"],
        )
        self.assertEqual(split_credits("Laufey; dodie"), ["Laufey", "dodie"])
        self.assertEqual(split_credits("Laufey|dodie"), ["Laufey", "dodie"])

    def test_split_credits_keeps_ampersand_and_comma_names_whole(self) -> None:
        self.assertEqual(split_credits("Simon & Garfunkel"), ["Simon & Garfunkel"])
        self.assertEqual(
            split_credits("Crosby, Stills & Nash"), ["Crosby, Stills & Nash"]
        )


class CanonicalArtistTests(unittest.TestCase):
    def test_spotify_collaboration_credit_wins(self) -> None:
        items = [music_track(), music_track(persistent_id="PID2")]
        artist, primary, source = canonical_artist(items, LAUFEY_KEY, LAUFEY_CATALOG)
        self.assertEqual(artist, "Laufey; Los Angeles Philharmonic")
        self.assertEqual(primary, "Laufey")
        self.assertEqual(source, "Spotify track credit")

    def test_spotify_single_artist_name_is_never_split(self) -> None:
        key = credit_parts("AC/DC")
        catalog = {key: [("AC/DC", "AC/DC")] * 5}
        items = [music_track(artist="AC/DC", album_artist="AC/DC")]
        artist, primary, source = canonical_artist(items, key, catalog)
        self.assertEqual(artist, "AC/DC")
        self.assertEqual(primary, "AC/DC")
        self.assertEqual(source, "Spotify single-artist name")

    def test_mixed_separator_variants_prove_a_collaboration(self) -> None:
        items = [
            music_track(artist="Laufey/dodie"),
            music_track(persistent_id="PID2", artist="Laufey/dodie"),
            music_track(persistent_id="PID3", artist="Laufey; dodie"),
        ]
        artist, primary, source = canonical_artist(
            items, credit_parts("Laufey/dodie"), {}
        )
        self.assertEqual(artist, "Laufey; dodie")
        self.assertEqual(primary, "Laufey")
        self.assertEqual(source, "dominant Music credit variant")

    def test_uniform_unknown_credit_is_left_untouched(self) -> None:
        items = [music_track(artist="AC/DC", album_artist="AC/DC")]
        artist, primary, source = canonical_artist(items, credit_parts("AC/DC"), {})
        self.assertEqual(artist, "AC/DC")
        self.assertEqual(primary, "")
        self.assertEqual(source, "unconfirmed credit")

    def test_solo_case_variants_unify_to_dominant_text(self) -> None:
        items = [
            music_track(artist="Laufey"),
            music_track(persistent_id="PID2", artist="Laufey"),
            music_track(persistent_id="PID3", artist="laufey"),
        ]
        artist, primary, source = canonical_artist(items, credit_parts("Laufey"), {})
        self.assertEqual(artist, "Laufey")
        self.assertEqual(primary, "Laufey")
        self.assertEqual(source, "dominant Music credit variant")

    def test_solo_spotify_name_is_canonical(self) -> None:
        key = credit_parts("dodie")
        catalog = {key: [("dodie", "dodie")] * 4}
        artist, primary, source = canonical_artist(
            [music_track(artist="Dodie")], key, catalog
        )
        self.assertEqual(artist, "dodie")
        self.assertEqual(primary, "dodie")
        self.assertEqual(source, "Spotify single-artist name")


class SortTargetTests(unittest.TestCase):
    def test_uniform_sort_value_is_kept(self) -> None:
        self.assertEqual(
            sort_target(["Laufey", "Laufey"], LAUFEY_KEY, "Laufey; Los Angeles Philharmonic"),
            "Laufey",
        )

    def test_mixed_sort_values_unify_to_the_dominant_value(self) -> None:
        self.assertEqual(
            sort_target(
                ["Laufey", "Laufey", ""], LAUFEY_KEY, "Laufey; Los Angeles Philharmonic"
            ),
            "Laufey",
        )

    def test_stale_credit_variant_sort_value_is_cleared(self) -> None:
        self.assertEqual(
            sort_target(
                ["Laufey/Los Angeles Philharmonic"] * 2,
                LAUFEY_KEY,
                "Laufey; Los Angeles Philharmonic",
            ),
            "",
        )


class BuildPlanTests(unittest.TestCase):
    def test_consistent_solo_artist_produces_no_rows(self) -> None:
        rows, groups = build_plan([music_track(artist="Laufey", album_artist="Laufey")], {})
        self.assertEqual(rows, [])
        self.assertEqual(groups, 0)

    def test_solo_name_variants_are_unified(self) -> None:
        tracks = [
            music_track(artist="Laufey", album_artist="Laufey"),
            music_track(persistent_id="PID2", artist="Laufey", album_artist="Laufey"),
            music_track(persistent_id="PID3", artist="laufey", album_artist="laufey"),
        ]
        rows, groups = build_plan(tracks, {})
        self.assertEqual(groups, 1)
        by_id = {row["music_persistent_id"]: row for row in rows}
        self.assertEqual(by_id["PID3"]["action"], "would_update")
        self.assertEqual(by_id["PID3"]["new_artist"], "Laufey")
        self.assertEqual(by_id["PID3"]["new_album_artist"], "Laufey")
        self.assertEqual(by_id["PID1"]["action"], "current")

    def test_collaboration_credit_and_album_artist_are_normalized(self) -> None:
        tracks = [
            music_track(),
            music_track(persistent_id="PID2", title="Falling Behind", sort_artist="Laufey"),
        ]
        rows, groups = build_plan(tracks, LAUFEY_CATALOG)
        self.assertEqual(groups, 1)
        self.assertEqual({row["action"] for row in rows}, {"would_update"})
        for row in rows:
            self.assertEqual(row["new_artist"], "Laufey; Los Angeles Philharmonic")
            self.assertEqual(row["new_album_artist"], "Laufey")
            self.assertEqual(row["new_sort_artist"], "Laufey")
            self.assertEqual(row["canonical_source"], "Spotify track credit")

    def test_unrelated_album_artist_is_preserved(self) -> None:
        rows, _ = build_plan([music_track(album_artist="Various Artists")], LAUFEY_CATALOG)
        self.assertEqual(rows[0]["new_album_artist"], "Various Artists")

    def test_unconfirmed_uniform_credit_only_fixes_sort_variance(self) -> None:
        tracks = [
            music_track(artist="AC/DC", album_artist="AC/DC", sort_artist="AC/DC"),
            music_track(
                persistent_id="PID2", artist="AC/DC", album_artist="AC/DC", sort_artist=""
            ),
        ]
        rows, groups = build_plan(tracks, {})
        self.assertEqual(groups, 1)
        for row in rows:
            self.assertEqual(row["new_artist"], "AC/DC")
            self.assertEqual(row["new_album_artist"], "AC/DC")
            self.assertEqual(row["new_sort_artist"], "")

    def test_already_consistent_group_is_omitted_from_the_report(self) -> None:
        tracks = [
            music_track(artist="AC/DC", album_artist="AC/DC", sort_artist="AC/DC"),
            music_track(
                persistent_id="PID2", artist="AC/DC", album_artist="AC/DC", sort_artist="AC/DC"
            ),
        ]
        rows, groups = build_plan(tracks, {})
        self.assertEqual(groups, 0)
        self.assertEqual(rows, [])

    def test_vinyl_albums_are_protected(self) -> None:
        rows, groups = build_plan(
            [music_track(album="A Night At The Symphony (VINYL)")], LAUFEY_CATALOG
        )
        self.assertEqual(rows[0]["action"], "protected_vinyl")
        self.assertEqual(groups, 0)


class ScanParsingTests(unittest.TestCase):
    def test_scan_parses_bridge_records(self) -> None:
        record = "\x1f".join([
            "PID1", "From The Start", "Laufey/Los Angeles Philharmonic",
            "Laufey", "Laufey", "Laufey", "A Night At The Symphony",
            "true", "SPOTIFY_ARCHIVE_ID=spotify1",
        ])
        with patch.object(
            music_artist_credits, "run_bridge", return_value=record + "\x1e"
        ):
            tracks = music_artist_credits.scan_artist_credits()
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["artist"], "Laufey/Los Angeles Philharmonic")
        self.assertTrue(tracks[0]["compilation"])


if __name__ == "__main__":
    unittest.main()
