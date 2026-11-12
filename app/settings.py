"""Settings. Each can be overridden with an environment variable of the same name in capitals."""

import os
from zoneinfo import ZoneInfo


def _get(name: str, default, cast):
    raw = os.environ.get(name.upper())
    return default if raw is None else cast(raw)


BUDGET_MINUTES = _get("budget_minutes", 15, int)
ACCURACY_FLOOR = _get("accuracy_floor", 0.80, float)
TARGET_RETENTION = _get("target_retention", 0.90, float)
# "Different days" and "today" are counted in the learner's time zone.
TIMEZONE = ZoneInfo(_get("timezone", "America/Vancouver", str))

CURRICULUM_DIR = _get("curriculum_dir", os.path.join(os.path.dirname(__file__), "..", "curriculum"), str)
DB_PATH = _get("db_path", "/data/farsi.db", str)

# Fixed rules from the design. Kept here so they're easy to find.
LOAD_SHARE = 0.80  # new material only while the 7-day forecast is under 80% of the budget
ACCURACY_WINDOW = 50  # attempts
ACCURACY_MIN_ATTEMPTS = 10  # below this the accuracy gate is open
DEFAULT_SECONDS_PER_ATTEMPT = 45
DEFAULT_ITEMS_PER_ATTEMPT = 3
MAX_NEW_PER_DAY = 8
MAX_NEW_PER_DAY_STAGE_0 = 5
MAX_NEW_ON_GRAMMAR_DAY = 3
LEECH_LAPSES = 4
FIRST_EXPOSURES = 3  # repeat, hear, cued
SESSION_IDLE_MINUTES = 30
WARMUP_MAX = 2
NEW_MATERIAL_SECONDS = 180
SCENES_MAX = 3
EASY_WORDS_MAX = 15
INCLUDE_MAX = 6
MISSION_CHECK_DAYS = (30, 90, 180)
