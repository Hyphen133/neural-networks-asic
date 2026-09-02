"""Unit tests for the DADS loader and the extraction helpers.

    .venv/bin/python -m pytest train/test_dadsdata.py -q
"""

import io
import os
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dadsdata as D  # noqa: E402
import extract_dads as E  # noqa: E402


def test_parse_name_reads_label_and_index():
    assert D.parse_name("drone-83355.wav") == (1, 83355)
    assert D.parse_name("/some/dir/no-drone-100.wav") == (0, 100)


def test_parse_name_rejects_unknown_names():
    with pytest.raises(ValueError):
        D.parse_name("foo.wav")
    with pytest.raises(ValueError):
        D.parse_name(None)


def test_split_is_constant_within_a_recording_block():
    assert len({D.which_split(1, i) for i in range(D.BLOCK_POS, 2 * D.BLOCK_POS)}) == 1
    assert len({D.which_split(0, i) for i in range(D.BLOCK_NEG, 2 * D.BLOCK_NEG)}) == 1


def test_split_proportions_are_roughly_80_10_10():
    s = np.array([D.which_split(1, i) for i in range(0, 400_000, 13)])
    frac = np.bincount(s, minlength=3) / len(s)
    assert 0.7 < frac[0] < 0.9 and 0.05 < frac[1] < 0.15 and 0.05 < frac[2] < 0.15


def test_keep_fraction_is_deterministic_and_proportional():
    kept = [D.keep_fraction(i, 0.35) for i in range(20_000)]
    assert kept == [D.keep_fraction(i, 0.35) for i in range(20_000)]
    assert 0.33 < np.mean(kept) < 0.37
    assert all(D.keep_fraction(i, 1.0) for i in range(100))


def test_loop_tile_repeats_short_clip_to_full_length():
    x = np.arange(8000, dtype=np.float32)
    t = D.loop_tile(x)
    assert t.shape == (D.CLIP_LEN,)
    assert t[8000] == x[0] and t[15999] == x[7999]
    assert D.loop_tile(np.zeros(0, np.float32)).shape == (D.CLIP_LEN,)


def test_segments_cuts_long_clip_and_drops_silence():
    rng = np.random.default_rng(0)
    long = rng.standard_normal(10 * D.CLIP_LEN).astype(np.float32) * 0.1
    segs = D.segments(long, 4)
    assert len(segs) == 4 and all(len(s) == D.CLIP_LEN for s in segs)
    assert D.segments(np.zeros(10 * D.CLIP_LEN, np.float32), 4) == []
    short = rng.standard_normal(5000).astype(np.float32)
    assert len(D.segments(short, 4)) == 1 and len(D.segments(short, 4)[0]) == D.CLIP_LEN


def _wav_bytes(samples: np.ndarray, rate: int = 16000, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(width); w.setframerate(rate)
        w.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def test_decode_wav_scales_to_unit_range():
    y = D.decode_wav(_wav_bytes((np.sin(np.arange(5000) * 0.1) * 20000)))
    assert y.shape == (5000,) and y.dtype == np.float32
    assert abs(y.max() - 20000 / 32768) < 1e-3


def test_decode_wav_rejects_wrong_rate():
    with pytest.raises(ValueError):
        D.decode_wav(_wav_bytes(np.zeros(100), rate=44100))


def test_peak_normalise_hits_target_and_survives_silence():
    a = np.array([[0.1, -0.5, 0.2], [0.0, 0.0, 0.0]], np.float32)
    n = D.peak_normalise(a, 0.7)
    assert abs(np.abs(n[0]).max() - 0.7) < 1e-6
    assert np.all(n[1] == 0)


def test_clip_gain_is_log_uniform_in_range_and_deterministic():
    g = np.array([E.clip_gain(1, i, 0, 0.25) for i in range(4000)])
    assert g.min() >= 0.25 and g.max() <= 1.0
    assert 0.45 < np.median(g) < 0.55          # log-uniform median = sqrt(0.25)
    assert E.clip_gain(1, 7, 0, 0.25) == E.clip_gain(1, 7, 0, 0.25)
    assert E.clip_gain(1, 7, 0, 1.0) == 1.0
