"""Global admission tests through the real CLI, isolated from the user's installation."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = Path(os.environ.get('AGENTNET_MODULE') or REPO / 'scripts' / 'agentnet.py')
OLD = 'aaaaaaaa-1111-4000-8000-000000000001'
NEW = 'bbbbbbbb-2222-4000-8000-000000000002'
LATER = 'cccccccc-3333-4000-8000-000000000003'


class TestSessionToggle(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix='agentnet-toggle-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'state'
        self.cwd = self.base / 'project'
        self.cwd.mkdir()
        self.other = self.base / 'other-project'
        self.other.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(('AGENTNET_', 'CLAUDE', 'CODEX_', 'OPENCODE_', 'AI_AGENT'))
        }
        self.env.update(AGENTNET_ROOT=str(self.root), PYTHONUTF8='1')

    def cli(self, *args: str, sid: str | None = None, stdin: str = '',
            cwd: Path | None = None, extra_env: dict[str, str] | None = None,
            code: int = 0) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if sid:
            env['AGENTNET_ID'] = sid
        env.update(extra_env or {})
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True,
            text=True, encoding='utf-8', env=env, cwd=cwd or self.cwd, timeout=20,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def hook(self, event: str, sid: str, *, cwd: Path | None = None,
             extra_env: dict[str, str] | None = None, **payload: object
             ) -> subprocess.CompletedProcess[str]:
        return self.cli('hook', event, cwd=cwd, extra_env=extra_env,
                        stdin=json.dumps({'session_id': sid, 'cwd': str(cwd or self.cwd),
                                          **payload}))

    def info(self, sid: str, cwd: Path | None = None) -> Path:
        project = (cwd or self.cwd).resolve()
        digest = hashlib.sha1(os.path.normcase(str(project)).encode('utf-8')).hexdigest()[:8]
        return self.root / 'workspaces' / f'{project.name}-{digest}' / 'agents' / sid / 'info.md'

    def metadata(self, sid: str) -> dict:
        return tomllib.loads(self.info(sid).read_text(encoding='utf-8').split('+++', 2)[1])

    def snapshot(self) -> dict[str, tuple[bytes, int]]:
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob('*') if path.is_file()}

    def assert_silent(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')

    def test_default_admission_and_enable_are_read_only_without_identity(self) -> None:
        self.assertEqual(self.cli('admission').stdout.splitlines()[0], 'enabled')
        self.cli('enable')
        self.cli('enable')
        self.assertFalse(self.root.exists())

    def test_toggle_is_persistent_global_and_idempotent(self) -> None:
        self.cli('disable')
        self.cli('disable', cwd=self.other)
        self.assertEqual(self.cli('admission', cwd=self.other).stdout.splitlines()[0], 'disabled')
        self.assertEqual(set(self.snapshot()), {'.disabled'})
        self.cli('enable', cwd=self.other)
        self.cli('enable')
        self.assertEqual(self.cli('admission').stdout.splitlines()[0], 'enabled')
        self.assertEqual(self.snapshot(), {})

    def test_separate_state_roots_have_independent_toggles(self) -> None:
        self.cli('disable')
        another = self.base / 'separate-state'
        result = self.cli('admission', extra_env={'AGENTNET_ROOT': str(another)})
        self.assertEqual(result.stdout.splitlines()[0], 'enabled')
        self.assertFalse(another.exists())

    def test_management_does_not_touch_existing_registration_or_snapshot(self) -> None:
        self.cli('register', '--name', 'existing', sid=OLD)
        self.cli('log', 'keep this worklog', sid=OLD)
        self.cli('lock', 'acquire', 'test-lock', sid=OLD)
        before = self.snapshot()
        self.cli('disable', sid=OLD)
        self.cli('admission', sid=OLD)
        self.cli('enable', sid=OLD)
        self.assertEqual(self.snapshot(), before)

    def test_management_does_not_resolve_conflicting_harness_identities(self) -> None:
        conflicting = {'CLAUDE_CODE_SESSION_ID': OLD, 'CODEX_SESSION_ID': NEW}
        for command in ('disable', 'admission', 'enable'):
            self.cli(command, extra_env=conflicting)
        self.assertEqual(self.snapshot(), {})

    def test_disabled_hooks_are_silent_and_do_not_create_workspace_or_snapshot(self) -> None:
        self.cli('disable')
        for event in ('session-start', 'stop'):
            self.assert_silent(self.hook(event, NEW))
        self.assertFalse((self.root / 'workspaces').exists())
        self.assertFalse((self.root / 'dashboard-data.json').exists())
        self.assertFalse((self.root / 'config.toml').exists())
        self.assertEqual(len(list((self.root / '.disabled-sessions').iterdir())), 1)

    def test_disabled_hooks_without_identity_do_not_create_fallback_workspace(self) -> None:
        self.cli('disable')
        before = self.snapshot()
        for payload in ('', '{bad', '[]', 'null', '{}'):
            for event in ('session-start', 'stop'):
                self.assert_silent(self.cli('hook', event, stdin=payload))
        self.assertEqual(self.snapshot(), before)

    def test_payload_session_id_is_used_without_shell_session_variables(self) -> None:
        output = self.hook('session-start', OLD).stdout
        self.assertIn('additionalContext', json.loads(output)['hookSpecificOutput'])
        self.assertTrue(self.info(OLD).is_file())
        self.assertFalse(list(self.root.rglob('.fallback-ids')))
        self.cli('disable')
        self.assertIn('additionalContext', self.hook('session-start', OLD, source='resume').stdout)

    def test_explicit_wrapper_id_still_takes_precedence_over_payload(self) -> None:
        self.cli('register', sid=OLD)
        self.cli('disable')
        output = self.hook('session-start', NEW, extra_env={'AGENTNET_ID': OLD}).stdout
        self.assertIn(OLD[:8], output)
        self.assertFalse(self.info(NEW).exists())

    def test_new_payload_overrides_inherited_claude_session_id(self) -> None:
        self.cli('register', sid=OLD)
        self.cli('disable')
        before = self.info(OLD).read_bytes()
        self.assert_silent(self.hook('session-start', NEW,
                                     extra_env={'CLAUDE_CODE_SESSION_ID': OLD}))
        self.assertEqual(self.info(OLD).read_bytes(), before)
        self.assertFalse(self.info(NEW).exists())

    def test_hook_payload_is_authoritative_over_other_harness_environment(self) -> None:
        inherited = {'CODEX_SESSION_ID': OLD, 'OPENCODE_SESSION_ID': OLD}
        self.cli('disable')
        self.assert_silent(self.hook('session-start', NEW, extra_env=inherited))
        self.cli('enable')
        self.assert_silent(self.hook('stop', NEW, extra_env=inherited))
        self.assertIn('additionalContext', self.hook('session-start', LATER,
                                                    extra_env=inherited).stdout)
        self.assertTrue(self.info(LATER).exists())
        self.assertFalse(self.info(OLD).exists())

    def test_disabled_sessions_stay_excluded_after_enable_until_explicit_register(self) -> None:
        self.cli('disable')
        self.assert_silent(self.hook('session-start', NEW))
        self.cli('enable')
        for source in ('resume', 'compact', 'startup'):
            self.assert_silent(self.hook('session-start', NEW, source=source, cwd=self.other))
        self.assert_silent(self.hook('stop', NEW))
        self.assertFalse(self.info(NEW).exists())
        self.assertFalse(self.info(NEW, self.other).exists())
        self.assertIn('additionalContext', self.hook('session-start', LATER).stdout)
        self.assertTrue(self.info(LATER).exists())
        # 显式 register 是重新入网的入口：解除排除标记并注册，之后的钩子按普通会话处理。
        self.assertIn('已解除排除', self.cli('register', sid=NEW, cwd=self.other).stdout)
        self.assertTrue(self.info(NEW, self.other).exists())
        self.assertEqual(list((self.root / '.disabled-sessions').iterdir()), [])
        self.assertIn('additionalContext',
                      self.hook('session-start', NEW, source='resume', cwd=self.other).stdout)

    def test_register_while_still_disabled_does_not_readmit(self) -> None:
        self.cli('disable')
        self.assert_silent(self.hook('session-start', NEW))
        result = self.cli('register', sid=NEW)
        self.assertIn('[SKIP]', result.stdout)
        self.assertIn('agentnet register', result.stdout)
        self.assertFalse(self.info(NEW).exists())
        self.assertEqual(len(list((self.root / '.disabled-sessions').iterdir())), 1)

    def test_stop_first_seen_while_disabled_also_excludes_session(self) -> None:
        self.cli('disable')
        self.assert_silent(self.hook('stop', NEW))
        self.cli('enable')
        self.assert_silent(self.hook('session-start', NEW, source='resume'))
        self.assertFalse(self.info(NEW).exists())

    def test_manual_registration_cannot_bypass_disable(self) -> None:
        self.cli('disable')
        result = self.cli('register', '--name', 'not-visible', sid=NEW)
        self.assertIn('[SKIP]', result.stdout)
        self.assertFalse((self.root / 'workspaces').exists())
        self.cli('enable')
        self.assertIn('已解除排除', self.cli('register', sid=NEW).stdout)
        self.assertTrue(self.info(NEW).exists())

    def test_disabled_manual_registration_without_identity_creates_no_workspace(self) -> None:
        self.cli('disable')
        before = self.snapshot()
        self.assertIn('[SKIP]', self.cli('register').stdout)
        self.assertEqual(self.snapshot(), before)

    def test_existing_registration_refresh_remains_available(self) -> None:
        self.cli('register', '--name', 'existing', '--topics', 'testing', sid=OLD)
        before = self.metadata(OLD)
        self.cli('disable')
        self.cli('register', sid=OLD)
        after = self.metadata(OLD)
        for field in ('id', 'registered_at', 'display_name', 'topics'):
            self.assertEqual(after[field], before[field])

    def test_existing_session_can_change_workspace_while_disabled(self) -> None:
        self.cli('register', sid=OLD)
        self.cli('disable')
        self.assertIn('additionalContext', self.hook('session-start', OLD, cwd=self.other).stdout)
        self.assertTrue(self.info(OLD, self.other).exists())

    def test_existing_archived_session_can_resume_while_disabled(self) -> None:
        self.cli('register', sid=OLD)
        self.cli('exit', sid=OLD)
        archives = list(self.root.glob(f'workspaces/*/archive/{OLD}/info.md'))
        self.assertEqual(len(archives), 1)
        before = archives[0].read_bytes()
        self.cli('disable')
        self.assertIn('additionalContext', self.hook('session-start', OLD, source='resume').stdout)
        self.assertEqual(archives[0].read_bytes(), before)

    def test_existing_stop_hook_still_delivers_letters(self) -> None:
        self.cli('register', sid=OLD)
        self.cli('register', sid=LATER)
        self.cli('disable')
        self.cli('send', '--to', LATER, '--subject', 'still-online', '--body',
                 'message survives disable', '--no-reminder', sid=OLD)
        output = self.hook('stop', LATER, extra_env={'AGENTNET_ONESHOT': '1'}).stdout
        self.assertIn('message survives disable', output)
        self.assertEqual(list((self.info(LATER).parent / 'inbox').glob('*.md')), [])
        self.assertEqual(len(list((self.info(LATER).parent / 'read').glob('*.md'))), 1)

    def test_subagent_hooks_never_modify_or_exclude_parent(self) -> None:
        self.cli('register', sid=OLD)
        self.cli('disable')
        before = self.snapshot()
        for event in ('session-start', 'stop'):
            self.assert_silent(self.hook(event, OLD, agent_id='subagent', agent_type='Explore'))
        self.assertEqual(self.snapshot(), before)

    def test_agent_flag_session_stop_still_delivers_letters(self) -> None:
        # 用 --agent 启动的主会话：payload 只带 agent_type、没有 agent_id。
        self.cli('register', sid=OLD)
        self.cli('register', sid=LATER)
        self.cli('send', '--to', LATER, '--subject', 'agent-flag', '--body',
                 'letter for agent-flag session', '--no-reminder', sid=OLD)
        output = self.hook('stop', LATER, agent_type='my-agent',
                           extra_env={'AGENTNET_ONESHOT': '1'}).stdout
        self.assertIn('letter for agent-flag session', output)
        self.assertEqual(list((self.info(LATER).parent / 'inbox').glob('*.md')), [])

    def test_agent_flag_session_start_is_still_not_auto_registered(self) -> None:
        self.assert_silent(self.hook('session-start', NEW, agent_type='my-agent'))
        self.assertFalse(self.info(NEW).exists())

    def test_sweep_prunes_only_expired_exclusion_markers(self) -> None:
        self.cli('register', sid=OLD)
        self.cli('disable')
        self.assert_silent(self.hook('session-start', NEW))
        self.assert_silent(self.hook('session-start', LATER))
        markers = self.root / '.disabled-sessions'
        expired = markers / hashlib.sha256(NEW.encode('utf-8')).hexdigest()
        fresh = markers / hashlib.sha256(LATER.encode('utf-8')).hexdigest()
        self.assertTrue(expired.is_file() and fresh.is_file())
        aged = time.time() - 15 * 86400   # 默认保留期 14 天
        os.utime(expired, (aged, aged))
        self.assertIn('排除标记', self.cli('sweep', sid=OLD).stdout)
        self.assertFalse(expired.exists())
        self.assertTrue(fresh.exists())

    def test_spawn_is_blocked_before_creating_child_or_launching_process(self) -> None:
        self.cli('register', sid=OLD)
        self.cli('disable')
        result = self.cli('spawn', '--task', 'test task', sid=OLD, code=1)
        self.assertIn('agentnet enable', result.stderr)
        self.assertEqual(len(list(self.root.glob('workspaces/*/agents/*/info.md'))), 1)
        self.assertFalse(list(self.root.glob('workspaces/*/agents/*/inbox/*.md')))

    def test_reset_is_blocked_before_terminating_existing_session(self) -> None:
        self.cli('register', sid=OLD)
        self.cli('register', sid=LATER)
        self.cli('disable')
        before = self.info(LATER).read_bytes()
        result = self.cli('reset', LATER, '--task', 'new task', sid=OLD, code=1)
        self.assertIn('agentnet enable', result.stderr)
        self.assertEqual(self.info(LATER).read_bytes(), before)

    def test_dashboard_keeps_existing_sessions_and_never_lists_disabled_sessions(self) -> None:
        self.cli('register', '--name', 'visible-old', sid=OLD)
        self.cli('disable')
        self.hook('session-start', NEW)
        self.hook('stop', NEW)
        self.cli('dashboard')
        self.assertTrue((self.root / 'dashboard.html').is_file())
        data = json.loads((self.root / 'dashboard-data.json').read_text(encoding='utf-8'))
        ids = {agent['id'] for ws in data['workspaces'] for agent in ws['agents']}
        self.assertEqual(ids, {OLD})
        self.assertNotIn(NEW, json.dumps(data))
        self.cli('enable')
        self.hook('session-start', NEW, source='resume')
        self.hook('session-start', LATER)
        self.cli('dashboard')
        data = json.loads((self.root / 'dashboard-data.json').read_text(encoding='utf-8'))
        ids = {agent['id'] for ws in data['workspaces'] for agent in ws['agents']}
        self.assertEqual(ids, {OLD, LATER})

    def test_disable_does_not_stop_an_existing_poller(self) -> None:
        self.cli('register', sid=OLD)
        self.cli('register', sid=LATER)
        env = {**self.env, 'AGENTNET_ID': OLD}
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), 'poll', '--interval', '1', '--max-wait', '10'],
            cwd=self.cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.metadata(OLD).get('poller_pid') == process.pid:
                    break
                time.sleep(0.05)
            self.assertEqual(self.metadata(OLD).get('poller_pid'), process.pid)
            self.cli('disable')
            self.assertIsNone(process.poll())
            self.assertEqual(self.metadata(OLD).get('poller_pid'), process.pid)
            self.assert_silent(self.hook('session-start', NEW))
            self.assertIsNone(process.poll())
            self.cli('send', '--to', OLD, '--subject', 'after-disable', '--body',
                     'poller still receives letters', '--no-reminder', sid=LATER)
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error.decode('utf-8'))
            self.assertIn('poller still receives letters', output.decode('utf-8'))
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
