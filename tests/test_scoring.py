from __future__ import annotations

import unittest

from youtube_match import (
    automatic_approval_eligible,
    candidate_record,
    candidate_score,
)
from download_mp3 import selected_candidate
from apple_music_duplicates import metadata_key, normalize


TRACK = {
    "title": "Running Up That Hill (A Deal with God)",
    "primary_artist": "Kate Bush",
    "duration_ms": 300000,
}


CAST_TRACK = {
    "title": "Valley of Ashes",
    "primary_artist": "Paul Whitty",
    "album": "The Great Gatsby - A New Musical",
    "duration_ms": 180000,
}

# A licensed cast-album upload: YouTube generated the channel, the distributor
# supplied no usable artist name, and the embedded credits name the performer.
LICENSED_CAST_CANDIDATE = {
    "title": "Valley of Ashes",
    "channel": "Release - Topic",
    "duration": 180,
    "description": "Provided to YouTube by a distributor",
    "track": "Valley of Ashes",
    "artist": "Paul Whitty",
    "album": "The Great Gatsby - A New Musical",
}


class LicensedTopicTests(unittest.TestCase):
    """A generically named Topic channel should not sink a licensed match.

    Cast recordings credit one performer on Spotify while the auto-generated
    channel is named for the release or another cast member, so confirming the
    artist by channel name alone fails on material that is plainly legitimate.
    """

    def test_licensed_upload_confirms_the_artist_despite_the_channel_name(self) -> None:
        score, notes, hard_reject = candidate_score(
            CAST_TRACK, LICENSED_CAST_CANDIDATE
        )
        self.assertFalse(hard_reject)
        self.assertTrue(any("licensed metadata" in note for note in notes))
        self.assertFalse(any("artist not confirmed" in note for note in notes))
        eligible, reason = automatic_approval_eligible(
            CAST_TRACK, LICENSED_CAST_CANDIDATE, score, hard_reject, 95.0
        )
        self.assertTrue(eligible, reason)

    def test_a_topic_channel_alone_is_not_enough(self) -> None:
        # No "provided to youtube by": not the licensed pipeline, so the
        # generic channel name still confirms nothing.
        candidate = dict(LICENSED_CAST_CANDIDATE, description="a fan upload")
        _score, notes, _hard = candidate_score(CAST_TRACK, candidate)
        self.assertTrue(any("artist not confirmed" in note for note in notes))

    def test_a_licensed_upload_off_a_topic_channel_is_not_enough(self) -> None:
        # Anyone can title a channel; only YouTube can mint a Topic channel,
        # which is what ties this confirmation to the licensed pipeline.
        candidate = dict(LICENSED_CAST_CANDIDATE, channel="Musicals Fan Uploads")
        _score, notes, _hard = candidate_score(CAST_TRACK, candidate)
        self.assertTrue(any("artist not confirmed" in note for note in notes))

    def test_mismatched_embedded_credits_still_hard_reject(self) -> None:
        candidate = dict(LICENSED_CAST_CANDIDATE, artist="Somebody Else")
        _score, _notes, hard_reject = candidate_score(CAST_TRACK, candidate)
        self.assertTrue(hard_reject)


class ApprovalEvidenceTests(unittest.TestCase):
    """The approval decision must weigh the evidence the score weighed.

    Scoring runs on the hydrated candidate while approval re-derives its
    signals from a rebuilt one, so any field the record drops is invisible to
    approval. When that happened the two disagreed outright: the notes read
    "artist confirmed by licensed metadata" while the track was held back for
    "artist is not confirmed".
    """

    def test_record_carries_the_embedded_credits_forward(self) -> None:
        hydrated = dict(
            LICENSED_CAST_CANDIDATE, id="abc123", webpage_url="https://x/abc123"
        )
        record = candidate_record(CAST_TRACK, hydrated)
        self.assertEqual(record["metadata_artist"], "Paul Whitty")
        # Stored verbatim; the scorer normalises before matching on it.
        self.assertIn(
            "provided to youtube by", record["metadata_description"].lower()
        )

    def test_score_and_approval_agree_on_a_licensed_upload(self) -> None:
        hydrated = dict(
            LICENSED_CAST_CANDIDATE, id="abc123", webpage_url="https://x/abc123"
        )
        record = candidate_record(CAST_TRACK, hydrated)
        self.assertIn("licensed metadata", record["score_notes"])
        # Rebuilt exactly the way the matcher rebuilds it before approving.
        rebuilt = {
            "title": record["youtube_title"],
            "channel": record["youtube_channel"],
            "duration": record["youtube_duration_seconds"],
            "channel_is_verified": record["youtube_channel_verified"],
            "description": record["metadata_description"],
            "artist": record["metadata_artist"],
            "track": record["metadata_track"],
            "album": record["metadata_album"],
        }
        eligible, reason = automatic_approval_eligible(
            CAST_TRACK, rebuilt, record["score"], bool(record["hard_reject"]), 95.0
        )
        self.assertTrue(eligible, reason)
        self.assertNotIn("not confirmed", reason)


class DownloadGateInheritsMatchFindingTests(unittest.TestCase):
    """The download gate re-checks approval from the stored track row.

    That row keeps the channel and title but never the embedded credits, so a
    licensed cast-album upload the matcher confirmed would be refused here for
    "artist is not confirmed" -- approved by one stage and unreachable by the
    next. The finding is persisted so both stages agree.
    """

    def track_row(self, licensed: int) -> dict:
        return {
            "title": "Valley of Ashes",
            "primary_artist": "Paul Whitty",
            "album": "The Great Gatsby - A New Musical",
            "duration_ms": 180000,
            "youtube_title": "Valley of Ashes",
            "youtube_channel": "Release - Topic",
            "youtube_duration_seconds": 180,
            "youtube_channel_verified": 0,
            "youtube_licensed_topic": licensed,
        }

    def test_a_persisted_finding_carries_the_track_through(self) -> None:
        track = self.track_row(1)
        eligible, reason = automatic_approval_eligible(
            track, selected_candidate(track), 100.0, False, 95.0
        )
        self.assertTrue(eligible, reason)

    def test_without_it_the_generic_channel_still_blocks(self) -> None:
        track = self.track_row(0)
        eligible, reason = automatic_approval_eligible(
            track, selected_candidate(track), 100.0, False, 95.0
        )
        self.assertFalse(eligible)
        self.assertIn("artist is not confirmed", reason)

    def test_rows_predating_the_column_do_not_crash(self) -> None:
        track = self.track_row(0)
        del track["youtube_licensed_topic"]
        self.assertFalse(selected_candidate(track)["licensed_topic"])


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
