"""AgentNet 不变式测试。

    python -m unittest discover -s tests -v
    # 或
    python tests/test_agentnet.py

只用标准库（与被测代码同样的约束）。**测的是"错了不会立刻报错、只会静默出错"的那些不变式**——
这类缺陷不会在人眼前崩溃，只会在几小时后表现为"我写的职责声明不见了"或"两个人同时拿到了锁"。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
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

    def test_dead_pid_alone_never_downgrades(self) -> None:
        """死掉的 `pid` **不**构成死亡证据——这条契约在 2026-08-20 被反转过。

        原契约是"pid 没了就提前判死"，为的是快点抓住"注册成功后崩掉的壳"
        （17948ac6 报告的 ccrg 拉起即崩）。但同一个判据连续三次误杀活着的实例：
        `resolve_pid` 拿不到 `CLAUDE_PID` 时回退 `os.getppid()`，那是短命 CLI 进程的
        父进程，**天生随手就死**。用一个天生不可靠的信号做不可逆的判定，方向就是错的。

        壳仍然会被抓到，改由心跳超时兜底（见
        `TestLivenessFailsOpen.test_crashed_shell_is_caught_by_heartbeat_not_by_pid`）。
        """
        stale = an.now() - timedelta(seconds=an.dead_after_s() - 30)
        meta = {'status': an.STATUS_ACTIVE, 'last_active': stale, 'pid': 999999999}
        self.assertEqual(an.effective_status(meta), an.STATUS_ACTIVE,
                         '默认不查 pid，保持函数纯粹')
        self.assertEqual(an.effective_status(meta, verify_pid=True), an.STATUS_ACTIVE,
                         '死 pid 单独不足以判死——心跳还在阈值内')

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

class TestAtomicWriteUnderContention(Base):
    """Windows 上 `os.replace` 会被"目标正被读取"挡住（POSIX 不会）。

    一个 agent 同时跑着多个轮询器是**被容忍**的状态（harness 的退出通知乱序到达，
    实例照旧通知重挂就会短暂并存），每个都在秒级读写同一份 info.md——所以原子写
    必须扛住这一下，而不是要求调用方保证单写者。
    """

    def test_retries_past_transient_permission_error(self) -> None:
        target = self.tmp / 'info.md'
        real_replace = an.os.replace
        attempts: list[int] = []

        def flaky(src: object, dst: object) -> None:
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError(13, '目标正被另一个进程读取')
            real_replace(src, dst)

        an.os.replace = flaky
        try:
            an._atomic_write(target, '内容')
        finally:
            an.os.replace = real_replace

        self.assertEqual(target.read_text(encoding='utf-8'), '内容')
        self.assertEqual(len(attempts), 3, '应当重试到成功')

    def test_leaves_no_temp_file_when_giving_up(self) -> None:
        """退避耗尽后必须响亮失败，且不留下看着像正常产物的临时文件。"""
        target = self.tmp / 'info.md'
        real_replace = an.os.replace

        def always_locked(src: object, dst: object) -> None:
            raise PermissionError(13, '一直被占')

        an.os.replace = always_locked
        try:
            with self.assertRaises(PermissionError):
                an._atomic_write(target, '内容')
        finally:
            an.os.replace = real_replace

        self.assertEqual(list(self.tmp.glob('*.tmp.*')), [], '失败路径必须清理临时文件')

    def test_read_retries_past_transient_permission_error(self) -> None:
        """读侧也要重试——MoveFileEx 替换的那一瞬，读者一样打不开这个路径。

        这一侧比写侧影响大：read_info 在每个轮询器的每一轮、每条命令启动时、
        看板每次刷新时都会调用，不重试就等于"别人恰好在写"能掀掉一条无关命令。
        """
        target = self.tmp / 'info.md'
        target.write_text('内容', encoding='utf-8')
        real_read = an.Path.read_text
        attempts: list[int] = []

        def flaky(self_path: object, **kwargs: object) -> str:
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError(13, '文件正被替换')
            return real_read(self_path, **kwargs)

        an.Path.read_text = flaky
        try:
            self.assertEqual(an.read_text_with_retry(target), '内容')
        finally:
            an.Path.read_text = real_read
        self.assertEqual(len(attempts), 3, '应当重试到成功')

    def test_unlink_retries_past_transient_permission_error(self) -> None:
        """删除侧是这个竞态的**第三张面孔**，先前漏了——`release_lock` 的 unlink 撞上
        WinError 32，异常炸穿轮询器主循环，直接把实例打下线。"""
        target = self.tmp / 'current.lock'
        target.write_text('x', encoding='utf-8')
        real_unlink = an.Path.unlink
        attempts: list[int] = []

        def flaky(self_path: object, **kwargs: object) -> None:
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError(32, '另一个程序正在使用此文件')
            real_unlink(self_path, **kwargs)

        an.Path.unlink = flaky
        try:
            an.unlink_with_retry(target)
        finally:
            an.Path.unlink = real_unlink
        self.assertFalse(target.exists())
        self.assertEqual(len(attempts), 3)

    def test_opportunistic_failure_does_not_propagate(self) -> None:
        """sweep / 看板刷新失败**不能**停掉轮询器——它们的重要性远低于收信与心跳。"""
        def boom() -> None:
            raise PermissionError(32, '锁文件被占')

        an.run_opportunistic(boom, 'sweep')   # 不抛即通过

    def test_opportunistic_failure_is_reported(self) -> None:
        """宽 catch 但**不静默**——否则一个一直失败的 sweep 会无人察觉。"""
        import io
        import contextlib as ctxlib
        buffer = io.StringIO()
        with ctxlib.redirect_stdout(buffer):
            an.run_opportunistic(lambda: (_ for _ in ()).throw(RuntimeError('炸了')), 'sweep')
        self.assertIn('sweep', buffer.getvalue())
        self.assertIn('炸了', buffer.getvalue())

    def test_does_not_swallow_other_errors(self) -> None:
        """源文件不见了是真错误，不该被当成"再等等就好"。"""
        real_replace = an.os.replace

        def missing(src: object, dst: object) -> None:
            raise FileNotFoundError(2, '源文件不存在')

        an.os.replace = missing
        try:
            with self.assertRaises(FileNotFoundError):
                an._atomic_write(self.tmp / 'info.md', '内容')
        finally:
            an.os.replace = real_replace

    def test_concurrent_writers_all_succeed(self) -> None:
        """并发读写同一个文件：每一次都必须落盘成功，内容是其中某一个的全文。

        **读侧必须走 `read_text_with_retry`**——那正是 agentnet 到处在用的读法。
        用裸 `read_text` 的话这个用例会偶发失败，而失败的是**测试自己的读**，
        不是被测代码：`MoveFileEx` 在替换的那一瞬让该路径打不开，读者一样吃
        PermissionError。第一版就是这么误判的，还以为是退避预算不够
        （实测写侧最多重试 4 次即成功，预算绰绰有余）。
        """
        target = self.tmp / 'info.md'
        errors: list[BaseException] = []

        def hammer(tag: str) -> None:
            for _ in range(10):
                try:
                    an._atomic_write(target, f"holder={tag}\n")
                    an.read_text_with_retry(target)      # 制造"正在读"的窗口
                except BaseException as exc:             # noqa: BLE001
                    errors.append(exc)
                time.sleep(0.01)

        threads = [threading.Thread(target=hammer, args=(f"w{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [], f'并发写不应报错，实得：{errors[:3]}')
        self.assertRegex(target.read_text(encoding='utf-8'), r'^holder=w\d\n$')


class TestReviewTerminalState(Base):
    """评审用信件表达时，唯一不是免费得到的性质是"这轮结束了没、结论是什么"。

    其余三条由构造满足：轮次校验＝只能回复收到的信；append-only＝一封一文件；
    单次原子写＝文件名唯一。所以只加两个 kind，不加线程状态字段。
    """

    def test_terminal_kinds_are_accepted_by_cli(self) -> None:
        for kind in ('review-resolved', 'review-blocked'):
            self.assertIn(kind, an.LETTER_KINDS, f'{kind} 不在允许的 kind 里，send 会被拒')

    def test_terminal_set_matches_the_kinds(self) -> None:
        """判据集合与允许值必须同源，否则会出现"发得出去但判不出终态"的 kind。"""
        self.assertTrue(an.TERMINAL_REVIEW_KINDS.issubset(set(an.LETTER_KINDS)))

    def test_ongoing_review_kinds_are_not_terminal(self) -> None:
        for kind in ('review-request', 'review-reply'):
            self.assertNotIn(kind, an.TERMINAL_REVIEW_KINDS, f'{kind} 不该被当成终态')


class TestLetterHeadline(Base):
    """收信输出的**第一行**必须自带结论。

    实测事故（2026-08-14，`0de75e6c` 复盘）：它漏读了至少 6 封已送达的信。根因不在它——
    收信与 `[RELOAD]` 共用同一条退出路径，而 RELOAD 频率高一个数量级，于是形成
    `head -3` 看退出原因的习惯；那对 RELOAD 够（首行即 `[RELOAD]`），对收信恰好不够
    （原先前三行是分隔线、警告块、"共 N 封"，正文在第 5 行之后）。
    """

    def letters(self, count: int = 1) -> list[tuple[dict, str, Path]]:
        return [({'from': f'sender{i}0000', 'kind': 'letter', 'subject': f'主题{i}',
                  'thread': 't'}, f'正文第一行\n正文第二行\n', Path(f'l{i}.md'))
                for i in range(count)]

    def test_first_line_announces_a_letter(self) -> None:
        first = an.render_letters(self.letters()).splitlines()[0]
        self.assertTrue(first.startswith('[LETTER]'), f'首行没有自带结论：{first!r}')

    def test_first_line_survives_head_3(self) -> None:
        """判据必须放在**任何粗略查看都会撞见**的位置——这就是那次失效的直接教训。"""
        head = '\n'.join(an.render_letters(self.letters()).splitlines()[:3])
        self.assertIn('[LETTER]', head)
        self.assertIn('必须读完', head, 'head -3 里看不到"要读完"就等于没说')

    def test_first_line_carries_sender_and_subject(self) -> None:
        first = an.render_letters(self.letters()).splitlines()[0]
        self.assertIn('sender00', first)
        self.assertIn('主题0', first)

    def test_reload_and_letter_differ_on_line_one(self) -> None:
        """两者共用退出路径，唯一能机械区分的就是首行——不能长得像。"""
        letter_first = an.render_letters(self.letters()).splitlines()[0]
        self.assertNotIn('[RELOAD]', letter_first)
        self.assertTrue(letter_first.startswith('[LETTER]'))


class TestSpawnWindowTargeting(Base):
    """"开在发起方当前窗口"是平台限制，不是偶发失败——措辞与默认值都要如实反映。

    实测（`0de75e6c` 报告）：6 次 spawn **6 次**降级。两条退路都验死了——`WT_WINDOWID`
    未实现；进程树反查也不行（本机一个 WindowsTerminal.exe 托管 23 个窗口）。
    """

    def build(self, window: str) -> tuple:
        return an.build_launch(mode='tab', window=window, title='t', cwd='C:/x',
                               child=['claude'], slug='ws-1', allow_focus=False)

    def test_default_targets_the_shared_window(self) -> None:
        """默认不再去试"当前窗口"——成功率≈0，而尝试要抢前台、成功了反而夺走用户焦点。"""
        _, _, target, notes = self.build('shared')
        self.assertEqual(target, an.workspace_window_name('ws-1'))
        self.assertEqual(notes, [], '默认路径不该报告任何降级——它本来就不是降级')

    def test_explicit_name_is_honoured(self) -> None:
        _, _, target, _ = self.build('my-window')
        self.assertEqual(target, 'my-window')

    def test_limit_is_stated_as_a_limit_not_a_failure(self) -> None:
        """措辞决定调用方会不会反复重试、会不会向用户承诺做不到的事。"""
        self.assertIn('WT_WINDOWID', an.WINDOW_TARGETING_LIMIT)
        for misleading in ('未能', '失败', '重试'):
            self.assertNotIn(misleading, an.WINDOW_TARGETING_LIMIT,
                             f'限制说明里不该出现暗示"下次可能成功"的字眼：{misleading}')


class TestRearmNotice(Base):

    def test_explains_that_kills_are_expected(self) -> None:
        """轮询器被杀是**上游已知限制**（compact/会话结束时 SIGTERM 所有被追踪任务，
        无豁免机制）。不说清楚，实例会误判成"环境有问题"而放弃重挂。

        实测两个实例都栽在这：一个断定「挂不住」停止重试 ⇒ 永久离线；
        另一个把 `killed` 当成"有人在清理"，白停一轮。
        """
        self.assertIn('压缩', an.REARM_NOTICE, '要点明 compact 是成因之一')
        self.assertIn('重挂即可', an.REARM_NOTICE)

    def test_tells_you_to_verify_before_rearming(self) -> None:
        """收到 killed 通知**先查 whoami**——那个词也覆盖"正常退出"和"其实还活着"。

        实测：连报两次 killed，而 `whoami` 显示轮询器正常运行（pid 369724）。
        见状就重挂会顶掉正在跑的那个：新的接管、旧的写 `[退位]` 退出、又产生一条
        新的 killed 通知——**盲目响应会制造它试图修复的那个混乱**。
        """
        self.assertIn('whoami', an.REARM_NOTICE)
        self.assertIn('只有它说未运行才重挂', an.REARM_NOTICE)

    def test_states_the_fallback(self) -> None:
        """没有轮询器**仍然收得到信**（Stop 钩子每回合 drain）——

        不说这条，实例会把"降级"误当成"失效"，代价是主动退出网络。
        """
        self.assertIn('仍然收得到信', an.REARM_NOTICE)

    def test_warns_against_self_backgrounding(self) -> None:
        """只讲"脚本 spawn 后继进程"这一种形态，读者要自己完成类比才受益——

        而需要读者补一步推理的警告等于没警告：实测有人读过原文后仍用 `poll &` 踩坑。
        """
        self.assertIn('&', an.REARM_NOTICE)
        self.assertIn('nohup', an.REARM_NOTICE)


class TestUnarmedPollerIsBlocking(Base):
    """**不让回合在未挂载轮询器的状态下结束。**

    Stop 钩子触发的时刻，恰好是 agent 可能转入空闲的那一刻——而"空闲 + 没有轮询器"
    是唯一真正有害的组合：此后叫不醒它，5 分钟后判死、别人投信被拒。
    干活期间没有轮询器**不损失什么**（钩子每回合 drain、心跳随命令刷新），
    所以拦截的目的不是止损，是**别在进入空闲前留下这个缺口**。
    """

    def hook_output(self) -> dict:
        import argparse, io, contextlib, json
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            an.cmd_drain(argparse.Namespace(hook=True, no_block=False))
        return json.loads(buffer.getvalue() or '{}')

    def go_offline(self) -> None:
        an.merge_info(self.ctx().info_path, {'poller_pid': 999999})   # 不存在的 pid

    def test_blocks_repeatedly_not_just_once(self) -> None:
        """拦一次仍然依赖 agent 记得——而用户报告的正是"总是忘记"。"""
        self.register()
        self.go_offline()
        for attempt in range(an.UNARMED_BLOCK_LIMIT):
            self.assertEqual(self.hook_output().get('decision'), 'block',
                             f'第 {attempt + 1} 次应仍然拦截')

    def test_gives_up_after_the_limit(self) -> None:
        """真的挂不上的实例不该被无限挡在回合里。"""
        self.register()
        self.go_offline()
        for _ in range(an.UNARMED_BLOCK_LIMIT):
            self.hook_output()
        self.assertNotIn('decision', self.hook_output(), '超过上限后必须放行')

    def test_arming_resets_the_counter(self) -> None:
        """挂上后计数清零，下次掉线要能重新拦满 N 次。"""
        self.register()
        self.go_offline()
        self.hook_output()
        an.merge_info(self.ctx().info_path, {'poller_pid': os.getpid()})
        self.hook_output()                                    # armed ⇒ 清零
        self.go_offline()
        self.assertEqual(self.hook_output().get('decision'), 'block')

    def test_continuation_turn_never_blocks(self) -> None:
        """harness 的 stop_hook_active 为真时绝不能再 block，否则两边互相续命。"""
        import argparse, io, contextlib, json
        self.register()
        self.go_offline()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            an.cmd_drain(argparse.Namespace(hook=True, no_block=True))
        self.assertNotIn('decision', json.loads(buffer.getvalue() or '{}'))

    def test_message_does_not_overstate_the_loss(self) -> None:
        """夸大损失会损耗可信度：干活期间没有轮询器其实照常收信。

        说"空闲时收不到信"是对的，但先前那句"空闲时收不到信 + 立刻判死"读起来像
        此刻就在丢东西——而实际此刻毫无损失。措辞要把"现在没事"和"进入空闲就有事"分开。
        """
        self.register()
        self.go_offline()
        text = self.hook_output()['hookSpecificOutput']['additionalContext']
        self.assertIn('现在还没事', text)
        self.assertIn('转入空闲', text)
        self.assertIn('结束本回合之前', text)

    def test_counter_field_is_persisted(self) -> None:
        self.assertIn('unarmed_blocks', an.INFO_FIELD_ORDER)


class TestUnackedLetterSafetyNet(Base):
    """投递 ≠ 送达：poll 把全文打进后台输出文件，而**读那个文件是可跳过的环节**。

    实测两次（`0de75e6c`）。第二次是在首行改成 `[LETTER] …必须读完` **之后**——
    首行修复覆盖的是"看了输出仍漏读"，那次是**压根没打开输出**：在 harness 的通知层，
    收信退出与任何后台任务完成长得一样，忙起来会整批跳过。漏了 4 封。

    所以把送达保证挪到唯一跳不过的通道：Stop 钩子每回合必然触发。
    """

    def hook_output(self) -> dict:
        import argparse, io, contextlib, json
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            an.cmd_drain(argparse.Namespace(hook=True, no_block=False))
        return json.loads(buffer.getvalue() or '{}')

    def deliver(self, subject: str = '关键前提被质疑') -> None:
        """模拟轮询器投递了一封信（登记为未确认）。"""
        an.record_unacked(self.ctx(), [({'from': 'aaaa1111-x', 'subject': subject},
                                        '正文', Path('x.md'))])

    def test_unacked_letter_blocks_at_stop(self) -> None:
        self.register()
        an.merge_info(self.ctx().info_path, {'poller_pid': os.getpid()})  # 轮询器在跑
        self.deliver()
        payload = self.hook_output()
        self.assertEqual(payload.get('decision'), 'block',
                         '未确认的信必须打断——不 block 就只是又一张会被跳过的便签')

    def test_listing_names_the_sender_and_subject(self) -> None:
        """得说清**漏了哪封**，否则 agent 无从判断要不要补看。"""
        self.register()
        an.merge_info(self.ctx().info_path, {'poller_pid': os.getpid()})
        self.deliver('关键前提被质疑')
        text = self.hook_output()['hookSpecificOutput']['additionalContext']
        self.assertIn('aaaa1111', text)
        self.assertIn('关键前提被质疑', text)
        self.assertIn('agentnet last --full', text, '要给出补看的具体命令')

    def test_reminder_fires_only_once(self) -> None:
        """已读过的人不该被反复打断——提醒一次即清。"""
        self.register()
        an.merge_info(self.ctx().info_path, {'poller_pid': os.getpid()})
        self.deliver()
        self.hook_output()
        self.assertNotIn('decision', self.hook_output())

    def test_poll_delivery_registers_unacked(self) -> None:
        """真正的接线：consume 之后必须登记，否则兜底通道永远收不到东西。"""
        self.register()
        ctx = self.ctx()
        an.record_unacked(ctx, [({'from': 'bbbb2222-y', 'subject': 's'}, 'b', Path('y.md'))])
        self.assertEqual(len(an.read_info(ctx.info_path)[0]['unacked_letters']), 1)

    def test_field_is_persisted(self) -> None:
        self.assertIn('unacked_letters', an.INFO_FIELD_ORDER)


class TestPollerRetiresOnTerminalStatus(Base):
    """已终止的 agent 不得因为轮询器还在刷心跳而"假装活着"。

    实测：`6355c527` 的 `status = "exited"`，它的轮询器却仍每 5 分钟刷新 `last_active`
    ——花名册上它看起来一直健在，别人会把活派给一个不存在的实例。

    本设计的核心等价关系是「心跳停 ⟺ 收不到信 ⟺ 事实上已死」；漏掉这一支就把它打破了。
    退位此前只覆盖两种情形（登记文件消失、被新轮询器接管），少了第三种：**自己进了终态**。
    """

    def test_terminal_statuses_are_the_existing_set(self) -> None:
        """复用既有常量，不另造同义集合——两套定义迟早会分叉。"""
        self.assertEqual(an.TERMINAL_STATUSES, frozenset({'exited', 'archived'}))

    def test_exited_agent_is_not_counted_alive(self) -> None:
        """终态不按心跳推算——即便 last_active 是刚刚。"""
        meta = {'status': an.STATUS_EXITED, 'last_active': an.now()}
        self.assertEqual(an.effective_status(meta), an.STATUS_EXITED)

    def test_retires_when_own_status_is_terminal(self) -> None:
        """守卫要在该响时真的响：exited + 自己仍是在册轮询器 = 必须退位。"""
        self.register()
        ctx = self.ctx()
        me = os.getpid()
        an.merge_info(ctx.info_path, {'status': an.STATUS_EXITED,
                                      'poller_pid': me, 'last_active': an.now()})
        reason = an.retirement_reason(ctx, me)
        self.assertIsNotNone(reason, 'exited 的 agent 其轮询器必须退位，否则造出假活')
        self.assertIn('exited', str(reason))

    def test_keeps_running_while_active(self) -> None:
        """反向：正常在册时不能误退位，否则谁都收不到信。"""
        self.register()
        ctx = self.ctx()
        me = os.getpid()
        an.merge_info(ctx.info_path, {'status': an.STATUS_ACTIVE, 'poller_pid': me})
        self.assertIsNone(an.retirement_reason(ctx, me))

    def test_retires_when_superseded(self) -> None:
        self.register()
        ctx = self.ctx()
        an.merge_info(ctx.info_path, {'status': an.STATUS_ACTIVE, 'poller_pid': 424242})
        self.assertIn('接管', str(an.retirement_reason(ctx, os.getpid())))

    def test_retires_when_registration_gone(self) -> None:
        self.register()
        ctx = self.ctx()
        ctx.info_path.unlink()
        self.assertIn('归档', str(an.retirement_reason(ctx, os.getpid())))


class TestErrandFraming(Base):
    """任务简报必须说清"收到 ≠ 完成"，否则实例会把读信当交付。"""

    def test_errand_note_demands_delivery(self) -> None:
        note = an.TRUST_NOTE_ERRAND
        self.assertIn('回信', note, '要说明做完必须回信')
        self.assertIn('agentnet reply', note, '要给出具体命令')
        self.assertIn('准备工作', note, '要点破挂 poll / 报状态只是准备')

    def test_ordinary_letter_note_stays_untrusted(self) -> None:
        """别把这层放宽误伤到同僚来信——那仍是不可信输入。"""
        self.assertIn('不可信', an.TRUST_NOTE_LETTER)
        self.assertNotIn('请执行', an.TRUST_NOTE_LETTER)


class TestDashboardDirectoryPicker(Base):
    """选目录那一步是**人给写权限**的边界，不能因为难用而被绕开或选错。

    实测：用户点 sweep 时看到「无法打开此文件夹，因为其中含有系统文件」——
    那是浏览器拒绝了**家目录**。`.agentnet` 是点开头的目录，在选择器里不显眼，
    停在上一级就点"选择"几乎是必然。提示语当时没给路径。
    """

    def test_template_has_a_placeholder_for_the_root(self) -> None:
        self.assertIn('__AGENTNET_ROOT__', an.DASHBOARD_TEMPLATE,
                      '路径要烘进页面，否则提示语说不出该选哪个目录')

    def test_generated_page_carries_the_absolute_path(self) -> None:
        import argparse
        an.cmd_dashboard(argparse.Namespace(open=False))
        html = (an.ROOT / an.DASHBOARD_HTML).read_text(encoding='utf-8')
        self.assertNotIn('__AGENTNET_ROOT__', html, '占位符没被替换')
        self.assertIn('agentnet-test-', html, '页面里应含真实根目录路径')

    def test_backslashes_are_escaped_for_javascript(self) -> None:
        """Windows 路径进 JS 字符串必须转义，否则 `\\U` 之类会把页面整崩。"""
        import argparse
        an.cmd_dashboard(argparse.Namespace(open=False))
        html = (an.ROOT / an.DASHBOARD_HTML).read_text(encoding='utf-8')
        line = next(l for l in html.splitlines() if 'AGENTNET_ROOT_PATH =' in l)
        self.assertNotRegex(line, r"(?<!\\)\\(?!\\)", f'路径里有未转义的反斜杠：{line}')

    def test_handle_is_persisted_across_reloads(self) -> None:
        """句柄只存页面变量的话，"只需选这一次"其实是每次刷新都问一次。"""
        self.assertIn('indexedDB', an.DASHBOARD_TEMPLATE)
        self.assertIn('queryPermission', an.DASHBOARD_TEMPLATE)

    def test_error_message_names_the_actual_cause(self) -> None:
        """报错要指向真正的成因（选到上级），而不是只把浏览器原话丢回来。"""
        self.assertIn('含有系统文件', an.DASHBOARD_TEMPLATE)
        self.assertIn('上级', an.DASHBOARD_TEMPLATE)

    def test_generated_javascript_actually_parses(self) -> None:
        """用**真解析器**验生成物，而不是肉眼看模板。

        实测踩过：模板里的 `\\n` 在非 raw 字符串里变成真换行，落进 JS 单引号字符串
        导致整页白屏 `Invalid or unexpected token`——而当时我核对的是**模板源码**，
        看不出来。判据必须落在**生成出来的那份**上。

        node 不在就跳过：这是加强验证，不该让没装 node 的环境无法跑测试。
        """
        import argparse
        import re
        import shutil as sh
        import subprocess
        node = sh.which('node')
        if not node:
            self.skipTest('未安装 node，跳过 JS 语法校验')
        an.cmd_dashboard(argparse.Namespace(open=False))
        html = (an.ROOT / an.DASHBOARD_HTML).read_text(encoding='utf-8')
        blocks = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
        self.assertTrue(blocks, '页面里没有 script 块')
        probe = self.tmp / 'dashboard_script.js'
        probe.write_text(blocks[0], encoding='utf-8')
        done = subprocess.run([node, '--check', str(probe)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, f'生成的 JS 语法有错：\n{done.stderr[:600]}')


class TestTerminalExitCode(Base):
    """被 kill 掉的 agent 不该留下一个关不掉的分页；崩掉的**必须**留下。

    `agentnet kill` 用 `taskkill /F`，那给出退出码 1（实测）；而 Windows Terminal 的
    `closeOnExit: graceful` 只在 0 时关标签页（实测本机就是这个配置）——于是被杀的
    实例留下死分页。`wt` 没有单次调用级的 closeOnExit 覆盖（已查证），
    所以只能让分页里的顶层进程自己退出 0。
    """

    def test_zero_passes_through(self) -> None:
        self.assertEqual(an.exit_code_for_terminal(self.agent_id, 0), 0)

    def test_deliberate_kill_becomes_zero(self) -> None:
        self.register()
        an.merge_info(self.ctx().info_path, {'status': an.STATUS_EXITED})
        self.assertEqual(an.exit_code_for_terminal(self.agent_id, 1), 0,
                         '被有意结束的实例应让分页自动关闭')

    def test_archived_agent_also_becomes_zero(self) -> None:
        """`exit` / `sweep` 会把整个目录搬进 archive/，登记文件就不在原处了。"""
        import argparse
        self.register()
        an.cmd_exit(argparse.Namespace())
        self.assertEqual(an.exit_code_for_terminal(self.agent_id, 1), 0)

    def test_crash_keeps_its_exit_code(self) -> None:
        """**这条是重点**：一律返回 0 会把崩溃现场也关掉——

        拿一个 UX 小病换一个诊断大病。登记还是 active ⇒ 没人下过手 ⇒ 就是崩了。
        """
        self.register()
        started = time.monotonic()
        self.assertEqual(an.exit_code_for_terminal(self.agent_id, 3), 3,
                         '没人 kill 它，说明是崩溃，分页必须留着')
        self.assertLess(time.monotonic() - started, an.DELIBERATE_EXIT_GRACE_S + 3,
                        '等待不该远超宽限')

    def test_unknown_agent_passes_through(self) -> None:
        """读不出登记就别猜——原样透传比猜错好。"""
        self.assertEqual(an.exit_code_for_terminal('no-such-agent-id', 7), 7)


class TestOrphanedWait(Base):
    """author 等一个已经死掉的 reviewer，是这套协议里唯一会**永远卡住**的状态。

    poll 刻意无超时（标准流程强制：对方一轮可能耗时数小时），所以没有任何东西会打破它。
    reviewer 崩掉 / 被杀 / 被 sweep 归档后，那封回信永远不会来，而 author
    **不知道自己在等一个死人**。
    """

    def spawn_child(self, name: str = 'reviewer-1') -> str:
        """造一个"我拉起的、活着的"子实例登记。"""
        child_id = 'cccc0000-0000-0000-0000-00000000000' + name[-1]
        ctx = self.ctx()
        home = ctx.agents_dir / child_id
        for sub in ('inbox', 'read', 'sent'):
            (home / sub).mkdir(parents=True, exist_ok=True)
        an.merge_info(home / 'info.md', {
            'id': child_id, 'workspace': ctx.slug, 'kind': 'claude',
            'registered_at': an.now(), 'last_active': an.now(),
            'status': an.STATUS_ACTIVE, 'pid': os.getpid(),
            'display_name': name, 'spawned_by': self.agent_id,
        })
        return child_id

    def test_my_children_are_found(self) -> None:
        self.register()
        child = self.spawn_child()
        self.assertIn(child, an.my_live_children(self.ctx()))

    def test_other_peoples_children_are_not_mine(self) -> None:
        """只盯自己拉起的——别人的 reviewer 死活与我无关，报了就是噪音。"""
        self.register()
        child = self.spawn_child()
        an.merge_info(self.ctx().info_path_of(child), {'spawned_by': 'someone-else'})
        self.assertEqual(an.my_live_children(self.ctx()), {})

    def test_dead_child_is_detected(self) -> None:
        self.register()
        child = self.spawn_child()
        watched = an.my_live_children(self.ctx())
        an.merge_info(self.ctx().info_path_of(child), {'status': an.STATUS_EXITED})
        self.assertIn(child, an.dead_among(self.ctx(), watched))

    def test_archived_child_is_detected(self) -> None:
        """被 sweep 归档时整个目录会搬走，登记文件就不在原处了。"""
        self.register()
        child = self.spawn_child()
        watched = an.my_live_children(self.ctx())
        an.archive_agent(self.ctx(), child, 'sweep', 'no heartbeat')
        self.assertIn(child, an.dead_among(self.ctx(), watched))

    def test_live_child_is_not_reported(self) -> None:
        """反向：活着的不能误报，否则 author 会被赶去重开一个正在干活的 reviewer。"""
        self.register()
        self.spawn_child()
        watched = an.my_live_children(self.ctx())
        self.assertEqual(an.dead_among(self.ctx(), watched), {})

    def test_notice_is_actionable(self) -> None:
        """要点名是谁、并给出下一步——但**先确认再动手**，见 TestDeadChildNoticeIsHedged。

        此处曾断言正文含"回信不会再来"。那句话是过度自信的措辞（把推断说成断言），
        已删；用例跟着改，否则它会把一个已知有害的契约钉死在这里。
        """
        text = an.render_dead_children({'cccc0000-1111': 'gate9-reviewer'})
        self.assertIn('cccc0000', text)
        self.assertIn('gate9-reviewer', text)
        self.assertIn('spawn', text)

    def test_watch_interval_is_between_loop_and_heartbeat(self) -> None:
        """比主循环稀（省 I/O）、比心跳密（author 在等，不能拖 5 分钟）。"""
        self.assertGreater(an.CHILD_WATCH_INTERVAL_S, 2)
        self.assertLess(an.CHILD_WATCH_INTERVAL_S, an.heartbeat_interval_s())


class TestLivenessFailsOpen(Base):
    """**进程证据只用来证明"活着"，永不用来证明"死了"。**

    2026-08-20 一天之内三次误判，我为"用进程证据判死"打了两次补丁，每次都还剩一张面孔：

    1. `pid` 已死 + 轮询器活着 + 心跳新鲜 → 判死（4 个实例中招，**投给活人的信被拒**）
    2. 心跳"已静默 0 分钟"仍被两个陈旧 pid 推翻 → 判死
    3. `7c5fad3b`：**从未挂过轮询器**，唯一的 `pid` 是 `os.getppid()` 兜底、已死，
       心跳静默 3 分钟（阈值 5 分钟）→ 判死。而它当时正把信读进 `read/`，明明活着。

    第三次才看清方向本身是错的：**没有任何进程信号能可靠证明"死"**——
    `pid` 死可能是兜底指错了人，`poller_pid` 死是 harness 在回合边界杀后台任务的常态。
    唯一的判死依据只有心跳超时，而那是**定义**。
    """

    def fresh(self, **over: object) -> dict:
        meta = {'status': an.STATUS_ACTIVE, 'last_active': an.now()}
        meta.update(over)
        return meta

    def test_live_poller_outweighs_dead_pid(self) -> None:
        """**这条是事故的直接回归**：轮询器活着就是活着，别被陈旧的 pid 拖下水。"""
        meta = self.fresh(poller_pid=os.getpid(), pid=999999)
        self.assertEqual(an.effective_status(meta, verify_pid=True), an.STATUS_ACTIVE)

    def test_live_pid_alone_is_enough(self) -> None:
        meta = self.fresh(pid=os.getpid(), poller_pid=999999)
        self.assertEqual(an.effective_status(meta, verify_pid=True), an.STATUS_ACTIVE)

    def test_both_dead_does_not_kill_within_threshold(self) -> None:
        """两个 pid 都死也**不判死**——心跳还在阈值内就还活着。

        这条契约与旧版相反。旧版拿"两个都死"当死亡证据，于是 `7c5fad3b`
        （没挂轮询器、`pid` 是陈旧兜底、静默 3 分钟）被判死、收不到信。
        """
        stale = an.now() - timedelta(seconds=an.dead_after_s() - 30)
        meta = self.fresh(pid=999999, poller_pid=999998, last_active=stale)
        self.assertEqual(an.effective_status(meta, verify_pid=True), an.STATUS_ACTIVE)

    def test_never_polled_agent_is_not_killed_by_stale_pid(self) -> None:
        """**事故三的直接回归**：从未挂过轮询器的实例，只有一个不可信的 `pid`。

        `7c5fad3b` 实测：`poller_pid` 字段从未写入过，`pid` 已死，静默 3 分钟——
        被判 presumed-dead 而拒收投递，可它当时正把上一封信读进 `read/`。
        """
        silent = an.now() - timedelta(seconds=180)
        meta = {'status': an.STATUS_ACTIVE, 'last_active': silent, 'pid': 999999}
        self.assertEqual(an.effective_status(meta, verify_pid=True), an.STATUS_ACTIVE)

    def test_no_pid_recorded_is_not_evidence_of_death(self) -> None:
        """没有活进程 = **没有证据**，不是死亡证据——判死交给心跳。"""
        self.assertFalse(an.process_evidence_of_life({'status': an.STATUS_ACTIVE}))
        fresh = self.fresh()
        self.assertEqual(an.effective_status(fresh, verify_pid=True), an.STATUS_ACTIVE)

    def test_live_process_rescues_an_expired_heartbeat(self) -> None:
        """**方向倒过来之后新增的能力**：心跳超时，但还有活进程 ⇒ 判它活着。

        典型场景：一个实例正忙一件十分钟的活，期间根本不调 agentnet，心跳早就陈旧，
        可它的轮询器还在跑。旧写法把这种实例判死（于是别人投信被拒），新写法留着它。
        """
        old = an.now() - timedelta(seconds=an.dead_after_s() + 60)
        meta = {'status': an.STATUS_ACTIVE, 'last_active': old, 'poller_pid': os.getpid()}
        self.assertEqual(an.effective_status(meta, verify_pid=True), an.STATUS_ACTIVE)

    def test_expired_heartbeat_with_no_live_process_is_dead(self) -> None:
        """判死仍然存在，只是**唯一**入口是心跳超时且拿不出活进程。"""
        old = an.now() - timedelta(seconds=an.dead_after_s() + 60)
        meta = {'status': an.STATUS_ACTIVE, 'last_active': old,
                'pid': 999999, 'poller_pid': 999998}
        self.assertEqual(an.effective_status(meta, verify_pid=True), an.STATUS_PRESUMED_DEAD)

    def test_fresh_heartbeat_beats_dead_pids(self) -> None:
        """**新鲜心跳是最强证据**：只有活着的进程写得动它。

        第一版漏了这条，于是仍有实例带着"已静默 0 分钟"被判死——它正在干活、
        每条命令都刷心跳，只是没挂轮询器且 pid 恰好陈旧。让两个不可靠的字段推翻
        一个刚刚发生的事实，方向就是反的。
        """
        meta = {'status': an.STATUS_ACTIVE, 'last_active': an.now(),
                'pid': 999999, 'poller_pid': 999998}
        self.assertEqual(an.effective_status(meta, verify_pid=True), an.STATUS_ACTIVE)

    def test_crashed_shell_is_caught_by_heartbeat_not_by_pid(self) -> None:
        """注册成功后崩掉的壳仍然会被抓到——**但靠心跳超时，不靠 pid**。

        代价明说：从"一分钟暴露"变成"五分钟暴露"。这个代价**可逆**（壳多留一会儿，
        顶多有人投一封没人读的信）；而误判死**不可逆**（让人丢上下文、白开实例、
        拒收投给活人的信）。代价不对称时，判据偏向可逆的那侧。
        """
        registered_at = an.now() - timedelta(seconds=an.dead_after_s() + 5)
        shell = {'status': an.STATUS_ACTIVE, 'last_active': registered_at,
                 'pid': 999999, 'poller_pid': 999998}
        self.assertEqual(an.effective_status(shell, verify_pid=True), an.STATUS_PRESUMED_DEAD)
        # 而在阈值之内，同一个壳被当作活的——这正是上面说的那份代价，写出来而不是含糊过去。
        young = dict(shell, last_active=an.now() - timedelta(seconds=90))
        self.assertEqual(an.effective_status(young, verify_pid=True), an.STATUS_ACTIVE)

    def test_process_evidence_never_makes_things_deader(self) -> None:
        """**不变式**：打开 `verify_pid` 只可能让判定更"活"，绝不可能更"死"。

        这条守着方向本身。任何未来的改动若让进程证据重新参与判死，这里立刻红。
        """
        cases = [
            {'pid': 999999, 'poller_pid': 999998},
            {'pid': os.getpid()},
            {'poller_pid': os.getpid()},
            {},
        ]
        offsets = [0, 90, an.dead_after_s() - 30, an.dead_after_s() + 60]
        for pids in cases:
            for offset in offsets:
                meta = {'status': an.STATUS_ACTIVE,
                        'last_active': an.now() - timedelta(seconds=offset), **pids}
                without = an.effective_status(meta, verify_pid=False)
                with_pid = an.effective_status(meta, verify_pid=True)
                if without == an.STATUS_ACTIVE:
                    self.assertEqual(with_pid, an.STATUS_ACTIVE,
                                     f"verify_pid 把活的判成了死的: {pids} offset={offset}")

    def test_deliverable_to_a_live_agent_with_stale_pid(self) -> None:
        """端到端：这正是被拒收的那条路径。"""
        self.register()
        ctx = self.ctx()
        an.merge_info(ctx.info_path, {'poller_pid': os.getpid(), 'pid': 999999,
                                      'last_active': an.now()})
        self.assertIsNone(an.undeliverable_reason(ctx, self.agent_id, force=False),
                          '给活人投信又被拒了')


class TestDeadChildNoticeIsHedged(Base):
    """措辞必须与置信度匹配：把推断说成断言，会诱导对方扔掉正在进行的工作。"""

    def text(self) -> str:
        return an.render_dead_children({'aaaa1111-2222': 'final21-reviewer'})

    def test_titled_as_an_inference(self) -> None:
        self.assertIn('可能已退出', self.text())
        self.assertNotIn('已经不在了', self.text())

    def test_tells_you_to_verify_first(self) -> None:
        text = self.text()
        self.assertIn('agentnet who', text)
        self.assertIn('别重开', text)

    def test_drops_the_bogus_justification(self) -> None:
        """初版写「协议本就要求每轮换新实例，所以重开不是退步」——

        那条规则说的是**终审门**换人，不是**对拍中途**换人。它让误报显得无害，实际不是。
        """
        self.assertNotIn('不是退步', self.text())


class TestNamelessInstance(Base):
    """人类直接启动的实例**长期无名**——文档承诺的默认值根本不存在，且静默。

    实测（`b8839ea3`）：它跑了几十轮、发了十几封信，直到用户问"这个实例是谁"才发现
    自己在花名册里是 `-`。有名字的清一色是 spawn 出来的——因为拉起方替它写了。
    **越是长期存活、承担主线工作的实例，越是没有名字**，而它们恰恰最该被找到。
    """

    def register_output(self, name: str | None = None) -> str:
        import argparse, io, contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            an.cmd_register(argparse.Namespace(topics=None, name=name))
        return buffer.getvalue()

    def test_nameless_registration_warns(self) -> None:
        """默认值取不到时必须**响亮**——不提示就等于把失败伪装成正常。"""
        text = self.register_output()
        self.assertIn('还没有显示名', text)
        self.assertIn('--name', text)

    def test_named_registration_is_quiet(self) -> None:
        """有名字就别唠叨——告警只在真缺的时候响才有意义。"""
        self.assertNotIn('还没有显示名', self.register_output('my-name'))

    def test_help_no_longer_promises_a_default(self) -> None:
        """帮助文本曾写"默认取 harness 提供的会话名"——**那个默认值不存在**。

        承诺一个不存在的默认值，比不承诺更糟：读者据此以为不用管。
        """
        import argparse
        parser = argparse.ArgumentParser()
        an._args_register(parser)
        help_text = parser.format_help()
        self.assertNotIn('默认取 harness', help_text)
        self.assertIn('没有默认值', help_text)


class TestPollerStartupRace(Base):
    """钩子说"未运行"、紧接着 whoami 说"运行中"——不是判据漂移，是启动竞态。

    报告者先诊断为"两份判活实现漂移"，随后**主动收回**。查证：两处判据逐字相同
    （`pid_alive(poller_pid)`、同一份 info.md）——它的第二种解释（启动竞态）才成立。
    实测 `poll` 从 spawn 到写入 `poller_pid` 约 0.16 秒。
    """

    def test_grace_covers_the_measured_window(self) -> None:
        """0.16 秒的实测窗口，1.5 秒给了 10 倍余量。"""
        self.assertGreaterEqual(an.POLLER_STARTUP_GRACE_S, 1.0)

    def test_recheck_sees_a_late_write(self) -> None:
        """复查必须**重新读盘**，不能沿用第一次读到的 meta。"""
        import argparse, io, contextlib, json, threading
        self.register()
        ctx = self.ctx()
        an.merge_info(ctx.info_path, {'poller_pid': None})

        def arm_late() -> None:
            time.sleep(0.3)                       # 在宽限之内、第一次读之后
            an.merge_info(ctx.info_path, {'poller_pid': os.getpid()})

        writer = threading.Thread(target=arm_late)
        writer.start()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            an.cmd_drain(argparse.Namespace(hook=True, no_block=False))
        writer.join()
        payload = json.loads(buffer.getvalue() or '{}')
        self.assertNotIn('decision', payload, '复查应看到迟到的写入，不该再拦截')


class TestUnarmedNoticeOrdering(Base):
    """正确做法必须排在错误做法**之前**——多数读者按顺序执行。

    报告者指出：通知开头是命令式「立刻运行 agentnet poll」，末尾才是「先复核」。
    而它还带三次强制拦截，压力方向全在"快去挂"。**读到开头就照做的实例会踩进去。**
    """

    def notice(self) -> str:
        import argparse, io, contextlib, json
        self.register()
        an.merge_info(self.ctx().info_path, {'poller_pid': 999999})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            an.cmd_drain(argparse.Namespace(hook=True, no_block=False))
        return json.loads(buffer.getvalue())['hookSpecificOutput']['additionalContext']

    def test_verify_comes_before_the_imperative(self) -> None:
        text = self.notice()
        verify_at = text.index('whoami')
        arm_at = text.index('run_in_background')
        self.assertLess(verify_at, arm_at, '「先复核」必须排在「去挂上」之前')

    def test_warns_against_double_arming(self) -> None:
        self.assertIn('别重挂', self.notice())


class TestProvenance(Base):
    """被拉起的实例必须知道自己是被谁起来的，否则它会推错整条授权链。

    实测事故（2026-08-14）：`4bf64d40`（spawned_by=0de75e6c、role=peer）把 argv 上注入的
    引导语当成人类输入，据此断定「本会话是人类起的」，进而拒绝了拉起方要它 exit 的请求。
    它推错的每一步都是因为上下文里从没提过 spawned_by。
    """

    def test_spawned_instance_is_told_who_spawned_it(self) -> None:
        lines = an.provenance_lines({'spawned_by': '0de75e6c-b6d7-45df-9b16-94580257a759'})
        text = '\n'.join(lines)
        self.assertIn('0de75e6c', text, '必须点名拉起方')
        self.assertIn('不是人类直接启动的', text)

    def test_spawned_instance_is_told_the_prompt_is_injected(self) -> None:
        """那句引导语和真实用户输入长得一模一样，不说破就一定会被当成人类在说话。"""
        text = '\n'.join(an.provenance_lines({'spawned_by': 'abc12345'}))
        self.assertIn(an.BOOTSTRAP_PROMPT, text)
        self.assertIn('不是人类输入', text)

    def test_spawned_instance_is_told_exit_requests_are_legitimate(self) -> None:
        """拉起方本来就能 kill/reset，所以「请你 exit」是同一件事的优雅形式。"""
        text = '\n'.join(an.provenance_lines({'spawned_by': 'abc12345'}))
        self.assertIn('kill', text)
        self.assertIn('exit', text)

    def test_human_started_instance_is_told_the_opposite(self) -> None:
        """反向误判同样有害：人类起的实例不该被同僚一封信劝退。"""
        text = '\n'.join(an.provenance_lines({}))
        self.assertIn('人类直接启动', text)
        self.assertNotIn(an.BOOTSTRAP_PROMPT, text)


class TestArchivedLifecycle(Base):

    def test_archived_copy_finds_timestamped_dir(self) -> None:
        """二次归档会落成 `<id>-<时间戳>`，只匹配裸 id 会误报"没归档过"。"""
        ctx = self.ctx()
        ctx.archive_dir.mkdir(parents=True, exist_ok=True)
        (ctx.archive_dir / f"{self.agent_id}-20260814T120000").mkdir()
        found = an.archived_copy(ctx, self.agent_id)
        self.assertIsNotNone(found)
        self.assertTrue(found.name.startswith(self.agent_id))

    def test_archived_copy_absent_when_never_archived(self) -> None:
        self.assertIsNone(an.archived_copy(self.ctx(), self.agent_id))

    def test_poll_after_exit_is_not_an_error(self) -> None:
        """`agentnet exit` 之后再挂 poll 是多余但无害的——不该渲染成 exit code 1。

        此前它走 _die 退 1，被 harness 报成 "Background command failed"，
        实例于是回头排查一个并不存在的故障。
        """
        import argparse
        self.register()
        an.cmd_exit(argparse.Namespace())
        an.cmd_poll(argparse.Namespace(interval=1, max_wait=1))   # 不抛 SystemExit 即通过

    def test_poll_without_registration_is_still_an_error(self) -> None:
        """从没注册过 ≠ 已退出。前者是真错误，不能一起放行。"""
        import argparse
        with self.assertRaises(SystemExit) as caught:
            an.cmd_poll(argparse.Namespace(interval=1, max_wait=1))
        self.assertNotEqual(caught.exception.code, 0)

    def test_exit_warns_against_rearming_poll(self) -> None:
        """exit 会杀掉轮询器，harness 报它退出；不说破实例就会条件反射地重挂。"""
        import argparse
        import io
        import contextlib
        self.register()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            an.cmd_exit(argparse.Namespace())
        self.assertIn('不要再重挂', buffer.getvalue())


class TestLockExitCodes(Base):
    """竞争失败必须与"agentnet 用不了"用不同的退出码区分开。

    脚本调用方（SCPM 迁移是第一个）据此决定该重试还是该降级。只靠"非零"会把环境
    问题当成竞争，白白轮询到超时却看不到真正的原因。
    """

    def test_contention_has_its_own_exit_code(self) -> None:
        self.assertNotEqual(an.EXIT_LOCK_HELD, 1, '必须与通用错误码区分')
        self.assertNotEqual(an.EXIT_LOCK_HELD, 0)

    def test_held_lock_exits_with_that_code(self) -> None:
        import argparse
        self.register()
        ctx = self.ctx()
        an.try_acquire_lock(ctx, 'scpm', 'someone-else', 999, 'held by other', 600)
        with self.assertRaises(SystemExit) as caught:
            an.cmd_lock(argparse.Namespace(
                action='acquire', name='scpm', purpose='mine', ttl=600, all=False,
                wait=False, max_wait=0, poll_interval=1))
        self.assertEqual(caught.exception.code, an.EXIT_LOCK_HELD)


class TestLockWaiting(Base):
    """`--wait` 是 SCPM 迁移的前置条件：旧脚本的默认行为就是"等到拿到为止"。"""

    def wait_args(self, **overrides: object) -> object:
        import argparse
        defaults = dict(action='acquire', name='scpm', purpose='mine', ttl=600,
                        all=False, wait=True, max_wait=2, poll_interval=1)
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_returns_immediately_when_free(self) -> None:
        self.register()
        ok, _ = an.acquire_waiting(self.ctx(), self.wait_args())
        self.assertTrue(ok)

    def test_gives_up_at_max_wait(self) -> None:
        """软超时必须真的生效——否则一个持续续租的持有者能让调用方永远不返回。"""
        self.register()
        ctx = self.ctx()
        an.try_acquire_lock(ctx, 'scpm', 'someone-else', 999, 'held by other', 600)
        started = time.monotonic()
        ok, held = an.acquire_waiting(ctx, self.wait_args())
        self.assertFalse(ok)
        self.assertEqual((held or {}).get('holder'), 'someone-else')
        self.assertLess(time.monotonic() - started, 15, '不该远超 max_wait')

    def test_takes_over_once_holder_releases(self) -> None:
        """等待的意义就在这里：对方释放后自动拿到，不需要调用方重试。"""
        self.register()
        ctx = self.ctx()
        an.try_acquire_lock(ctx, 'scpm', 'someone-else', 999, 'brief', 600)

        def hand_over() -> None:
            time.sleep(1)
            an.release_lock(ctx, 'scpm', 'someone-else')

        releaser = threading.Thread(target=hand_over)
        releaser.start()
        try:
            ok, _ = an.acquire_waiting(ctx, self.wait_args(max_wait=20))
        finally:
            releaser.join()
        self.assertTrue(ok, '对方释放后应当自动接手')

    def test_expired_lease_is_taken_over_without_waiting_out_max(self) -> None:
        """租约到期即可抢占——这正是"孤儿锁不再靠人眼判断"的机制。"""
        self.register()
        ctx = self.ctx()
        an.try_acquire_lock(ctx, 'scpm', 'dead-holder', 999, 'crashed', -1)
        ok, _ = an.acquire_waiting(ctx, self.wait_args())
        self.assertTrue(ok)


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

    def test_short_chinese_is_not_falsely_rejected(self) -> None:
        """**假阳性回归**：`占位` 的 GBK 字节碰巧构成合法 UTF-8，反解出 `ռλ`。

        旧判据只要求"反解出不同的合法文本"，于是拒掉了一个完全正常的参数——实测挡住过
        真实工作，还让我据此在项目规范里写下"agentnet 参数一律用 ASCII"这条基于假阳性的
        建议。短串尤其危险，因为碰巧构成合法 UTF-8 的概率随长度下降。
        """
        for value in ('占位', '一', '重构', '你好', '锁', '幂等', '评审', '好的'):
            self.assertEqual(an.guard_text(value, '参数 --purpose'), value,
                             f'{value!r} 被误判为乱码')

    def test_cjk_detector(self) -> None:
        """区分"真乱码"与"碰巧能反解的合法中文"全靠它。"""
        self.assertTrue(an.has_cjk('占位'))
        self.assertTrue(an.has_cjk('mixed 中 123'))
        self.assertFalse(an.has_cjk('ռλ'), '亚美尼亚/希腊字母不是 CJK')
        self.assertFalse(an.has_cjk('plain ascii'))
        self.assertFalse(an.has_cjk(''))

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

    def test_bootstrap_prompt_has_no_cmd_metacharacters(self) -> None:
        """它要穿过 cmd.exe（.cmd 类启动器），元字符会被当成命令分隔。"""
        for char in ';&|<>^%"':
            self.assertNotIn(char, an.BOOTSTRAP_PROMPT, f'{char!r} 会被 cmd.exe 解释')

    def test_bootstrap_prompt_names_the_real_goal(self) -> None:
        """**它是那个实例收到的唯一一条用户消息**，必须自己说清终点在哪。

        实测（2026-08-15）：只写 `Run: agentnet drain` 时，四个终审 reviewer 连续
        把"跑完 drain"当成交付——读了信、挂了 poll、报了状态，然后停下，评审一行没做。
        它们没做错，是指令只描述了准备动作。
        """
        prompt = an.BOOTSTRAP_PROMPT.lower()
        self.assertIn('drain', prompt)
        self.assertIn('task', prompt, '要说明 drain 交付的是任务，不是终点')
        self.assertIn('reply', prompt, '要说明做完必须回信，否则没有交付')

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


class TestRepeatedOptionIsRejected(Base):
    """重复给同一个具名选项 ⇒ **当场报错**，绝不静默取最后一个。

    2026-08-20 实测：`send --to a --to b --to c --to d` 只投给了 `d`，
    回执写着"已投递 1 封"——每个字都真，唯独不提另外三个去哪了。发信方以为送到了，
    另外三个收信方从不知道有人找过自己。**静默丢弃比报错危险得多。**
    """

    def parse(self, argv: list[str]) -> argparse.Namespace:
        return an.build_parser().parse_args(argv)

    def test_repeating_a_single_valued_option_errors(self) -> None:
        with self.assertRaises(SystemExit):
            self.parse(['register', '--name', '甲', '--name', '乙'])

    def test_single_value_still_works(self) -> None:
        args = self.parse(['register', '--name', '甲'])
        self.assertEqual(args.name, '甲')

    def test_guard_covers_every_subcommand(self) -> None:
        """守卫装在**建子解析器的那个循环**里，所以每条命令都受保护。

        逐个 subcommand 找一个具名选项试重复——将来新增命令也自动被覆盖，
        不依赖谁记得给它加。
        """
        checked = 0
        for cmd in an.COMMANDS:
            parser = an.build_parser()
            target = self._first_repeatable_flag(cmd)
            if target is None:
                continue
            flag, value = target
            with self.assertRaises(SystemExit, msg=f"{cmd.name} {flag} 重复未被拒绝"):
                parser.parse_args([cmd.name, flag, value, flag, value])
            checked += 1
        self.assertGreater(checked, 5, '样本太少，等于没测')

    def _first_repeatable_flag(self, cmd: object) -> tuple[str, str] | None:
        """挑一个"接受值、且不是 append"的选项。没有则跳过该命令。"""
        probe = argparse.ArgumentParser()
        for key in ('store', None):
            probe.register('action', key, an.StoreOnce)
        add_args = getattr(cmd, 'add_args', None)
        if add_args is None:
            return None
        add_args(probe)
        for action in probe._actions:                    # noqa: SLF001
            if not action.option_strings or action.nargs == 0:
                continue
            if not isinstance(action, an.StoreOnce):
                continue                                  # append / store_true 不适用
            if action.choices:
                return action.option_strings[0], str(list(action.choices)[0])
            if action.type is int:
                return action.option_strings[0], '1'
            return action.option_strings[0], 'x'
        return None


class TestMultiRecipientSend(Base):
    """`--to` 是**显式**可重复的那一个——多收件人是真需求，所以给它 append。"""

    def test_repeated_to_reaches_everyone(self) -> None:
        args = an.build_parser().parse_args(
            ['send', '--to', 'aaa', '--to', 'bbb', '--to', '@topic',
             '--subject', 's', '--body', 'b'])
        self.assertEqual(args.to, ['aaa', 'bbb', '@topic'])

    def test_single_to_is_still_a_list(self) -> None:
        args = an.build_parser().parse_args(
            ['send', '--to', 'aaa', '--subject', 's', '--body', 'b'])
        self.assertEqual(args.to, ['aaa'])

    def test_list_valued_args_are_still_text_guarded(self) -> None:
        """append 选项拿到的是 `list[str]`——**别让它从入口体检里溜过去**。

        把 `--to` 改成可重复时差点漏掉这里：体检只认 `isinstance(value, str)`，
        改成 list 之后收件人 token 就绕过了乱码守卫，损坏的 id 会直接写进信件。
        """
        broken = '娴嬭瘯'                       # UTF-8 被按 GBK 解出来的典型残骸
        with self.assertRaises(SystemExit):
            an.guard_text(broken, '参数 --to')
        args = an.build_parser().parse_args(
            ['send', '--to', 'aaa', '--to', broken, '--subject', 's', '--body', 'b'])
        with self.assertRaises(SystemExit, msg='list 里的坏 token 必须被体检拦下'):
            for name, value in vars(args).items():
                if name.startswith('_'):
                    continue
                for item in (value if isinstance(value, list) else [value]):
                    if isinstance(item, str):
                        an.guard_text(item, f"参数 --{name.replace('_', '-')}")


class TestCommandRegistryWiring(Base):
    """每条命令注册的处理函数，必须**真的是那条命令的处理函数**。

    2026-08-20 实测：往 `@command(...)` 与 `def cmd_restore` **之间**插了两个模块级
    helper，装饰器于是把 `archived_agent_ids` 注册成了 restore 的处理函数——
    `agentnet restore` 一跑就 `TypeError`。171 个测试全绿，因为它们一律直接调
    `an.cmd_xxx(...)`，**从不经过注册表**：装饰器接错了谁，没有任何用例看得见。
    """

    def test_every_handler_is_a_cmd_function(self) -> None:
        for cmd in an.COMMANDS:
            self.assertEqual(
                cmd.handler.__name__, f"cmd_{cmd.name}",
                f"命令 `{cmd.name}` 注册的处理函数是 `{cmd.handler.__name__}`——"
                f"多半是有东西插进了 @command(...) 与 def 之间")

    def test_every_handler_takes_exactly_args(self) -> None:
        """处理函数的签名必须是 `(args)`——注册表就是这么调它的。"""
        import inspect
        for cmd in an.COMMANDS:
            params = list(inspect.signature(cmd.handler).parameters)
            self.assertEqual(params, ['args'], f"`{cmd.name}` 的签名是 {params}")


class TestPartialDeliveryIsNotSilent(Base):
    """群发遇到一个不可投的收件人，**不能掐断整批**，也不能读起来像全部成功。

    2026-08-20 实测：7 个收件人里第 6 个已死，`check_deliverable` 的 `_die`
    掐断了循环——**第 7 个完全可投，却从未被尝试**；而回执只列了成功的 5 个，
    对后两个只字未提。发信方以为送到了，那两位从不知道有人找过自己。

    这是「多收件人只投最后一个」修好之后**新引入**的同族缺陷：
    修掉一个静默丢弃，换来另一个静默丢弃。
    """

    def peer(self, suffix: str, alive: bool) -> str:
        """造一个同 workspace 的收件人。``alive=False`` 的心跳早已超时且无活进程。"""
        agent_id = f"bbbbbbbb-0000-0000-0000-00000000{suffix}"
        d = self.ctx().agents_dir / agent_id
        (d / 'inbox').mkdir(parents=True, exist_ok=True)
        last = an.now() if alive else an.now() - timedelta(seconds=an.dead_after_s() + 120)
        meta: dict = {'id': agent_id, 'status': an.STATUS_ACTIVE, 'last_active': last}
        if not alive:
            meta.update({'pid': 999999, 'poller_pid': 999998})
        an.write_doc(d / 'info.md', meta, '', an.INFO_FIELD_ORDER)
        return agent_id

    def test_dead_recipient_does_not_block_the_ones_after_it(self) -> None:
        self.register()
        dead = self.peer('0001', alive=False)
        alive = self.peer('0002', alive=True)
        with self.assertRaises(SystemExit) as caught:       # 有人没投出去 ⇒ 非零退出
            an.cmd_send(argparse.Namespace(
                to=[dead, alive], subject='s', body='b', body_file=None,
                kind='letter', thread=None, force=False))
        self.assertEqual(caught.exception.code, 1)
        # **关键断言**：排在死人后面的那个必须真的收到了
        self.assertEqual(len(an.inbox_letters(self.ctx(), alive)), 1,
                         '死掉的收件人掐断了整批，后面的人一封都没收到')
        self.assertEqual(len(an.inbox_letters(self.ctx(), dead)), 0)

    def test_unresolvable_recipient_does_not_block_the_ones_after_it(self) -> None:
        """**第二个中止点**：解析失败（归档 / 找不到 / 前缀不唯一）同样不能掐断整批。

        第一次只修了"不可投递"那一处，于是一个**已归档**的收件人照旧让排在它后面的
        全部没投出去——同一个模式的兄弟实例，漏修一个等于没修
        （见 feedback_enumerate_root_cause_instances_before_fixing）。
        """
        self.register()
        alive = self.peer('0007', alive=True)
        with self.assertRaises(SystemExit) as caught:
            an.cmd_send(argparse.Namespace(
                to=['no-such-agent-xyz', alive], subject='s', body='b', body_file=None,
                kind='letter', thread=None, force=False))
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(len(an.inbox_letters(self.ctx(), alive)), 1,
                         '解析不出来的 token 掐断了整批')

    def test_single_target_commands_still_die_on_bad_token(self) -> None:
        """反向守卫：kill / reset / archive 只作用于一个 agent，**必须**当场失败，
        不能因为群发放宽了就跟着含糊——否则会作用到错的对象上。"""
        self.register()
        with self.assertRaises(SystemExit):
            an.resolve_single_target(self.ctx(), 'no-such-agent-xyz')

    def test_all_alive_exits_zero(self) -> None:
        """全都投出去时不能反过来误报失败。"""
        self.register()
        peers = [self.peer('0003', alive=True), self.peer('0004', alive=True)]
        an.cmd_send(argparse.Namespace(
            to=peers, subject='s', body='b', body_file=None,
            kind='letter', thread=None, force=False))          # 不抛即通过
        for p in peers:
            self.assertEqual(len(an.inbox_letters(self.ctx(), p)), 1)

    def test_force_delivers_to_everyone(self) -> None:
        self.register()
        dead = self.peer('0005', alive=False)
        alive = self.peer('0006', alive=True)
        an.cmd_send(argparse.Namespace(
            to=[dead, alive], subject='s', body='b', body_file=None,
            kind='letter', thread=None, force=True))
        for p in (dead, alive):
            self.assertEqual(len(an.inbox_letters(self.ctx(), p)), 1)


class TestRestoreAfterRepeatedArchiving(Base):
    """被归档过多次的 agent **必须还能恢复**，且旧副本里的信不能被埋掉。

    2026-08-20 实测：`cmd_restore` 自己重写了一遍前缀匹配（正确的解析早就在
    `archived_copy` 里），于是二次归档产生的 `<id>-<时间戳>` 副本让它永远
    "前缀不唯一"——**连完整 uuid 都救不了**。全网 6 个 agent 中招，
    `17948ac6` 有 15 份副本。它们重新注册只会拿到一个新的空目录，
    旧副本里的未读信件**永久掩埋**。
    """

    def archive_copy(self, agent_id: str, stamp: str, letters: int = 0) -> Path:
        d = self.ctx().archive_dir / f"{agent_id}-{stamp}"
        (d / 'inbox').mkdir(parents=True)
        for i in range(letters):
            (d / 'inbox' / f"2026{stamp}-sender-{i:04d}.md").write_text(
                '+++\nfrom = "sender"\n+++\n正文', encoding='utf-8')
        return d

    def test_full_uuid_resolves_despite_many_copies(self) -> None:
        self.register()
        agent_id = self.agent_id
        an.archive_agent(self.ctx(), agent_id, 'test', 'first')
        for stamp in ('20260818T134552', '20260819T120143'):
            self.archive_copy(agent_id, stamp)
        an.cmd_restore(argparse.Namespace(target=agent_id))
        self.assertTrue((self.ctx().agents_dir / agent_id / 'info.md').exists())

    def test_prefix_still_resolves(self) -> None:
        self.register()
        an.archive_agent(self.ctx(), self.agent_id, 'test', 'first')
        self.archive_copy(self.agent_id, '20260818T134552')
        an.cmd_restore(argparse.Namespace(target=self.agent_id[:8]))
        self.assertTrue((self.ctx().agents_dir / self.agent_id / 'info.md').exists())

    def test_letters_in_older_copies_are_recovered(self) -> None:
        """**这条是"永久掩埋"的直接回归**：旧副本里的信必须跟着回来。"""
        self.register()
        an.archive_agent(self.ctx(), self.agent_id, 'test', 'first')
        self.archive_copy(self.agent_id, '20260818T134552', letters=2)
        self.archive_copy(self.agent_id, '20260819T120143', letters=3)
        an.cmd_restore(argparse.Namespace(target=self.agent_id))
        self.assertEqual(len(an.inbox_letters(self.ctx(), self.agent_id)), 5)

    def snapshot(self, dir_name: str, display_name: str, days_ago: int) -> None:
        """在 archive/ 下摆一份指定归档时刻的副本。"""
        d = self.ctx().archive_dir / dir_name
        (d / 'inbox').mkdir(parents=True, exist_ok=True)
        an.write_doc(d / 'info.md',
                     {'id': self.agent_id, 'display_name': display_name,
                      'status': an.STATUS_ARCHIVED,
                      'archived_at': an.now() - timedelta(days=days_ago)},
                     '', an.INFO_FIELD_ORDER)

    def test_restores_the_newest_snapshot_regardless_of_directory_name(self) -> None:
        """恢复的必须是 **`archived_at` 最新**的那份，与目录叫什么无关。

        实测：裸名优先让 restore 复活了两天前的旧快照、`display_name` 丢失。
        而裸名**两种情况都可能**——它通常是第一次归档（最老），但被 restore 移走后
        名字空出来，下次归档又会占用它。所以任何基于名字的启发式都是错的，
        这里把**两种排列都测**，让名字排序无处藏身。
        """
        stamp = '20260818T134552'
        for bare_days, stamped_days, winner in ((2, 0, '带时间戳的'), (0, 2, '裸名的')):
            with self.subTest(bare_days=bare_days):
                self.setUp_fresh_archive()
                self.snapshot(self.agent_id, '裸名的', bare_days)
                self.snapshot(f"{self.agent_id}-{stamp}", '带时间戳的', stamped_days)
                an.cmd_restore(argparse.Namespace(target=self.agent_id))
                meta, _ = an.read_info(self.ctx().info_path_of(self.agent_id))
                self.assertEqual(meta.get('display_name'), winner,
                                 '恢复的不是 archived_at 最新的那份')

    def setUp_fresh_archive(self) -> None:
        """清掉上一轮 subTest 留下的目录，让两种排列互不干扰。"""
        for path in (self.ctx().agents_dir, self.ctx().archive_dir):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        self.ctx().archive_dir.mkdir(parents=True, exist_ok=True)

    def test_two_different_agents_still_ambiguous(self) -> None:
        """归并到 agent 之后仍然可能真的有歧义——那时才该拒绝。"""
        self.register()
        an.archive_agent(self.ctx(), self.agent_id, 'test', 'first')
        other = self.ctx().archive_dir / (self.agent_id[:8] + '-ffff-0000-0000-000000000000')
        (other / 'inbox').mkdir(parents=True)
        with self.assertRaises(SystemExit):
            an.cmd_restore(argparse.Namespace(target=self.agent_id[:8]))


class TestSweepSparesLiveProcesses(Base):
    """静默超时但**进程还活着** ⇒ 不归档。归档比判死更狠，同一条不变式更该适用。"""

    def stale_agent(self, **over: object) -> None:
        self.register()
        an.merge_info(self.ctx().info_path,
                      {'last_active': an.now() - timedelta(seconds=an.archive_after_s() + 60),
                       **over})

    def test_live_poller_is_spared(self) -> None:
        self.stale_agent(poller_pid=os.getpid())
        an.cmd_sweep(argparse.Namespace(dry_run=False, quiet=True))
        self.assertTrue(self.ctx().info_path.exists(), '有活进程却被归档了')

    def test_truly_gone_is_still_archived(self) -> None:
        """放过活的，不等于放过死的——归档本身必须还有效。"""
        self.stale_agent(poller_pid=999998, pid=999999)
        an.cmd_sweep(argparse.Namespace(dry_run=False, quiet=True))
        self.assertFalse(self.ctx().info_path.exists(), '真的没进程了却没归档')


if __name__ == '__main__':
    unittest.main(verbosity=2)
