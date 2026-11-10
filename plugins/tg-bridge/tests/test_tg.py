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


if __name__ == "__main__":
    unittest.main()
