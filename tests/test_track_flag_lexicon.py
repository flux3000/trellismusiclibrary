"""Track flag thesaurus: variants it must catch and song titles it must leave alone."""
import pytest

from app.utils.ingest import detect_track_flags, _extract_trailing_songwriter
from app.utils.track_flag_lexicon import match_flags, title_segments

# Variants seen in real info files and library track titles.
POSITIVE = [
    ("Banter", {"banter"}),
    ("stage banter", {"banter"}),
    ("(Chatter)", {"banter"}),
    ("<chatter>", {"banter"}),
    ("Chit-Chat", {"banter"}),
    ("Crosstalk", {"banter"}),
    ("Dialogue", {"banter"}),
    ("Talk", {"banter"}),
    ("-Talk-", {"banter"}),
    ("(Talking)", {"banter"}),
    ("Herb speaks", {"banter"}),
    ("Cannon Talks", {"banter"}),
    ("(Esbjörn Talks)", {"banter"}),
    ("Pat's remarks (L)", {"banter"}),
    ("interim / comments (00:17.71)", {"banter"}),
    ("[5:46.45] [banter] > Hasten Down the Wind", {"banter"}),
    ("15 banter", {"banter"}),
    ("technical difficulties", {"banter"}),
    ("Tuning", {"tuning"}),
    ("tune up", {"tuning"}),
    ("Tuning, Etc.", {"tuning"}),
    ("Pre-Show Tune Up", {"tuning"}),
    ("Mike Check & Tuning", {"tuning"}),
    ("Banter & Tuning", {"banter", "tuning"}),
    ("Crowd/Tuning", {"audience", "tuning"}),
    ("tuning and banter (Bobby)", {"banter", "tuning"}),
    ("Crowd", {"audience"}),
    ("(Applause)", {"audience"}),
    ("Encore Break", {"audience"}),
    ("encore break #2", {"audience"}),
    ("crowd noise before intro", {"audience"}),
    ("Applause Before Encore, Talk", {"audience", "banter"}),
    ("Intro", {"introduction"}),
    ("01. Intro", {"introduction"}),
    ("(Intro)", {"introduction"}),
    ("< intro >", {"introduction"}),
    ("Introductions", {"introduction"}),
    ("Song Intro", {"introduction"}),
    ("Set II intro", {"introduction"}),
    ("Intro by festival MC", {"introduction"}),
    ("introduction by Dexter Gordon", {"introduction"}),
    ("Sam introduces Béla", {"introduction"}),
    ("(Cox Family Intro)", {"introduction"}),
    ("MC", {"introduction"}),
    ("Intro, Tuning", {"introduction", "tuning"}),
    ("Band Intros", {"band_intros"}),
    ("Band Introductions", {"band_intros"}),
    ("band intro's", {"band_intros"}),
    ("Band Member Intros", {"band_intros"}),
    ("Band Intros by Nick Forester", {"band_intros"}),
    ("Announcement", {"announcement"}),
    ("Announcements by Willis Conover and Art Farmer", {"announcement"}),
    ("(Fm Speaker)", {"announcement"}),
    ("Station ID", {"announcement"}),
    ("Interview", {"interview"}),
    ("Q & A", {"interview"}),
    ("Interview with Nate Smith", {"interview"}),
    ("Outro", {"outro"}),
    ("Outtro", {"outro"}),
    ("outtro-", {"outro"}),
    ("(fm outro)", {"outro"}),
    ("Closing Remarks", {"outro"}),
    ("Goodnight", {"outro"}),
    ("good night etc.", {"outro"}),
    ("Band Intros, Goodnight…", {"band_intros", "outro"}),
]

# Song titles and musical sections that share a word with a flag.
NEGATIVE = [
    "Talking Heads",
    "Talk to Me",
    "(It's Just) Talk",
    "Money Talks",
    "Pillow Talk",
    "Sweet Talk",
    "Don't Talk (Put Your Head On My Shoulder)",
    "Everybody's Talkin'",
    "Speak Low",
    "Goodnight, Irene",
    "Goodnight Irene",
    "And We Bid You Goodnight",
    "Good Night To Boogie",
    "Thank You (Falettinme Be Mice Elf Agin)",
    "Thank You",
    "Farewell Blues",
    "Ashokan Farewell",
    "Welcome to Wonderland",
    "Lost In A Crowd",
    "Crowd Control",
    "Tuning In",
    "Piano Intro >",
    "Dark Star Intro -> Fields of Gray",
    "Instrumental Intro",
    "Instrumental Outro",
    "Intro > Deep Water",
    "Outro (Y'all Come) > Dark Star",
    "Interviewer Blues",
    "Announcing Spring",
    "Chatterbox",
    "Bandit Introspection",
    "Fiddle Tune",
    "Tune Up Your Heart",
    "Soundcheck Blues",
    "Cryptical Envelopment >",
]


@pytest.mark.parametrize("title,expected", POSITIVE)
def test_variants_flag(title, expected):
    assert match_flags(title) == expected, title


@pytest.mark.parametrize("title", NEGATIVE)
def test_song_titles_not_flagged(title):
    assert match_flags(title) == set(), title


def test_detect_track_flags_includes_outro_and_structure():
    assert detect_track_flags("Outro") == ["outro"]
    assert detect_track_flags("// Outro") == ["outro", "start_truncated"]
    assert detect_track_flags("Dark Star (x)") == ["incomplete"]


def test_normalization_drops_numbers_timestamps_and_marks():
    assert title_segments("12 - Banter (1:08)") == ["banter"]
    assert title_segments("tuning/banter - Island Time") == ["tuning", "banter", "island time"]
    assert title_segments("Café Intro") == ["cafe intro"]


def test_songwriter_credit_not_taken_from_flag_label():
    assert _extract_trailing_songwriter("Tuning (Bobby)") == ("Tuning (Bobby)", None)
    assert _extract_trailing_songwriter("Outro (Pat Metheny)") == ("Outro (Pat Metheny)", None)
    assert _extract_trailing_songwriter("Carry On (Stephen Stills)") == ("Carry On", "Stephen Stills")
