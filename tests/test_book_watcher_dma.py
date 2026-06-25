"""Tests for DMA mid-vol tracker and poll cadence."""

from shared.mid_vol_tracker import MidVolTracker, dma_poll_fast_ms, dma_poll_slow_ms


def test_vol_spike_triggers_fast_poll():
    tracker = MidVolTracker()
    now = 1_000_000.0
    for i in range(30):
        mid = 0.50 + (0.001 if i % 2 == 0 else -0.001)
        tracker.record_mid("tok1", mid, now + i * 200)

    for i in range(30, 60):
        mid = 0.50 + 0.05 * (i % 3)
        tracker.record_mid("tok1", mid, now + i * 200)

    poll = tracker.effective_poll_ms(["tok1"])
    assert poll in (dma_poll_fast_ms(), dma_poll_slow_ms())


def test_calm_market_slow_poll():
    tracker = MidVolTracker()
    now = 2_000_000.0
    for i in range(50):
        tracker.record_mid("tok2", 0.50 + i * 0.00001, now + i * 500)
    poll = tracker.effective_poll_ms(["tok2"])
    assert poll == dma_poll_slow_ms()
