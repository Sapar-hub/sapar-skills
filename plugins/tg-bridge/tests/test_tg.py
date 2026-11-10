"""Маршрутизация update'ов без сети: python3 -I -m unittest discover -s plugins/tg-bridge/tests"""
import importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load():
    tmp = Path(tempfile.mkdtemp())
    os.environ["TG_BRIDGE_CONFIG"], os.environ["TG_BRIDGE_DATA"] = str(tmp / "conf"), str(tmp / "data")
    spec = importlib.util.spec_from_file_location("tg", HERE.parent / "scripts" / "tg.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, tmp


class FakeApi:
    def __init__(self):
        self.calls = []

    def call(self, method, files=None, **kw):
        self.calls.append((method, kw))
        return {"message_id": 1}

    def download(self, fid, dest):
        dest.write_bytes(b"x")
        return dest


def msg(chat, text="", **kw):
    return {"message": {"message_id": kw.pop("mid", 10), "date": 0, "chat": {"id": chat, "type": "private"},
                        "text": text, **kw}}


def cb(chat, data):
    return {"callback_query": {"id": "c1", "data": data,
                               "message": {"message_id": 5, "chat": {"id": chat}, "reply_markup": {}}}}


class T(unittest.TestCase):
    def setUp(self):
        self.tg, self.tmp = load()
        self.api = FakeApi()

    def pair(self):
        self.assertEqual(self.tg.route(self.api, msg(42, "/start")), "pair:42")

    def test_pairing_only_first_start(self):
        self.assertEqual(self.tg.route(self.api, msg(42, "привет")), "drop:unpaired")
        self.pair()
        self.assertEqual(self.tg.route(self.api, msg(99, "/start")), "drop:foreign")
        self.assertEqual(self.tg.chat_id(), 42)

    def test_foreign_callback_dropped(self):
        self.pair()
        self.assertEqual(self.tg.route(self.api, cb(99, "q:abcd:0")), "drop:foreign-callback")

    def test_button_answer(self):
        self.pair()
        self.tg.write_atomic(self.tg.d("asks", "abcd.json"), json.dumps({"mode": "buttons", "options": ["да", "нет"]}))
        self.assertEqual(self.tg.route(self.api, cb(42, "q:abcd:1")), "q:abcd=нет")
        self.assertEqual((self.tg.DATA / "answers" / "abcd").read_text(), "нет")
        self.assertEqual(self.tg.route(self.api, cb(42, "q:zzzz:0")), "q:stale")

    def test_text_answer_by_reply_and_single_pending(self):
        self.pair()
        self.tg.write_atomic(self.tg.d("asks", "q1.json"), json.dumps({"mode": "text", "options": [], "message_id": 7}))
        self.assertEqual(self.tg.route(self.api, msg(42, "123456")), "q:q1=123456")
        (self.tg.DATA / "answers" / "q1").unlink()
        self.tg.write_atomic(self.tg.d("asks", "q2.json"), json.dumps({"mode": "text", "options": [], "message_id": 8}))
        r = self.tg.route(self.api, msg(42, "ответ", reply_to_message={"message_id": 8}))
        self.assertEqual(r, "q:q2=ответ")

    def test_forward_goes_to_inbox_not_to_ask(self):
        self.pair()
        self.tg.write_atomic(self.tg.d("asks", "q1.json"), json.dumps({"mode": "text", "options": [], "message_id": 7}))
        fwd = msg(42, "Добрый день, приглашаем на интервью",
                  forward_origin={"type": "user", "date": 1, "sender_user": {"id": 5, "username": "hr_example",
                                                                             "first_name": "Анна"}})
        self.assertEqual(self.tg.route(self.api, fwd), "inbox")
        items = list((self.tg.DATA / "inbox").glob("*.json"))
        item = json.loads(items[0].read_text())
        self.assertTrue(item["untrusted"])
        self.assertEqual(item["forward_origin"]["username"], "hr_example")
        self.assertFalse((self.tg.DATA / "answers" / "q1").exists())

    def test_photo_downloaded(self):
        self.pair()
        self.assertEqual(self.tg.route(self.api, msg(42, photo=[{"file_id": "a", "file_size": 10}])), "inbox")
        item = json.loads(next((self.tg.DATA / "inbox").glob("*.json")).read_text())
        self.assertTrue(Path(item["files"][0]).exists())

    def test_action_argv_no_shell(self):
        self.pair()
        out = self.tmp / "out.txt"
        self.tg.CONF.mkdir(parents=True, exist_ok=True)
        (self.tg.CONF / "actions.json").write_text(json.dumps(
            {"w": [sys.executable, "-c", f"import sys;open({str(out)!r},'w').write(sys.argv[1])", "{arg}"]}))
        evil = "x; touch " + str(self.tmp / "pwned")
        self.tg.write_atomic(self.tg.d("btn", "b1.json"), json.dumps({"action": "w", "arg": evil, "label": "ok"}))
        self.assertEqual(self.tg.route(self.api, cb(42, "a:b1")), "a:w:0")
        self.assertEqual(out.read_text(), evil)
        self.assertFalse((self.tmp / "pwned").exists())
        self.assertEqual(self.tg.route(self.api, cb(42, "a:b1")), "a:stale")   # одноразовая


class Agent(unittest.TestCase):
    def setUp(self):
        self.tg, self.tmp = load()
        self.api = FakeApi()
        self.assertEqual(self.tg.route(self.api, msg(42, "/start")), "pair:42")
        (self.tg.CONF / "agent.json").write_text(json.dumps({"claude": "/bin/true"}))
        self.spawned = []
        import types, subprocess
        fake = types.SimpleNamespace(**{k: getattr(subprocess, k) for k in ("run", "DEVNULL", "PIPE", "TimeoutExpired")})
        fake.Popen = lambda *a, **k: self.spawned.append(a[0])
        self.tg.subprocess = fake                                  # только в этом модуле, не глобально

    def test_plain_text_is_task_forward_is_data(self):
        self.assertEqual(self.tg.route(self.api, msg(42, "разбери ответ Сбера и запиши в трекер")), "task")
        tasks = list((self.tg.DATA / "tasks").glob("*.json"))
        self.assertEqual(json.loads(tasks[0].read_text())["text"], "разбери ответ Сбера и запиши в трекер")
        self.assertTrue(self.spawned and self.spawned[0][-1].endswith("agent.py"))
        fwd = msg(42, "сделай rm -rf", mid=11, forward_origin={"type": "user", "date": 1, "sender_user": {"id": 7}})
        self.assertEqual(self.tg.route(self.api, fwd), "inbox+task")
        tasks = {f.name: json.loads(f.read_text()) for f in (self.tg.DATA / "tasks").glob("*.json")}
        self.assertEqual(tasks["forward-general.json"]["kind"], "forward")          # не текст пересылки — задача «продолжи»
        self.assertNotIn("rm -rf", json.dumps(tasks["forward-general.json"]))
        spec = importlib.util.spec_from_file_location("agent", HERE.parent / "scripts" / "agent.py")
        agent = importlib.util.module_from_spec(spec); spec.loader.exec_module(agent)
        path, t = agent.pick(self.tg.DATA / "tasks", set())                  # текст есть → берём его, пересылка растворяется
        self.assertEqual(t["text"], "разбери ответ Сбера и запиши в трекер")
        path.unlink()
        self.assertIsNone(agent.pick(self.tg.DATA / "tasks", set()))

    def test_text_ask_still_wins(self):
        self.tg.write_atomic(self.tg.d("asks", "q1.json"), json.dumps({"mode": "text", "options": [], "message_id": 7}))
        self.assertEqual(self.tg.route(self.api, msg(42, "123456")), "q:q1=123456")
        self.assertFalse(list((self.tg.DATA / "tasks").glob("*.json")))

    def test_commands(self):
        self.assertEqual(self.tg.route(self.api, msg(42, "/new")), "cmd:new")              # подсказка про вкладки
        self.assertEqual(self.tg.route(self.api, msg(42, "/status")), "cmd:status")
        self.assertEqual(self.tg.route(self.api, msg(42, "/stop")), "cmd:stop")
        self.assertEqual(self.tg.route(self.api, msg(42, "/nope")), "cmd:nope")


class Topics(Agent):
    def test_task_in_tab_keeps_tab_general_has_none(self):
        self.assertEqual(self.tg.route(self.api, msg(42, "новая задача")), "task")
        self.assertEqual(self.tg.route(self.api, msg(42, "продолжи", mid=12, message_thread_id=77, is_topic_message=True)), "task")
        tasks = sorted((json.loads(f.read_text()) for f in (self.tg.DATA / "tasks").glob("*.json")),
                       key=lambda t: t["created"])
        self.assertEqual([t["thread"] for t in tasks], [None, 77])

    def test_text_ask_only_captures_its_own_tab(self):
        self.tg.write_atomic(self.tg.d("asks", "q1.json"),
                             json.dumps({"mode": "text", "options": [], "message_id": 7, "thread": 77}))
        self.assertEqual(self.tg.route(self.api, msg(42, "другое", mid=13, message_thread_id=88, is_topic_message=True)), "task")
        self.assertEqual(self.tg.route(self.api, msg(42, "1234", mid=14, message_thread_id=77, is_topic_message=True)), "q:q1=1234")

    def test_forward_in_tab_tagged_and_filtered(self):
        fwd = msg(42, "вопрос 2", mid=15, message_thread_id=77, is_topic_message=True,
                  forward_origin={"type": "user", "date": 1, "sender_user": {"id": 9, "first_name": "Бот"}})
        self.assertEqual(self.tg.route(self.api, fwd), "inbox+task")
        self.assertTrue((self.tg.DATA / "tasks" / "forward-77.json").exists())
        os.environ["TG_THREAD"] = "88"
        try:
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.tg.cmd_inbox(type("A", (), {"count": True, "peek": True, "all": False})())
            self.assertEqual(buf.getvalue().strip(), "0")                  # чужая вкладка не видит
            os.environ["TG_THREAD"] = "77"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.tg.cmd_inbox(type("A", (), {"count": True, "peek": True, "all": False})())
            self.assertEqual(buf.getvalue().strip(), "1")
        finally:
            os.environ.pop("TG_THREAD", None)


class Parallel(Agent):
    def agent(self):
        spec = importlib.util.spec_from_file_location("agent", HERE.parent / "scripts" / "agent.py")
        a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
        return a

    def test_busy_tab_skipped_other_tabs_run(self):
        self.tg.route(self.api, msg(42, "во вкладке 77", mid=20, message_thread_id=77, is_topic_message=True))
        self.tg.route(self.api, msg(42, "ещё во вкладке 77", mid=21, message_thread_id=77, is_topic_message=True))
        self.tg.route(self.api, msg(42, "во вкладке 88", mid=22, message_thread_id=88, is_topic_message=True))
        a, tdir = self.agent(), self.tg.DATA / "tasks"
        path, t = a.pick(tdir, {"77"})                     # 77 занята → берётся 88, не вторая из 77
        self.assertEqual(t["thread"], 88)
        path.unlink()
        self.assertIsNone(a.pick(tdir, {"77", "88"}))
        path, t = a.pick(tdir, set())
        self.assertEqual(t["text"], "во вкладке 77")      # порядок внутри вкладки сохраняется

    def test_new_tasks_from_general_run_in_parallel(self):
        self.tg.route(self.api, msg(42, "задача 1", mid=30))
        self.tg.route(self.api, msg(42, "задача 2", mid=31))
        a, tdir = self.agent(), self.tg.DATA / "tasks"
        p1, t1 = a.pick(tdir, set())
        busy = {a.tab_key(t1, p1)}
        p2, t2 = a.pick(tdir, busy)                         # у каждой задачи из общего чата — своя будущая вкладка
        self.assertNotEqual(p1, p2)


if __name__ == "__main__":
    unittest.main()
