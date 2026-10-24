"""Scoring logic of the decision regression check on small synthetic points (python3 tools/dbench/test_regress.py)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regress as R  # noqa: E402

# session 1: improved +5% (a=1) at r3, regressed -4% (b=2) at r5, unreliable (c=3) at r6, +1.5% (d=4) at r8 below the 2%
# noise, +6% (e=5 riding along with a revert of b) at r9; session 2 (comparable): neutral +0.5% (f=6) at r2
OUT = {"version": 1, "groups": [[1, 2]], "sessions": {
    "1": {"noise": 2.0, "done_seqs": list(range(1, 11)), "runs": [
        {"seq": 3, "label": "a", "verdict": "improved", "delta": 5.0, "noise": 2.0, "changes": {"a": "1"}},
        {"seq": 5, "label": "b", "verdict": "regressed", "delta": -4.0, "noise": 2.0, "changes": {"b": "2"}},
        {"seq": 6, "label": "c", "verdict": "unreliable", "delta": 9.0, "noise": 2.0, "changes": {"c": "3"}},
        {"seq": 8, "label": "d", "verdict": "improved", "delta": 1.5, "noise": 2.0, "changes": {"d": "4"}},
        {"seq": 9, "label": "e", "verdict": "improved", "delta": 6.0, "noise": 2.0, "changes": {"e": "5", "b": "(default)"}}]},
    "2": {"noise": 2.0, "done_seqs": [1, 2, 3], "runs": [
        {"seq": 2, "label": "f", "verdict": "neutral", "delta": 0.5, "noise": 2.0, "changes": {"f": "6"}}]},
    "3": {"noise": 2.0, "done_seqs": [1, 2], "runs": []}}}


def pt(id_, after, session=1, source="t"):
    return {"id": id_, "session": session, "after": after, "source": source}


def run(cid, **chg):
    return {"key": R.choice_key(cid, chg), "id": cid, "changes": chg, "raw": list(chg.items()), "safety": "safe", "kind": "config"}


STOP = R.stop_choice()


def test_measured_same_session_after_the_point_only():
    assert R.measured_of({"a": "1"}, 1, 2, OUT)["delta"] == 5.0
    assert R.measured_of({"a": "1"}, 1, 3, OUT) is None  # measured at/before the point: not hindsight
    assert R.measured_of({"a": "2"}, 1, 0, OUT) is None  # other value
    assert R.measured_of({"e": "5"}, 1, 0, OUT)["seq"] == 9  # a revert riding along still matches
    assert R.measured_of({"e": "5", "x": "1"}, 1, 0, OUT) is None  # the run did not change x


def test_measured_comparable_session_and_unreliable_excluded():
    m = R.measured_of({"f": "6"}, 1, 9, OUT)
    assert m["where"] == "comparable" and m["session"] == 2
    assert R.measured_of({"c": "3"}, 1, 0, OUT) is None  # unreliable runs are no measurement
    assert R.measured_of({"a": "1"}, 3, 0, OUT) is None  # session 3 has no comparable session


def test_stop_good_and_bad():
    e = R.evaluate(STOP, pt(1, 8), OUT)  # after r8: r9 won +6% beyond the noise
    assert not e["good"] and e["missed"] == 6.0 and e["value"] == -6.0 and e["runs_after"] == 2
    e = R.evaluate(STOP, pt(2, 9), OUT)  # nothing after r9
    assert e["good"] and e["value"] == 0
    e = R.evaluate(STOP, pt(3, 7, session=2), OUT)
    assert e["good"]
    # a later improvement within the noise is not a missed win
    out = json.loads(json.dumps(OUT))
    out["sessions"]["1"]["runs"] = [r for r in out["sessions"]["1"]["runs"] if r["seq"] != 9]
    assert R.evaluate(STOP, pt(4, 7), out)["good"]


def test_clip():
    out = json.loads(json.dumps(OUT))
    out["sessions"]["1"]["runs"][0]["delta"] = 65.0
    assert R.evaluate(run("x", a="1"), pt(1, 2), out)["value"] == R.CLIP


def test_metrics():
    pts = [pt(1, 2), pt(2, 4), pt(3, 0), pt(4, 5), pt(5, 8), pt(6, 9)]
    ch = {"1": run("a", a="1"), "2": run("b", b="2"), "3": run("z", z="1"), "4": STOP, "5": STOP, "6": None}
    m = R.metrics(ch, pts, OUT)
    assert m["points"] == 6 and m["run_picks"] == 3 and m["stops"] == 2 and m["no_answer"] == 1
    assert m["measured_picks"] == 2 and m["improved"] == 1 and m["regressed"] == 1 and m["regressed_sum"] == -4.0
    assert m["stop_bad"] == 2 and m["stop_missed_sum"] == 12.0
    assert m["outcome_score"] == round((5 - 4 - 6 - 6) / 6, 3)
    assert m["coverage_pct"] == round(100 * 4 / 6, 1)
    # the session ends at its first STOP (after r5): runs r6..r10 saved, the +6% at r9 missed
    assert m["sessions_stopped"] == 1 and m["runs_saved"] == 5 and m["first_stop_missed"][0][2] == 9


def test_policy_violation():
    def tradeoff_of(var, val):
        return {"category": "durability"} if var == "innodb_flush_log_at_trx_commit" and str(val) != "1" else None
    t = dict(run("trx-commit-2", innodb_flush_log_at_trx_commit="2"), safety="trade-off")
    assert R.policy_violation(t, "safe", tradeoff_of)
    assert R.policy_violation(t, "benchmark", tradeoff_of) is None
    # labelled safe, but the settings are a trade-off (an AI / planner proposal)
    p = dict(run("plan-x", innodb_flush_log_at_trx_commit="0"), safety=None, kind=None)
    assert "durability" in R.policy_violation(p, "safe", tradeoff_of)
    assert R.policy_violation(dict(run("ps-lite", **{"ps:profile": "lite"}), kind="advice"), "benchmark", tradeoff_of)
    assert R.policy_violation(run("ok", a="1"), "safe", tradeoff_of) is None
    assert R.policy_violation(STOP, "safe", tradeoff_of) is None


def test_compare_flags_and_changed_decisions():
    pts = [pt(1, 2), pt(2, 4), pt(3, 8)]
    prev = {"1": run("a", a="1"), "2": run("z", z="1"), "3": STOP}
    same = R.compare(prev, prev, pts, OUT, 0.1)
    assert not same["flags"] and not same["changed"] and same["agreement_pct"] == 100
    worse = {"1": run("z", z="1"), "2": run("b", b="2"), "3": dict(run("t", innodb_flush_log_at_trx_commit="2"), violation="trade-off")}
    c = R.compare(worse, prev, pts, OUT, 0.1)
    assert len(c["flags"]) == 3  # score drop, a regressed pick, a policy violation
    assert len(c["changed"]) == 3 and c["agreement_pct"] == 0
    first = next(x for x in c["changed"] if x["id"] == 1)
    assert first["prev_outcome"].startswith("improved +5.0%") and first["cur_outcome"] == "not measured" and first["gain"] == -5.0
    # within the tolerance: no flag
    small = {"1": run("a", a="1"), "2": run("z", z="1"), "3": run("f", f="6")}  # STOP missing +6% -> f, comparable +0.5%
    assert not R.compare(small, prev, pts, OUT, 0.1)["flags"]
    # only common points are compared (a corpus that grew since the baseline)
    c = R.compare({"1": run("a", a="1"), "9": STOP}, prev, pts + [pt(9, 1)], OUT, 0.1)
    assert c["common"] == 1


def test_baseline_choice():
    with tempfile.TemporaryDirectory() as d:
        for i, (b, acc) in enumerate((("main-aaa", True), ("x-dirty", True), ("main-bbb", False), ("main-ccc", True))):
            json.dump({"kind": "regress-baseline", "build": b, "created": f"2026-10-2{i}", "accepted": acc}, open(os.path.join(d, b + ".json"), "w"))
        json.dump({"key": "not a baseline"}, open(os.path.join(d, "other.json"), "w"))
        assert R.pick_baseline(d, "new", None)["build"] == "main-ccc"
        assert R.pick_baseline(d, "main-ccc", None)["build"] == "x-dirty"  # never itself
        assert R.pick_baseline(d, "new", "main-*")["build"] == "main-ccc"
        assert R.pick_baseline(d, "main-ccc", "main-*")["build"] == "main-aaa"  # main-bbb was flagged, not accepted
        assert R.pick_baseline(d, "new", "main-bbb")["build"] == "main-bbb"  # named explicitly: even a flagged one


def test_comparability_key():
    def sess(cpus, argv, name="x"):
        return {"session": {"id": 1, "facts": json.dumps({"hardware": {"cpus": cpus}})},
                "runs": [{"inputs": json.dumps({"bench": {"name": name, "observed": [{"argv": argv.split()}]}})}]}
    ro = "sysbench oltp_read_only --tables=8 --table-size=88000000 --threads={}"
    k64, k1024 = R.cmp_key(sess(32, ro.format(64))), R.cmp_key(sess(32, ro.format(1024)))
    assert k64 != k1024 and k64.startswith("sysbench:oltp_read_only:8x88000000|low|large")
    assert R.cmp_key(sess(32, ro.format(1024))) == R.cmp_key(sess(32, ro.format(1024) + " --mysql-host=10.0.0.1"))
    assert R.cmp_key(sess(2, ro.format(8))) != R.cmp_key(sess(8, ro.format(16)))  # CPU class
    hdb = R.cmp_key(sess(32, "", "HammerDB TPROC-C 200W, 64 VU (5 min rampup, 10 min measured)"))
    assert hdb == "tprocc:200w|low|large"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa
                fails += 1
                import traceback
                print("FAIL", name, repr(e))
                traceback.print_exc()
    sys.exit(1 if fails else 0)
