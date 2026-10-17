import json
from pathlib import Path

import pytest

from twincat_mcp.config import SafetyConfig
from twincat_mcp.safety import Guard, OpClass, SafetyError


def mk(mode="guarded", **kw):
    cfg = SafetyConfig(mode=mode, **kw)
    cfg.max_mode = kw.get("max_mode", mode)
    return Guard(cfg)


def test_readonly_blocks_everything_but_read():
    g = mk("readonly")
    g.check(OpClass.READ, action="x")
    for op in (OpClass.EDIT, OpClass.WRITE, OpClass.CONTROL, OpClass.DESTRUCTIVE):
        with pytest.raises(SafetyError):
            g.check(op, action="x", target="y")


def test_guarded_write_needs_allowlist_and_respects_denylist():
    g = mk("guarded")
    with pytest.raises(SafetyError, match="allowlist is empty"):
        g.check(OpClass.WRITE, action="ads_write", target="MAIN.nX")
    g = mk("guarded", write_allowlist=["MAIN.fb*"], write_denylist=["*.bOpen"])
    g.check(OpClass.WRITE, action="ads_write", target="MAIN.fbFill.tSettle")
    with pytest.raises(SafetyError, match="not in write_allowlist"):
        g.check(OpClass.WRITE, action="ads_write", target="GVL.nY")
    with pytest.raises(SafetyError, match="denylist"):
        g.check(OpClass.WRITE, action="ads_write", target="MAIN.fbFill.fbValve.bOpen")


def test_full_mode_write_ignores_allowlist_but_not_denylist():
    g = mk("full", write_denylist=["*AT_Q*"])
    g.check(OpClass.WRITE, action="ads_write", target="Anything.Goes")
    with pytest.raises(SafetyError):
        g.check(OpClass.WRITE, action="ads_write", target="MAIN.AT_Q_bOut")


def test_control_token_flow_guarded():
    g = mk("guarded")
    with pytest.raises(SafetyError, match="needs a confirm_token"):
        g.check(OpClass.CONTROL, action="ads_plc_state", target="stop")
    tok = g.request_token("ads_plc_state", "stop")["confirm_token"]
    with pytest.raises(SafetyError, match="issued for"):
        g.check(OpClass.CONTROL, action="ads_plc_state", target="run", confirm_token=tok)
    g.check(OpClass.CONTROL, action="ads_plc_state", target="stop", confirm_token=tok)
    with pytest.raises(SafetyError, match="already used"):
        g.check(OpClass.CONTROL, action="ads_plc_state", target="stop", confirm_token=tok)


def test_full_mode_control_free_destructive_still_confirms():
    g = mk("full")
    g.check(OpClass.CONTROL, action="ads_plc_state", target="stop")
    with pytest.raises(SafetyError):
        g.check(OpClass.DESTRUCTIVE, action="xae_activate_configuration", target="5.1.1.1.1.1")
    g2 = mk("full", confirm_destructive=False)
    g2.check(OpClass.DESTRUCTIVE, action="xae_activate_configuration", target="5.1.1.1.1.1")


def test_mode_escalation_capped():
    g = mk("guarded", max_mode="guarded")
    with pytest.raises(SafetyError, match="ceiling"):
        g.set_mode("full")
    g.set_mode("readonly")
    assert g.mode == "readonly"
    g2 = mk("guarded", max_mode="full")
    g2.set_mode("full")
    assert g2.mode == "full"


def test_audit_log_written(tmp_path):
    g = mk("full", audit_log=str(tmp_path / "a.jsonl"))
    g.audit("ads_write", {"name": "X", "value": 1}, OpClass.WRITE, "ok")
    g.audit("ads_read", {"name": "X"}, OpClass.READ, "ok")  # reads not audited by default
    lines = Path(tmp_path / "a.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["tool"] == "ads_write" and rec["mode"] == "full"
    assert g.tail_audit(5)[0]["tool"] == "ads_write"


def test_n78_status_purges_expired_and_spent_tokens(monkeypatch):
    """#78: tc_safety_status listed a token that had already expired (and kept it until the next issue)."""
    import time as _time
    g = mk("guarded", confirm_ttl_s=10)
    now = [1000.0]
    monkeypatch.setattr(_time, "time", lambda: now[0])
    a = g.request_token("ads_write", "MAIN.nA")["confirm_token"]
    b = g.request_token("ads_write", "MAIN.nB")["confirm_token"]
    g.consume_token(b, "ads_write", "MAIN.nB")
    assert [t["target"] for t in g.status()["pending_tokens"]] == ["MAIN.nA"]
    now[0] += 10.1
    assert g.status()["pending_tokens"] == [] and a not in g._tokens and b not in g._tokens
