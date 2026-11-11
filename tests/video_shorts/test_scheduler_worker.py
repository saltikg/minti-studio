from threading import Event, Thread

from flask import Flask

from app.video_shorts import scheduler_worker


def test_tick_thread_keeps_running_while_customer_ingest_is_busy(monkeypatch):
    app = Flask("scheduler-test")
    stop = Event()
    ingest_release = Event()
    tick_count = 0

    def slow_test_ingest():
        ingest_release.wait(2)

    def tick():
        nonlocal tick_count
        tick_count += 1
        if tick_count == 4:
            stop.set()
        return False

    monkeypatch.setattr(scheduler_worker.youtube_scheduling, "scheduler_tick", tick)
    monkeypatch.setattr(scheduler_worker.scheduler_reachability, "process_monitors", lambda: False)

    ingest_thread = Thread(target=slow_test_ingest, name="simulated-customer-ingest")
    ingest_thread.start()
    scheduler_worker.run_tick_loop(app, stop, interval_seconds=0.01)

    assert ingest_thread.is_alive()
    assert tick_count == 4
    ingest_release.set()
    ingest_thread.join(timeout=1)


def test_publish_loop_claims_only_publish_short(monkeypatch):
    app = Flask("scheduler-publish-test")
    stop = Event()
    calls = []

    monkeypatch.setattr(scheduler_worker, "_mem_available_mb", lambda: 4096)
    monkeypatch.setattr(scheduler_worker, "_publish_job_waiting", lambda: True)

    def run(command, *, env, check):
        calls.append((command, env["MINTI_SCHEDULER_WORKER_ID"], check))
        stop.set()
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(scheduler_worker.subprocess, "run", run)
    scheduler_worker._publish_loop(app, stop, "scheduler:test:1")

    assert calls == [
        (
            [scheduler_worker.sys.executable, "-m", "app.video_shorts.publish_worker_once"],
            "scheduler:test:1",
            False,
        )
    ]
