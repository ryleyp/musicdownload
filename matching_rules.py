"""Auditable policy settings for Spotify-to-YouTube matching.

Adjust values here, then run the scoring tests before using the new policy.
The score is clamped to 0-100. Automatic approval also requires every safety
gate in ``automatic_approval_eligible``; score alone is never sufficient.
"""

SCORING_VERSION = 10
# Later policies add rejection gates without changing the numeric weights introduced
# in policy 6. Hydrated scores from 6+ remain usable only after all current
# safety gates are recalculated locally.
STORED_SCORE_COMPATIBLE_SINCE = 6

# A close runtime is mandatory for automatic approval and --min-score downloads.
AUTO_APPROVAL_MAX_DURATION_DIFFERENCE_SECONDS = 5
DOWNLOADED_FILE_MAX_DURATION_DIFFERENCE_SECONDS = 5
DEFAULT_CANDIDATE_HYDRATION_COUNT = 8

# Score weights. Runtime contributes as much as the source-quality signals.
TITLE_SIMILARITY_POINTS = 45
TITLE_TOKEN_COVERAGE_POINTS = 15
ARTIST_MATCH_POINTS = 15
ARTIST_MISMATCH_PENALTY = 18

SOURCE_BONUSES = {
    "artist_topic": 15,
    "official_audio": 10,
    "official_lyric_video": 8,
    "verified_artist": 5,
    "audio": 3,
    "lyric_video": 2,
}

# (maximum difference in seconds, score adjustment)
DURATION_SCORE_BANDS = (
    (2, 20),
    (5, 16),
    (8, 8),
    (12, 0),
    (20, -20),
    (45, -40),
    (float("inf"), -65),
)
DURATION_MISSING_PENALTY = 25
DURATION_HARD_REJECT_SECONDS = 45

MIN_TITLE_TOKEN_COVERAGE = 0.65
LOW_TITLE_COVERAGE_PENALTY = 35
MIN_TITLE_SIMILARITY = 0.45

HARD_REJECT_PHRASES = {
    "behind the scenes",
    "concert video",
    "interview",
    "music video",
    "music vid",
    "official 4k video",
    "official hd video",
    "official music video",
    "official promo video",
    "official video",
    "reaction",
    "tutorial",
}

# A bare "video" label is normally a music/promo video. Keep the small allowlist
# here so this policy is visible and adjustable without touching scoring code.
ALLOWED_VIDEO_PHRASES = {
    "lyric video",
    "lyrics video",
}

# An unexpected term is rejected unless Spotify names that version in its title.
# Keep phrases normalized (lowercase words separated by a single space).
ALTERED_VERSION_TERMS = {
    "8d audio",
    "a cappella",
    "acapella",
    "acoustic",
    "alternate version",
    "alternative version",
    "anniversary",
    "bass boosted",
    "cast recording",
    "clean",
    "concert",
    "cover",
    "demo",
    "deluxe",
    "edit",
    "extended",
    "explicit",
    "instrumental",
    "karaoke",
    "live",
    "mashup",
    "medley",
    "metal version",
    "nightcore",
    "original motion picture soundtrack",
    "orchestral version",
    "piano version",
    "performance",
    "radio edit",
    "re recorded",
    "remaster",
    "remastered",
    "remix",
    "reverb",
    "session",
    "sing along",
    "slowed",
    "sped up",
    "stereo",
    "stripped",
    "symphonic version",
    "soundtrack",
    "tribute",
    "taylor s version",
    "unplugged",
    "rock version",
}

# When Spotify names one of these versions, the YouTube title or hydrated
# track/album metadata must confirm it. This prevents a pristine official
# upload of the wrong master from outranking the requested recording.
REQUIRED_VERSION_TERMS = {
    "a cappella",
    "acapella",
    "acoustic",
    "alternate version",
    "alternative version",
    "cast recording",
    "cover",
    "demo",
    "instrumental",
    "karaoke",
    "live",
    "metal version",
    "orchestral version",
    "piano version",
    "radio edit",
    "re recorded",
    "remaster",
    "remastered",
    "remix",
    "sing along",
    "slowed",
    "sped up",
    "stripped",
    "symphonic version",
    "taylor s version",
    "unplugged",
    "rock version",
}

# Regex patterns catch altered-version labels that contain changing dates or
# descriptive marketing language. They run against normalized text.
ALTERED_VERSION_PATTERNS = (
    ("dated version", r"\b(?:19|20)\d{2}\s+(?:version|mix|remix|remaster)\b"),
    ("as it should have sounded", r"\bas it should have sounded\b"),
    ("reimagined version", r"\b(?:new|updated|reimagined|redux)\s+version\b"),
)

DEFAULT_AUTO_APPROVAL_SCORE = 95.0
