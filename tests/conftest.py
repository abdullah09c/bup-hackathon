import json
from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parents[1] / "BUP_CSE_FEST_2026_Participant_Docs" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"


def load_cases():
    return json.loads(SAMPLES.read_text(encoding="utf-8"))["cases"]


@pytest.fixture(scope="session")
def cases():
    return load_cases()
