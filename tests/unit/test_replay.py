from forge_agent.application.replay import replay_delay_seconds


def test_replay_delay_scales_and_caps() -> None:
    assert replay_delay_seconds(None, "2026-01-01T00:00:02+00:00", 1) == 0
    assert replay_delay_seconds(
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:02+00:00",
        0,
    ) == 0
    assert replay_delay_seconds(
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:02+00:00",
        2,
    ) == 1
    assert replay_delay_seconds(
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:01:00+00:00",
        1,
        max_delay_s=5,
    ) == 5
