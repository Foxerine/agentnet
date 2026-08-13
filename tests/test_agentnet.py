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

# 默认测仓库里那份；`AGENTNET_MODULE` 可指向**候选**文件，让改动在替换掉正在被
# 全网轮询器使用的那份之前先跑一遍测试——脚本一改，所有 poll 都会 RELOAD 重挂，
# 所以"边改边试"的代价是整网抖动，必须先验证后替换。
_MODULE = Path(os.environ.get('AGENTNET_MODULE') or _REPO / 'scripts' / 'agentnet.py')
_spec = importlib.util.spec_from_file_location('agentnet', _MODULE)
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

    def test_dead_pid_detected_before_heartbeat_expires(self) -> None:
        """假活：钩子先注册成功、主体进程随后崩溃 —— 心跳还新鲜，但那个 agent 不存在了。

        实测来源：ccrg 角色拉起即崩，SessionStart 已写好注册，花名册于是显示 active
        （17948ac6 报告）。等 5 分钟心跳超时太慢，pid 就在手边，直接查。
        """
        meta = {'status': an.STATUS_ACTIVE, 'last_active': an.now(), 'pid': 999999999}
        self.assertEqual(an.effective_status(meta), an.STATUS_ACTIVE,
                         '默认不查 pid，保持函数纯粹')
        self.assertEqual(an.effective_status(meta, verify_pid=True), an.STATUS_PRESUMED_DEAD,
                         'pid 已不存在却仍判为存活')

    def test_live_pid_stays_active(self) -> None:
        meta = {'status': an.STATUS_ACTIVE, 'last_active': an.now(), 'pid': os.getpid()}
        self.assertEqual(an.effective_status(meta, verify_pid=True), an.STATUS_ACTIVE)

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


class TestSkill(Base):
    """SKILL.md 是 agent 照着行动的文本，引用错了就是让它执行不存在的命令。

    断言的对象是**仓库里那份产物**（``_REPO/skill/SKILL.md``），而不是
    ``an.SKILL_PATH`` —— 后者由被测模块的 ``__file__`` 推导，测候选文件时会指到别处。
    """

    SKILL = _REPO / 'skill' / 'SKILL.md'

    def test_skill_file_exists(self) -> None:
        self.assertTrue(self.SKILL.exists(), f'缺少 {self.SKILL}')

    def test_skill_has_frontmatter_with_name_and_description(self) -> None:
        """Claude Code 靠 description 决定何时加载它——缺了就永远不会被触发。"""
        text = self.SKILL.read_text(encoding='utf-8')
        self.assertTrue(text.startswith('---\n'), 'skill 须以 YAML frontmatter 开头')
        head = text.split('---', 2)[1]
        self.assertIn('name: agentnet', head)
        self.assertRegex(head, r'description:\s*\S')

    def test_skill_references_only_real_commands(self) -> None:
        known = {cmd.name for cmd in an.COMMANDS}
        mentioned = set(an._SKILL_COMMAND_MENTION.findall(
            self.SKILL.read_text(encoding='utf-8')))
        self.assertEqual(sorted(mentioned - known), [])

    def test_guard_catches_a_renamed_command(self) -> None:
        """守卫必须在该响时真的响——只测当前文件干净，测不出这一点。"""
        known = {cmd.name for cmd in an.COMMANDS}
        self.assertNotIn('teleport', known)
        mentioned = set(an._SKILL_COMMAND_MENTION.findall('跑 `agentnet teleport --now`'))
        self.assertEqual(sorted(mentioned - known), ['teleport'])

    def test_guard_ignores_long_options(self) -> None:
        """`agentnet --help` 里的 --help 不是子命令。第一版正则把它当成了子命令，
        于是守卫在自己身上误报——误报会让人把守卫关掉，比没有守卫更糟。"""
        self.assertEqual(an._SKILL_COMMAND_MENTION.findall('`agentnet --help`'), [])

    def test_skill_path_points_beside_the_script(self) -> None:
        """部署形态是"仓库即运行时根目录"，skill 与 scripts/ 同级。"""
        self.assertEqual(an.SKILL_PATH.name, 'SKILL.md')
        self.assertEqual(an.SKILL_PATH.parent.name, 'skill')


# ══════════════════════════════════════════════════════════════════════════
# 拉起命令行的装配
# ══════════════════════════════════════════════════════════════════════════

class TestSpawnArgv(Base):
    """位置参数是新实例的"第一推动"——丢了它，spawn 表面成功、实例空转。

    这类失败极难查（进程活着、注册也在、就是不动），所以用例盯得比别处紧。
    """

    def build(self, blocked: list[str]) -> list[str]:
        return an.build_claude_argv(
            ['C:/x/claude.exe'], an.BOOTSTRAP_PROMPT, 'sid', 'nm', 'auto', blocked)

    def test_prompt_immediately_follows_executable(self) -> None:
        for blocked in ([], ['Edit', 'Write']):
            with self.subTest(blocked=blocked):
                self.assertEqual(self.build(blocked)[1], an.BOOTSTRAP_PROMPT)

    def test_variadic_flag_is_last(self) -> None:
        """``--disallowed-tools <tools...>`` 吞掉其后一切，所以它必须垫底。"""
        argv = self.build(['Edit', 'Write', 'NotebookEdit'])
        self.assertEqual(argv[-2], '--disallowed-tools')
        self.assertEqual(argv[-1], 'Edit,Write,NotebookEdit')

    def test_no_flag_omitted_when_nothing_blocked(self) -> None:
        """不禁用工具就别下这个选项——空值会被 commander 当成缺参数。"""
        self.assertNotIn('--disallowed-tools', self.build([]))

    def test_bootstrap_prompt_is_single_line_ascii(self) -> None:
        """多行 / 非 ASCII 会被 cmd.exe 与 commander 一路拆碎（实测崩在 `->`）。"""
        self.assertNotIn('\n', an.BOOTSTRAP_PROMPT)
        an.BOOTSTRAP_PROMPT.encode('ascii')   # 非 ASCII 会当场 UnicodeEncodeError

    def test_guard_fires_on_swallowed_positional(self) -> None:
        """守卫本身必须在该响的时候真的响——只测好输入测不出这个。"""
        bad = ['claude.exe', '--disallowed-tools', 'Edit', 'Run: agentnet drain']
        with self.assertRaises(RuntimeError) as caught:
            an.reject_swallowed_positional(bad, 3)
        self.assertIn('--disallowed-tools', str(caught.exception))

    def test_guard_accepts_correct_layout(self) -> None:
        argv = self.build(['Edit'])
        an.reject_swallowed_positional(argv, 1)   # 不应抛


class TestSpawnEnv(Base):

    def test_child_session_marker_not_inherited(self) -> None:
        """继承它会让被拉起的实例关掉 transcript 保存，既不可 resume 也难追溯。

        被拉起的是独立会话，不是调用方的子会话。
        """
        base = {'CLAUDE_CODE_CHILD_SESSION': '1', 'PATH': '/usr/bin'}
        env = an.build_child_env(base, 'aid', None, {})
        self.assertNotIn('CLAUDE_CODE_CHILD_SESSION', env)
        self.assertEqual(env['PATH'], '/usr/bin', '不该顺手丢掉无关变量')

    def test_identity_is_injected(self) -> None:
        env = an.build_child_env({}, 'aid-7', 'a,b', {})
        self.assertEqual(env['AGENTNET_ID'], 'aid-7')
        self.assertEqual(env['AGENTNET_TOPICS'], 'a,b')

    def test_absent_topics_leaves_no_empty_var(self) -> None:
        """空字符串是合法值，不是"未提供"——别用它表达缺失。"""
        self.assertNotIn('AGENTNET_TOPICS', an.build_child_env({}, 'aid', None, {}))

    def test_role_env_overrides_inherited(self) -> None:
        """角色 env 表达"同一个 CLI、不同后端"，必须盖住继承来的同名变量。

        盖不住的话，调用方的后端配置会漏进子实例——reviewer 就不再是换模型的
        独立评审，而是作者自己的模型换了个名字。
        """
        base = {'ANTHROPIC_BASE_URL': 'https://api.anthropic.com'}
        env = an.build_child_env(base, 'aid', None,
                                 {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:3456'})
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:3456')

    def test_base_mapping_not_mutated(self) -> None:
        """传进来的常常就是 os.environ，改了它会污染当前进程。"""
        base = {'PATH': '/usr/bin'}
        an.build_child_env(base, 'aid', 'x', {'FOO': '1'})
        self.assertEqual(base, {'PATH': '/usr/bin'})


# ══════════════════════════════════════════════════════════════════════════
# 归档后的空壳复活
# ══════════════════════════════════════════════════════════════════════════

class TestHollowShell(Base):
    """归档一个**仍在运行**的 agent 时，它那个还没退位的轮询器不得把目录写回来。

    实测事故（04b27904）：看板归档后，旧轮询器的心跳在 `agents/<id>/` 重建出一个
    只有 `last_active` 的目录。真历史连同未读信搁死在 archive/，花名册上却站着
    一个没有身份字段的幽灵，此后投给它的信也落进幽灵里。
    """

    def test_heartbeat_does_not_resurrect_archived_agent(self) -> None:
        path = self.tmp / 'gone' / 'info.md'
        self.assertIsNone(an.merge_info(path, {'last_active': an.now()}, create=False))
        self.assertFalse(path.exists(), '登记已归档，心跳不得凭空建出空壳')

    def test_create_true_still_creates(self) -> None:
        """默认行为不变——register 这类首次写入仍要能建文件。"""
        path = self.tmp / 'fresh' / 'info.md'
        path.parent.mkdir(parents=True)
        self.assertIsNotNone(an.merge_info(path, {'id': 'x'}))
        self.assertTrue(path.exists())

    def test_hollow_shell_is_displaced(self) -> None:
        shell_dir = self.tmp / 'agents' / 'aid'
        (shell_dir / 'inbox').mkdir(parents=True)
        an.merge_info(shell_dir / 'info.md', {'last_active': an.now()})   # 空壳：无 id
        (shell_dir / 'inbox' / 'stray.md').write_text('落进幽灵的信', encoding='utf-8')

        moved = an.displace_hollow_shell(shell_dir, 'aid')
        self.assertIsNotNone(moved)
        self.assertFalse(shell_dir.exists(), '空壳应被挪开，给恢复让路')

    def test_real_registration_is_never_displaced(self) -> None:
        """有 id = 真登记，绝不能被当成空壳挪走。"""
        real = self.tmp / 'agents' / 'aid2'
        real.mkdir(parents=True)
        an.merge_info(real / 'info.md', {'id': 'aid2', 'last_active': an.now()})
        with self.assertRaises(RuntimeError):
            an.displace_hollow_shell(real, 'aid2')
        self.assertTrue((real / 'info.md').exists())

    def test_displace_raises_ordinary_exception(self) -> None:
        """必须是 Exception 而非 SystemExit——看板动作在 except Exception 里逐条跑，
        SystemExit 会穿过它把整个轮询器带走。"""
        real = self.tmp / 'agents' / 'aid3'
        real.mkdir(parents=True)
        an.merge_info(real / 'info.md', {'id': 'aid3'})
        with self.assertRaises(Exception) as caught:
            an.displace_hollow_shell(real, 'aid3')
        self.assertNotIsInstance(caught.exception, SystemExit)

    def test_absorb_shell_rescues_letters(self) -> None:
        """空壳没有身份，却可能已经收到信——直接删就是丢信。"""
        shell = self.tmp / 'shell'
        (shell / 'inbox').mkdir(parents=True)
        (shell / 'inbox' / 'a.md').write_text('信 A', encoding='utf-8')
        (shell / 'inbox' / 'b.md').write_text('信 B', encoding='utf-8')
        destination = self.tmp / 'restored'
        (destination / 'inbox').mkdir(parents=True)

        self.assertEqual(an.absorb_shell(shell, destination), (2, None))
        self.assertEqual(
            sorted(p.name for p in (destination / 'inbox').glob('*.md')), ['a.md', 'b.md'])
        self.assertFalse(shell.exists(), '搬空后空壳应被删除')

    def test_absorb_shell_ignores_own_stub_info(self) -> None:
        """空壳自己那份残缺 info.md 不算"内容"——它正是空壳的定义。

        把它算进遗留物会让告警**永远**触发（实测：第一次真实恢复就误报了）。
        """
        shell = self.tmp / 'shell-stub'
        (shell / 'inbox').mkdir(parents=True)
        an.merge_info(shell / 'info.md', {'last_active': an.now()})

        self.assertEqual(an.absorb_shell(shell, self.tmp / 'dst'), (0, None))
        self.assertFalse(shell.exists(), '只剩残缺 info.md 时应当清干净')

    def test_absorb_shell_keeps_shell_when_data_remains(self) -> None:
        """搬不干净就留着并如实返回，不静默删除任何数据。"""
        shell = self.tmp / 'shell2'
        (shell / 'nested').mkdir(parents=True)
        (shell / 'nested' / 'weird.md').write_text('不在 inbox 里的东西', encoding='utf-8')
        destination = self.tmp / 'restored2'
        destination.mkdir()

        self.assertEqual(an.absorb_shell(shell, destination), (0, shell))
        self.assertTrue(shell.exists(), '还有数据没搬走时不得删除空壳')


if __name__ == '__main__':
    unittest.main(verbosity=2)
