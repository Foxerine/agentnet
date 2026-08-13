"""AgentNet 不变式测试。

    python -m unittest discover -s tests -v
    # 或
    python tests/test_agentnet.py

只用标准库（与被测代码同样的约束）。**测的是"错了不会立刻报错、只会静默出错"的那些不变式**——
这类缺陷不会在人眼前崩溃，只会在几小时后表现为"我写的职责声明不见了"或"两个人同时拿到了锁"。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_ROOT = Path(tempfile.mkdtemp(prefix='agentnet-test-'))
# ROOT 是被测模块的 import 期常量，所以必须在 import 之前指向临时目录，
# 否则测试会污染真实的 ~/.agentnet
os.environ['AGENTNET_ROOT'] = str(_ROOT)

_spec = importlib.util.spec_from_file_location('agentnet', _REPO / 'scripts' / 'agentnet.py')
assert _spec and _spec.loader
an = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(an)


class Base(unittest.TestCase):
    """每个用例一个干净的 workspace 与身份。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix='ws-'))
        self._cwd = Path.cwd()
        os.chdir(self.tmp)
        self.agent_id = 'aaaaaaaa-0000-0000-0000-000000000001'
        os.environ['AGENTNET_ID'] = self.agent_id
        an.Config._cache = None

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        os.environ.pop('AGENTNET_ID', None)

    def ctx(self) -> an.Ctx:
        return an.Ctx()

    def register(self, topics: str | None = None, name: str | None = None) -> None:
        import argparse
        an.cmd_register(argparse.Namespace(topics=topics, name=name))


# ══════════════════════════════════════════════════════════════════════════
# frontmatter：读写往返
# ══════════════════════════════════════════════════════════════════════════

class TestFrontmatter(Base):

    def test_roundtrip_preserves_types(self) -> None:
        """TOML 值类型必须原样回来——尤其 Windows 路径与带时区时间。"""
        path = self.tmp / 'doc.md'
        meta = {
            'id': 'x-1', 'pid': 42, 'cwd': r'C:\Users\Foo\Bar',
            'topics': ['a', 'b'], 'registered_at': an.now(),
        }
        order = ('id', 'pid', 'cwd', 'topics', 'registered_at')
        an.write_doc(path, meta, '正文', order)
        back, body = an.parse_doc(path)

        self.assertEqual(back['cwd'], r'C:\Users\Foo\Bar', '反斜杠路径被转义吃掉了')
        self.assertEqual(back['topics'], ['a', 'b'])
        self.assertEqual(back['pid'], 42)
        self.assertEqual(back['registered_at'], meta['registered_at'])
        self.assertEqual(body.strip(), '正文')

    def test_naive_datetime_refused(self) -> None:
        """裸 naive 时间会在跨时区比较时静默错位，必须拒绝。"""
        from datetime import datetime
        with self.assertRaises(ValueError):
            an.toml_value(datetime(2026, 1, 1, 0, 0, 0))

    def test_malformed_frontmatter_dies_loudly(self) -> None:
        path = self.tmp / 'bad.md'
        path.write_text('+++\nthis is not = = toml\n+++\n\nbody\n', encoding='utf-8')
        with self.assertRaises(SystemExit):
            an.parse_doc(path)


# ══════════════════════════════════════════════════════════════════════════
# info.md 字段级合并 —— 本项目最容易写错的一处
# ══════════════════════════════════════════════════════════════════════════

class TestInfoMerge(Base):

    def test_heartbeat_preserves_topics_and_body(self) -> None:
        """心跳每 5 分钟跑一次。若它重写整个文件，LLM 写的职责与日志会被静默抹掉。

        这是本仓库最隐蔽的一类缺陷：charter 完一切正常，五分钟后内容凭空消失。
        """
        self.register(topics='canvas,billing')
        ctx = self.ctx()
        an.merge_info(ctx.info_path, {}, body='## 负责内容\n\n我负责 A1\n\n## 工作日志\n\n- 开工\n')

        before_meta, before_body = an.read_info(ctx.info_path)
        an.merge_info(ctx.info_path, {'last_active': an.now(), 'pid': 999})  # 模拟心跳
        after_meta, after_body = an.read_info(ctx.info_path)

        self.assertEqual(after_body, before_body, '心跳把正文冲掉了')
        self.assertEqual(after_meta['topics'], before_meta['topics'], '心跳把 topics 冲掉了')
        self.assertEqual(after_meta['pid'], 999, '心跳自己的字段没写进去')
        self.assertNotEqual(after_meta['last_active'], before_meta.get('last_active_missing'))

    def test_register_is_idempotent(self) -> None:
        """钩子调一次 + LLM 再调一次，必须与只调一次等价。"""
        self.register(topics='alpha', name='first')
        ctx = self.ctx()
        first_meta, _ = an.read_info(ctx.info_path)
        an.merge_info(ctx.info_path, {}, body='## 负责内容\n\n手写的职责\n')

        self.register()  # 第二次，不带任何参数
        second_meta, second_body = an.read_info(ctx.info_path)

        self.assertEqual(second_meta['registered_at'], first_meta['registered_at'],
                         'registered_at 被第二次注册覆盖了')
        self.assertEqual(second_meta['topics'], ['alpha'], 'topics 被清空了')
        self.assertIn('手写的职责', second_body, '正文被覆盖了')
        agents = list((ctx.agents_dir).iterdir())
        self.assertEqual(len(agents), 1, f'重复注册产生了副本: {[a.name for a in agents]}')

    def test_charter_keeps_worklog(self) -> None:
        """更新职责不该清空履历——它们是两段不同所有权的内容。"""
        import argparse
        self.register()
        ctx = self.ctx()
        an.cmd_log(argparse.Namespace(entry='第一条记录', entry_file=None, plan=None, pivot=False))

        summary = self.tmp / 'scope.md'
        summary.write_text('我改做 B2 了', encoding='utf-8')
        an.cmd_charter(argparse.Namespace(topics='b2', summary_file=str(summary)))

        _, body = an.read_info(ctx.info_path)
        self.assertIn('我改做 B2 了', body)
        self.assertIn('第一条记录', body, 'charter 把工作日志清掉了')

    def test_worklog_appends_not_replaces(self) -> None:
        import argparse
        self.register()
        for text in ('第一步', '第二步', '第三步'):
            an.cmd_log(argparse.Namespace(entry=text, entry_file=None, plan=None, pivot=False))
        _, body = an.read_info(self.ctx().info_path)
        for text in ('第一步', '第二步', '第三步'):
            self.assertIn(text, body)

    def test_superseded_writer_cannot_clobber_owner(self) -> None:
        """被接替的轮询器不得覆盖新主人的登记。

        真实事故（0de75e6c 报告）：旧 poller 在 ``sleep`` 里睡着，醒来后用**自己的旧 pid**
        覆盖了新 poller 的登记 ⇒ ``poller_pid`` 指向一个不存在的进程 ⇒ 任何
        "读 poller_pid 再查存活"的外部检测都误判成死亡。密集 RELOAD 时必现。
        """
        self.register()
        info = self.ctx().info_path

        an.merge_info(info, {'poller_pid': 111})          # 旧 poller 认领
        an.merge_info(info, {'poller_pid': 222})          # 新 poller 接管（无条件，新主人）

        stale_write = an.merge_info(info, {'poller_pid': None}, expect={'poller_pid': 111})
        self.assertIsNone(stale_write, '旧持有者的写没有被前置条件挡住')
        self.assertEqual(an.read_info(info)[0]['poller_pid'], 222, '新主人的登记被覆盖了')

        own_write = an.merge_info(info, {'poller_pid': None}, expect={'poller_pid': 222})
        self.assertIsNotNone(own_write, '真正的持有者反而写不进去')
        self.assertIsNone(an.read_info(info)[0].get('poller_pid'))

    def test_expect_does_not_touch_body(self) -> None:
        """前置条件不成立时必须**完全不落盘**，不能顺手改了正文。"""
        self.register()
        info = self.ctx().info_path
        an.merge_info(info, {}, body='## 负责内容\n\n原样保留\n')
        refused = an.merge_info(info, {'pid': 7}, body='## 负责内容\n\n不该写进去\n',
                                expect={'poller_pid': 99999})
        self.assertIsNone(refused)
        _, body = an.read_info(info)
        self.assertIn('原样保留', body)
        self.assertNotIn('不该写进去', body)

    def test_split_build_body_roundtrip(self) -> None:
        body = '## 负责内容\n\n职责\n\n## 工作日志\n\n- a\n- b\n'
        scope, worklog = an.split_body(body)
        self.assertIn('职责', scope)
        self.assertIn('- a', worklog)
        rebuilt = an.build_body(scope, worklog)
        scope2, worklog2 = an.split_body(rebuilt)
        self.assertEqual(scope.strip(), scope2.strip())
        self.assertEqual(worklog.strip(), worklog2.strip())


# ══════════════════════════════════════════════════════════════════════════
# 存活判定：读取时推算，不信任存过的值
# ══════════════════════════════════════════════════════════════════════════

class TestStatus(Base):

    def test_status_is_computed_not_trusted(self) -> None:
        meta = {'status': an.STATUS_ACTIVE,
                'last_active': an.now() - timedelta(seconds=an.dead_after_s() + 60)}
        self.assertEqual(an.effective_status(meta), an.STATUS_PRESUMED_DEAD,
                         '存的是 active 就当 active —— 没有按心跳推算')

    def test_terminal_status_not_recomputed(self) -> None:
        """exited / archived 是显式终态，不该被心跳推算覆盖。"""
        for terminal in (an.STATUS_EXITED, an.STATUS_ARCHIVED):
            meta = {'status': terminal, 'last_active': an.now()}
            self.assertEqual(an.effective_status(meta), terminal)

    def test_missing_heartbeat_is_dead(self) -> None:
        self.assertEqual(an.effective_status({'status': an.STATUS_ACTIVE}),
                         an.STATUS_PRESUMED_DEAD)


# ══════════════════════════════════════════════════════════════════════════
# workspace 按 cwd 隔离
# ══════════════════════════════════════════════════════════════════════════

class TestWorkspaceIsolation(Base):

    def test_different_cwd_different_slug(self) -> None:
        a = Path(tempfile.mkdtemp(prefix='wsa-'))
        b = Path(tempfile.mkdtemp(prefix='wsb-'))
        self.assertNotEqual(an.workspace_slug(a), an.workspace_slug(b))

    def test_same_cwd_stable_slug(self) -> None:
        a = Path(tempfile.mkdtemp(prefix='wsc-'))
        self.assertEqual(an.workspace_slug(a), an.workspace_slug(a))

    def test_agent_invisible_from_other_workspace(self) -> None:
        self.register(topics='here')
        other = Path(tempfile.mkdtemp(prefix='other-'))
        os.chdir(other)
        names = [aid for aid, _, _ in an.iter_agents(an.Ctx())]
        self.assertNotIn(self.agent_id, names, '跨 workspace 看得见对方 —— 隔离失效')


# ══════════════════════════════════════════════════════════════════════════
# 锁：互斥 / 租约懒过期 / 释放后不留僵尸目录
# ══════════════════════════════════════════════════════════════════════════

class TestLocks(Base):

    def test_mutual_exclusion(self) -> None:
        ws = self.ctx()
        ok1, _ = an.try_acquire_lock(ws, 'scpm', 'holder-A', 1, 'first', 600)
        ok2, held = an.try_acquire_lock(ws, 'scpm', 'holder-B', 2, 'second', 600)
        self.assertTrue(ok1)
        self.assertFalse(ok2, '两个持有者同时拿到了同一把锁')
        self.assertEqual(held['holder'], 'holder-A')

    def test_expired_lease_is_stealable(self) -> None:
        """孤儿锁必须能被自动接管——这正是文件锁方案原本最痛的地方。"""
        ws = self.ctx()
        an.try_acquire_lock(ws, 'scpm', 'dead-holder', 1, 'crashed', 0)
        self.assertTrue(an.lock_expired(an.read_lock(ws, 'scpm')))
        ok, _ = an.try_acquire_lock(ws, 'scpm', 'new-holder', 2, 'taking over', 600)
        self.assertTrue(ok, '过期锁没能被抢占')
        self.assertEqual(an.read_lock(ws, 'scpm')['holder'], 'new-holder')

    def test_lock_without_lease_treated_as_expired(self) -> None:
        """没有有效租约的锁宁可被抢走，也不要永久悬挂。"""
        self.assertTrue(an.lock_expired({'holder': 'x'}))

    def test_release_prunes_empty_dir(self) -> None:
        """只删锁文件不删目录，会攒出一堆'存在但空闲'的僵尸锁。"""
        ws = self.ctx()
        an.try_acquire_lock(ws, 'tmp', self.agent_id, 1, '', 600)
        an.release_lock(ws, 'tmp', self.agent_id)
        self.assertFalse(an.lock_dir(ws, 'tmp').exists(), '释放后留下了空锁目录')

    def test_release_by_non_holder_refused(self) -> None:
        ws = self.ctx()
        an.try_acquire_lock(ws, 'scpm', 'owner', 1, '', 600)
        ok, why = an.release_lock(ws, 'scpm', 'someone-else')
        self.assertFalse(ok)
        self.assertIn('持有', why)


# ══════════════════════════════════════════════════════════════════════════
# 投递：不重复消费
# ══════════════════════════════════════════════════════════════════════════

class TestDelivery(Base):

    def test_letter_consumed_exactly_once(self) -> None:
        """poll 与 Stop 钩子会同时抢同一个收件箱，只能有一方拿到。"""
        self.register()
        ctx = self.ctx()
        an.write_letter(ctx, 'sender-1', self.agent_id, '主题', '正文',
                        'letter', None, None, None)

        first = an.consume(ctx, an.inbox_letters(ctx, self.agent_id))
        second = an.consume(ctx, an.inbox_letters(ctx, self.agent_id))

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0, '同一封信被消费了两次')
        self.assertEqual(len(list((ctx.home / 'read').iterdir())), 1)

    def test_concurrent_filenames_never_collide(self) -> None:
        names = {an.letter_filename('sender-1') for _ in range(200)}
        self.assertEqual(len(names), 200, '文件名重复 —— 并发投递会互相覆盖')

    def test_unicode_subject_and_body_survive(self) -> None:
        self.register()
        ctx = self.ctx()
        an.write_letter(ctx, 'sender-1', self.agent_id, '中文主题：评审请求',
                        '正文含中文与 emoji ✅', 'review-request', 'thread-1', None, None)
        items = an.consume(ctx, an.inbox_letters(ctx, self.agent_id))
        meta, body, _ = items[0]
        self.assertEqual(meta['subject'], '中文主题：评审请求')
        self.assertIn('✅', body)
        self.assertEqual(meta['thread'], 'thread-1')


# ══════════════════════════════════════════════════════════════════════════
# 编码守卫：损坏的文本绝不落盘
# ══════════════════════════════════════════════════════════════════════════

class TestMojibakeGuard(Base):

    def test_clean_text_passes(self) -> None:
        for value in ('ascii only', '正常的中文', 'mixed 混合 123', ''):
            self.assertEqual(an.guard_text(value, 'x'), value)

    def test_utf8_read_as_gbk_is_rejected(self) -> None:
        """损坏形态之一：UTF-8 字节恰好是合法 GBK，于是解出一串"能读但不对"的字。

        样本取自真实事故——`重挂 poll` 经这条链路后变成 `閲嶆寕 poll`。
        """
        for original in ('重挂 poll', '测试', '审核', '异常'):
            corrupted = original.encode('utf-8').decode('gbk')
            self.assertNotEqual(corrupted, original, '样本没有真的损坏，测试无意义')
            with self.assertRaises(SystemExit, msg=f'{corrupted!r} 未被拒绝'):
                an.guard_text(corrupted, '参数 --subject')

    def test_replacement_char_is_rejected(self) -> None:
        """损坏形态之二：UTF-8 字节不是合法 GBK，退化成替换字符，编不回去。

        `中文` / `主题` 恰好属于这一半（实测），所以守卫**必须两条判据都有**才能全覆盖——
        只做 GBK 往返会漏掉这一半，只查替换字符会漏掉另一半。
        """
        for original in ('中文标记', '主题', '评审请求'):
            corrupted = original.encode('utf-8').decode('gbk', errors='replace')
            with self.assertRaises(SystemExit, msg=f'{corrupted!r} 未被拒绝'):
                an.guard_text(corrupted, '参数 --body')
        with self.assertRaises(SystemExit):
            an.guard_text('已经烂掉\ufffd的文本', '参数 --body')


# ══════════════════════════════════════════════════════════════════════════
# 控制台队列：只认固定动词
# ══════════════════════════════════════════════════════════════════════════

class TestConsoleAllowlist(Base):

    def test_unknown_verb_refused(self) -> None:
        ws = self.ctx()
        for verb in ('shell', 'exec', 'rm', 'spawn', 'run', ''):
            result = an.run_console_action(ws, {'verb': verb, 'target': 'x'})
            self.assertIn('拒绝', result, f'动词 `{verb}` 没有被拒绝')

    def test_allowlist_is_narrow(self) -> None:
        """能转发任意命令或控制进程的动词绝不能进白名单。"""
        for dangerous in ('run', 'spawn', 'hook', 'exec', 'shell'):
            self.assertNotIn(dangerous, an.CONSOLE_VERBS)


# ══════════════════════════════════════════════════════════════════════════
# 归档往返
# ══════════════════════════════════════════════════════════════════════════

class TestArchive(Base):

    def test_archive_moves_and_releases_locks(self) -> None:
        """死掉的持锁者会把所有人卡住 —— 归档必须连它的锁一起收。"""
        self.register()
        ctx = self.ctx()
        an.try_acquire_lock(ctx, 'scpm', self.agent_id, 1, 'holding', 600)

        released = an.archive_agent(ctx, self.agent_id, 'test', 'unit test')

        self.assertIn('scpm', released, '归档没有释放它持有的锁')
        self.assertIsNone(an.read_lock(ctx, 'scpm'))
        self.assertFalse(ctx.agent_dir(self.agent_id).exists())
        self.assertTrue((ctx.archive_dir / self.agent_id / 'info.md').exists())
        meta, _ = an.parse_doc(ctx.archive_dir / self.agent_id / 'info.md')
        self.assertEqual(meta['status'], an.STATUS_ARCHIVED)
        self.assertEqual(meta['archived_by'], 'test')

    def test_archive_preserves_unread_letters(self) -> None:
        self.register()
        ctx = self.ctx()
        an.write_letter(ctx, 'someone', self.agent_id, 's', 'b', 'letter', None, None, None)
        an.archive_agent(ctx, self.agent_id, 'test', 'unit test')
        inbox = ctx.archive_dir / self.agent_id / 'inbox'
        self.assertEqual(len(list(inbox.iterdir())), 1, '归档把未读信件弄丢了')


# ══════════════════════════════════════════════════════════════════════════
# README 与实现保持一致
# ══════════════════════════════════════════════════════════════════════════

class TestReadme(Base):

    def test_readme_lists_every_command(self) -> None:
        """README 是 agent 读了就照做的规范性文本，漏一条命令就是漏一条协议。"""
        text = an.render_readme()
        for cmd in an.COMMANDS:
            self.assertIn(f'`{cmd.name}`', text, f'README 里没有 {cmd.name}')

    def test_readme_warns_against_wildcard_permission(self) -> None:
        """这条警告是踩过坑换来的，不能在改版中丢掉。"""
        self.assertIn('Bash(agentnet:*)', an.render_readme())


if __name__ == '__main__':
    unittest.main(verbosity=2)
