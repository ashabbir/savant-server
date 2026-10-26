"""Scheduler opt-out keeps manual queue consumption available."""
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.no_db


def test_scheduler_switch_prevents_in_process_automatic_schedulers(monkeypatch):
    from knowledge import maintenance
    from context import periodic_runner
    monkeypatch.setenv('SAVANT_WORKER_SCHEDULER_ENABLED', '0')
    scheduler = Mock()
    thread = Mock()
    monkeypatch.setattr(maintenance, 'BackgroundScheduler', scheduler)
    monkeypatch.setattr(periodic_runner.threading, 'Thread', thread)
    maintenance.start_maintenance_scheduler()
    periodic_runner.start_periodic_runner()
    scheduler.assert_not_called()
    thread.assert_not_called()


def test_worker_container_switch_only_starts_queue_consumer(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    log = tmp_path / 'calls'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('node', 'python', 'nice', 'mkdir', 'rm', 'sleep'):
        script = bin_dir / name
        if name == 'node':
            body = 'case "$1" in */server.js) exec /bin/sleep 2;; *) exit 0;; esac'
        elif name in {'mkdir', 'rm'}:
            body = 'exit 0'
        elif name == 'sleep':
            body = '/bin/sleep 0.2'
        else:
            body = f'echo "$@" >> "{log}"'
        script.write_text('#!/bin/sh\n' + body + '\n')
        script.chmod(0o755)
    env = {**os.environ, 'PATH': str(bin_dir), 'SAVANT_ROLE': 'worker',
           'SAVANT_SERVER_DATA_DIR': str(tmp_path / 'data'), 'SAVANT_WORKER_SCHEDULER_ENABLED': '0'}
    subprocess.run(['/bin/sh', str(Path(__file__).resolve().parents[1] / 'docker-entrypoint.sh')], env=env, capture_output=True, timeout=5)
    calls = log.read_text()
    assert 'context.job_worker' in calls
    assert 'context.periodic_runner' not in calls
    assert 'knowledge.maintenance_runner' not in calls


@pytest.mark.parametrize('header', ['X-App-Name', 'X-Savant-App'])
def test_both_application_headers_remain_supported(header):
    from flask import Flask
    from utils.auth import require_savant_app
    app = Flask(__name__)
    @app.get('/api/protected')
    @require_savant_app
    def protected():
        return {'ok': True}
    client = app.test_client()
    assert client.get('/api/protected', headers={header: 'savant-olympus'}).status_code == 200
    assert client.get('/api/protected', headers={header: 'unrecognized'}).status_code == 403
