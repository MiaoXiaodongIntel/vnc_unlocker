"""Real screenshots in tests/samples/<state>__<description>.png, classified via Windows OCR."""
from pathlib import Path

import pytest
from PIL import Image

import config
import detector

SAMPLES = sorted((Path(__file__).parent / "samples").glob("*.png"))


@pytest.mark.skipif(not SAMPLES, reason="no samples")
@pytest.mark.parametrize("path", SAMPLES, ids=[p.stem for p in SAMPLES])
def test_sample(path):
    expected = detector.State(path.stem.split("__")[0])
    s = config.Settings()
    s.keywords = {k: [detector.normalize(x) for x in v] for k, v in s.keywords.items()}
    a = detector.analyze(Image.open(path).convert("RGB"), s.ocr_languages, s.keywords, s.ocr_min_width, s.max_lines)
    assert a.state == expected, a.reason
