"""The reports_via switch: "files" keeps the slot files, "postman" publishes to and reads from a postman spool.

Run with the venv's python:  ~/venvs/LeaseResources/bin/python -B tests/test_reports_via.py
Everything goes into a temporary folder: a sandbox config, data and local folder, and a postman spool of its own whose
whoami.json the test writes as Postman.__init__ does, so neither the synced tree nor this machine's real spool is touched.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE.parent / "Postman"))
import postman  # noqa: E402

ME, PEER = "Pektolith", "Mistral"


class ReportsVia(unittest.TestCase):

    def load(self, via):
        """A fresh res module on a sandbox whose defaults.json says reports_via = via (None: the key is left out)."""
        self.tmp = Path(tempfile.mkdtemp(prefix="reports-via-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        config = json.loads((SERVICE / "config" / "defaults.json").read_text(encoding="utf-8"))
        stub = self.tmp / "mail.py"
        stub.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
        config.update({"mail_script": str(stub), "notify": False})
        if via is None:
            del config["reports_via"]
        else:
            config["reports_via"] = via
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "defaults.json").write_text(json.dumps(config), encoding="utf-8")
        for key, sub in [("LEASERES_DATA", "data"), ("LEASERES_CONFIG", "config"), ("LEASERES_LOCAL", "local")]:
            os.environ[key] = str(self.tmp / sub)
        os.environ["LEASERES_MACHINE"] = ME
        spec = importlib.util.spec_from_file_location("res_under_test", SERVICE / "res.py")
        res = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(res)
        return res

    def spool(self, res, me=ME):
        spool = postman.Spool(str(self.tmp / "spool"), postman.load_config())
        postman.write_json(spool.path("whoami.json"), {"machine": me, "protocol": postman.PROTOCOL, "started": postman.stamp()})
        res.spool = lambda: spool
        return spool

    def slots(self, kind):
        return sorted((self.tmp / "data" / "leases" / ME / kind).glob("*.json"))

    def test_key_must_be_there_and_valid(self):
        res = self.load(None)
        with self.assertRaises(KeyError):
            res.load_config()
        res = self.load("tailscale")
        with self.assertRaises(AssertionError):
            res.load_config()

    def test_files_branch_never_touches_a_spool(self):
        res = self.load("files")
        config = res.load_config()
        self.assertEqual(res.REPORTS_VIA, "files")
        res.spool = lambda: self.fail("the files branch asked for a spool")
        d = res.Dispatcher(config)
        d.report()
        self.assertEqual(len(self.slots("reports")), 1)
        self.assertEqual(len(self.slots("beats")), 1)
        published = res.read_published(ME)
        self.assertEqual(published["machine"], ME)
        self.assertEqual(published, dict(json.loads(self.slots("reports")[0].read_text(encoding="utf-8")),
                                         **json.loads(self.slots("beats")[0].read_text(encoding="utf-8"))))

    def test_postman_branch(self):
        res = self.load("postman")
        config = res.load_config()
        spool = self.spool(res)
        d = res.Dispatcher(config)
        d.report()
        self.assertEqual(self.slots("reports") + self.slots("beats"), [], "no slot file with the postman")
        state = {s["service"]: s for s in spool.states()}
        self.assertEqual(sorted(state), ["LeaseResources", "LeaseResourcesBeats"])
        self.assertEqual({s["origin"] for s in state.values()}, {ME})
        self.assertEqual(res.read_report(ME, "reports"), state["LeaseResources"]["body"])
        self.assertEqual(res.read_published(ME), dict(state["LeaseResources"]["body"], **state["LeaseResourcesBeats"]["body"]))
        self.assertTrue(res.dispatcher_state(ME, config)[1]["compliance"][0].startswith("Compliance on " + ME))

        d.report()                                        # nothing changed, nothing published
        self.assertEqual([s["seq"] for s in spool.states()], [1, 1])
        res.write_json(res.lease_path(ME, "L1"), {"id": "L1", "state": "queued", "requested": res.now_iso(), "need": {"cpu": 0.5}})
        d.report()                                        # the report changed; the beat is not due
        self.assertEqual({s["service"]: s["seq"] for s in spool.states()}, {"LeaseResources": 2, "LeaseResourcesBeats": 1})
        self.assertEqual(res.read_report(ME, "reports")["queue"], ["L1"])
        d.beat_at -= res.REPORT_SECONDS + 1
        d.report()
        self.assertEqual({s["service"]: s["seq"] for s in spool.states()}, {"LeaseResources": 2, "LeaseResourcesBeats": 2})

        # A peer's report and beat as its postman carries them in (Postman.learn writes state/<origin>/<service>.json).
        self.assertEqual(res.read_published(PEER), {}, "a machine never heard from")
        self.assertFalse(res.dispatcher_state(PEER, config)[0])
        os.makedirs(spool.path("state", PEER))
        lease = {"id": "L261110-1-" + ME, "state": "granted", "answer": "granted"}

        def carry(service, seq, body):
            postman.write_json(spool.path("state", PEER, service + ".json"),
                               {"origin": PEER, "service": service, "seq": seq, "written": postman.stamp(), "body": body})

        carry("LeaseResources", 7, {"machine": PEER, "updated": res.now_iso(), "queue": [], "leases": {lease["id"]: lease}})
        stale = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - res.FRESH_SECONDS - 60))
        carry("LeaseResourcesBeats", 3, {"updated": stale, "pid": 1, "compliance": ["old"]})
        self.assertEqual(res.report_lease(PEER, lease["id"]), lease, "an answer to a remote request comes from the postman")
        self.assertFalse(res.dispatcher_state(PEER, config)[0], "freshness from the beat's own updated")
        carry("LeaseResourcesBeats", 4, {"updated": res.now_iso(), "pid": 1, "compliance": ["from the beat"]})
        fresh, report = res.dispatcher_state(PEER, config)
        self.assertTrue(fresh)
        self.assertEqual(report["compliance"], ["from the beat"])
        self.assertEqual(res.read_report(ME, "reports")["machine"], ME, "each machine's own report")

    def test_postman_branch_refuses_another_name(self):
        res = self.load("postman")
        res.load_config()
        self.spool(res, me=PEER)
        with self.assertRaises(AssertionError):
            res.write_report(ME, "beats", {"updated": res.now_iso()})


if __name__ == "__main__":
    unittest.main(verbosity=2)
