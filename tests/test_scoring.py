from __future__ import annotations

import unittest

from youtube_match import automatic_approval_eligible, candidate_score
from apple_music_duplicates import metadata_key, normalize


TRACK = {
    "title": "Running Up That Hill (A Deal with God)",
    "primary_artist": "Kate Bush",
    "duration_ms": 300000,
}


class ScoringTests(unittest.TestCase):
    def test_topic_audio_wins(self) -> None:
        candidate = {
            "title": "Running Up That Hill (A Deal With God)",
            "channel": "Kate Bush - Topic",
            "duration": 300,
        }
        score, notes, hard_reject = candidate_score(TRACK, candidate)
        self.assertGreaterEqual(score, 100)
        self.assertFalse(hard_reject)
        self.assertTrue(any("Topic" in note for note in notes))

    def test_music_video_is_rejected(self) -> None:
        candidate = {
            "title": "Kate Bush - Running Up That Hill - Official Music Video",
            "channel": "KateBushMusic",
            "duration": 297,
        }
        score, _notes, hard_reject = candidate_score(TRACK, candidate)
        self.assertLess(score, 80)
        self.assertTrue(hard_reject)

    def test_hd_and_promo_music_video_labels_are_rejected(self) -> None:
        for title in (
            "Running Up That Hill (Official HD Video)",
            "Running Up That Hill (Official 4K Video)",
            "Running Up That Hill (Official Promo Video)",
            "Running Up That Hill (Music Vid)",
        ):
            with self.subTest(title=title):
                _score, _notes, hard_reject = candidate_score(
                    TRACK,
                    {
                        "title": title,
                        "channel": "KateBushMusic",
                        "duration": 300,
                    },
                )
                self.assertTrue(hard_reject)

    def test_generic_video_labels_are_rejected_but_lyric_video_is_allowed(self) -> None:
        for title in (
            "Running Up That Hill (Video)",
            "Running Up That Hill (Official Visual Video)",
        ):
            with self.subTest(title=title):
                _score, _notes, hard_reject = candidate_score(
                    TRACK,
                    {
                        "title": title,
                        "channel": "KateBushMusic",
                        "duration": 300,
                    },
                )
                self.assertTrue(hard_reject)

        _score, _notes, hard_reject = candidate_score(
            TRACK,
            {
                "title": "Running Up That Hill (Official Lyric Video)",
                "channel": "KateBushMusic",
                "duration": 300,
            },
        )
        self.assertFalse(hard_reject)

    def test_live_version_is_rejected(self) -> None:
        candidate = {
            "title": "Running Up That Hill (Live at the Hammersmith Odeon)",
            "channel": "Kate Bush",
            "duration": 301,
        }
        _score, _notes, hard_reject = candidate_score(TRACK, candidate)
        self.assertTrue(hard_reject)

    def test_score_alone_cannot_approve_runtime_outside_gate(self) -> None:
        candidate = {
            "title": "Running Up That Hill (A Deal With God) (Official Audio)",
            "channel": "Kate Bush - Topic",
            "duration": 306,
        }
        score, _notes, hard_reject = candidate_score(TRACK, candidate)
        self.assertGreaterEqual(score, 95)
        eligible, reason = automatic_approval_eligible(
            TRACK, candidate, score, hard_reject, 95
        )
        self.assertFalse(eligible)
        self.assertIn("runtime differs", reason)

    def test_missing_runtime_cannot_be_automatically_approved(self) -> None:
        candidate = {
            "title": "Running Up That Hill (A Deal With God) (Official Audio)",
            "channel": "Kate Bush",
            "duration": None,
        }
        score, _notes, hard_reject = candidate_score(TRACK, candidate)
        eligible, reason = automatic_approval_eligible(
            TRACK, candidate, score, hard_reject, 50
        )
        self.assertFalse(eligible)
        self.assertIn("runtime is unavailable", reason)

    def test_unrelated_channel_cannot_self_label_as_official(self) -> None:
        candidate = {
            "title": "Kate Bush - Running Up That Hill (Official Audio)",
            "channel": "Random Uploads",
            "duration": 300,
        }
        score, _notes, hard_reject = candidate_score(TRACK, candidate)
        eligible, reason = automatic_approval_eligible(
            TRACK, candidate, score, hard_reject, 80
        )
        self.assertFalse(eligible)
        self.assertIn("artist Topic channel", reason)

    def test_compact_official_artist_channel_is_recognized(self) -> None:
        candidate = {
            "title": "Kate Bush - Running Up That Hill (Official Audio)",
            "channel": "KateBushMusic",
            "duration": 300,
        }
        score, _notes, hard_reject = candidate_score(TRACK, candidate)
        eligible, _reason = automatic_approval_eligible(
            TRACK, candidate, score, hard_reject, 50
        )
        self.assertTrue(eligible)

    def test_fan_channel_with_artist_name_is_not_trusted(self) -> None:
        candidate = {
            "title": "Running Up That Hill (Official Audio)",
            "channel": "Kate_Bush_Forever",
            "duration": 300,
        }
        score, _notes, hard_reject = candidate_score(TRACK, candidate)
        eligible, reason = automatic_approval_eligible(
            TRACK, candidate, score, hard_reject, 50
        )
        self.assertFalse(eligible)
        self.assertIn("source is not", reason)
        self.assertLess(score, 95)

    def test_spotify_named_exact_version_is_not_rejected(self) -> None:
        track = dict(TRACK, title="Running Up That Hill (Live)")
        candidate = {
            "title": "Running Up That Hill (Live)",
            "channel": "Kate Bush - Topic",
            "duration": 300,
        }
        _score, _notes, hard_reject = candidate_score(track, candidate)
        self.assertFalse(hard_reject)

    def test_spotify_named_version_rejects_wrong_official_master(self) -> None:
        track = {
            "title": "Dear John (Taylor's Version)",
            "primary_artist": "Taylor Swift",
            "album": "Speak Now (Taylor's Version)",
            "duration_ms": 405000,
        }
        original = {
            "title": "Dear John",
            "track": "Dear John",
            "album": "Speak Now",
            "channel": "Taylor Swift - Topic",
            "duration": 403,
        }
        _score, notes, hard_reject = candidate_score(track, original)
        self.assertTrue(hard_reject)
        self.assertTrue(any("required taylor s version" in note for note in notes))

    def test_spotify_named_version_accepts_matching_master(self) -> None:
        track = {
            "title": "Dear John (Taylor's Version)",
            "primary_artist": "Taylor Swift",
            "album": "Speak Now (Taylor's Version)",
            "duration_ms": 405000,
        }
        rerecording = {
            "title": "Dear John (Taylor's Version) (Lyric Video)",
            "channel": "Taylor Swift",
            "channel_is_verified": True,
            "duration": 405,
        }
        score, _notes, hard_reject = candidate_score(track, rerecording)
        self.assertFalse(hard_reject)
        self.assertGreaterEqual(score, 95)
        eligible, _reason = automatic_approval_eligible(
            track, rerecording, score, hard_reject, 95
        )
        self.assertTrue(eligible)

    def test_shared_remaster_metadata_cannot_hide_wrong_core_title(self) -> None:
        track = {
            "title": "Today - 2011 Remaster",
            "primary_artist": "The Smashing Pumpkins",
            "album": "Siamese Dream (2011 Remaster)",
            "duration_ms": 201000,
        }
        wrong_song = {
            "title": "Luna (2011 Remaster)",
            "track": "Today",
            "artist": "The Smashing Pumpkins",
            "album": "Siamese Dream (2011 Remaster)",
            "channel": "The Smashing Pumpkins - Topic",
            "duration": 201,
        }
        _score, notes, hard_reject = candidate_score(track, wrong_song)
        self.assertTrue(hard_reject)
        self.assertTrue(
            any("title similarity below safety minimum" in note for note in notes)
        )

    def test_unrequested_sing_along_is_rejected(self) -> None:
        candidate = {
            "title": "Running Up That Hill (Sing-Along)",
            "channel": "Kate Bush - Topic",
            "duration": 300,
        }
        _score, _notes, hard_reject = candidate_score(TRACK, candidate)
        self.assertTrue(hard_reject)

    def test_dated_and_marketing_versions_are_rejected(self) -> None:
        for title in (
            "Running Up That Hill (2021 Version)",
            "Running Up That Hill (As It Should Have Sounded 2022)",
            "Running Up That Hill (Reimagined Version)",
        ):
            with self.subTest(title=title):
                _score, _notes, hard_reject = candidate_score(
                    TRACK,
                    {
                        "title": title,
                        "channel": "Kate Bush - Topic",
                        "duration": 300,
                    },
                )
                self.assertTrue(hard_reject)

    def test_sped_up_and_remix_are_rejected(self) -> None:
        for title in (
            "Running Up That Hill (Sped Up)",
            "Running Up That Hill (Club Remix)",
            "Running Up That Hill (Piano Version)",
            "Running Up That Hill (Orchestral Version)",
            "Running Up That Hill (Stripped)",
            "Running Up That Hill (Acapella)",
            "Running Up That Hill (A Cappella)",
        ):
            with self.subTest(title=title):
                _score, _notes, hard_reject = candidate_score(
                    TRACK,
                    {
                        "title": title,
                        "channel": "Kate Bush - Topic",
                        "duration": 300,
                    },
                )
                self.assertTrue(hard_reject)


class AppleMusicMatchingTests(unittest.TestCase):
    def test_metadata_matching_ignores_case_and_punctuation(self) -> None:
        left = metadata_key("Don't Start Now", "Future Nostalgia", "Dua Lipa")
        right = metadata_key("DON’T START NOW", "future nostalgia", "dua lipa")
        self.assertEqual(left, right)

    def test_album_is_part_of_duplicate_key(self) -> None:
        album = metadata_key("Song", "Original Album", "Artist")
        soundtrack = metadata_key("Song", "Movie Soundtrack", "Artist")
        self.assertNotEqual(album, soundtrack)

    def test_artist_is_part_of_duplicate_key(self) -> None:
        original = metadata_key("Song", "Album", "Original Artist")
        cover = metadata_key("Song", "Album", "Cover Artist")
        self.assertNotEqual(original, cover)

    def test_ampersand_normalizes_to_and(self) -> None:
        self.assertEqual(normalize("Simon & Garfunkel"), normalize("Simon and Garfunkel"))


if __name__ == "__main__":
    unittest.main()
