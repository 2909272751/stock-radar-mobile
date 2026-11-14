from backend.app.indicators import calculate_macd, macd_state, moving_average_state


def test_macd_returns_aligned_values():
    closes = [10.0 + index * 0.1 for index in range(80)]
    values = calculate_macd(closes)
    assert len(values) == len(closes)
    assert values[-1][0] > values[-1][1]
    assert values[-1][2] > 0


def test_golden_cross_is_detected_once():
    closes = [10.0] * 40 + [10.0 - index * 0.08 for index in range(1, 21)] + [8.4 + index * 0.35 for index in range(1, 21)]
    state = macd_state(closes[:62])
    assert state is not None
    assert state.crossed_up
    assert not state.crossed_down


def test_not_enough_bars_returns_none():
    assert macd_state([10.0] * 10) is None


def test_moving_average_state_returns_latest_values():
    closes = [10.0 + index * 0.05 for index in range(80)]
    state = moving_average_state(closes)
    assert state is not None
    assert state.ma5 > state.ma10 > state.ma20 > state.ma60
    assert state.prev_ma5 < state.ma5


def test_moving_average_works_without_sixty_bars():
    closes = [10.0 + index * 0.05 for index in range(27)]
    state = moving_average_state(closes)
    assert state is not None
    assert state.ma20 > 0
    assert state.ma60 is None
