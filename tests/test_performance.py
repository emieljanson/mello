from mello.managers.performance import AnimationTimer


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def run_frame(timer, clock, phases, animating, dt):
    timer.begin_frame()
    for name, ms in phases:
        clock.t += ms / 1000
        timer.mark(name)
    return timer.end_frame(animating, dt)


def test_summary_only_when_animation_ends():
    clock = FakeClock()
    timer = AnimationTimer(clock=clock)
    phases = [('events', 1), ('update', 2), ('draw', 10), ('display', 3)]
    assert run_frame(timer, clock, phases, True, 0.02) is None
    assert run_frame(timer, clock, [('events', 1), ('update', 2), ('draw', 40), ('display', 3)], True, 0.05) is None
    summary = run_frame(timer, clock, phases, False, 0.2)
    assert summary.startswith('ANIM | frames=2 | 0.07s | fps=29 | worst=50ms')
    assert 'draw=25.0' in summary
    assert 'worst frame ms: events=1 update=2 draw=40 display=3' in summary
    # Nothing more until the next animation
    assert run_frame(timer, clock, phases, False, 0.2) is None


def test_idle_frames_never_report():
    clock = FakeClock()
    timer = AnimationTimer(clock=clock)
    for _ in range(5):
        assert run_frame(timer, clock, [('draw', 5)], False, 0.2) is None
