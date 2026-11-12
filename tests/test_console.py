"""Admin console data: conversations, activity per agent, usage."""
from tests.test_plugins import make, plugin, rpc


def login(c):
    assert c.post("/api/auth/login", json={"username": "admin", "password": "secret"}).status_code == 200


def chat(c, text):
    sid = c.post("/api/chat/sessions").json()["id"]
    r = c.post(f"/api/chat/sessions/{sid}/stream", json={"text": text, "learn": False})
    assert r.status_code == 200
    return sid


def test_conversations_list_and_transcript(tmp_path):
    c = make(tmp_path, [plugin])
    sid = chat(c, "What are your rates?")
    chat(c, "Do you work weekends?")
    assert c.get("/api/admin/conversations").status_code == 401
    login(c)
    items = c.get("/api/admin/conversations").json()["items"]
    assert [i["summary"] for i in items] == ["Do you work weekends?", "What are your rates?"]
    assert items[0]["status"] == "answered" and items[0]["reply"] and items[0]["channel"] == "chat"
    page = c.get("/api/admin/conversations?limit=1").json()
    assert len(page["items"]) == 1 and page["hasMore"]
    msgs = c.get(f"/api/admin/conversations/{sid}").json()["messages"]
    assert [m["role"] for m in msgs][-2:] == ["user", "assistant"]
    assert c.get("/api/admin/conversations/nope").status_code == 404


def test_activity_and_usage_per_agent(tmp_path):
    c = make(tmp_path, [plugin])
    login(c)
    chat(c, "hello")
    rpc(c, "tools/call", {"name": "add_note", "arguments": {"text": "x"}})   # demo plugin: mcp.call + demo.note_added
    demo = c.get("/api/admin/activity?agent=demo").json()["items"]
    assert sorted(e["action"] for e in demo) == ["demo.note_added", "mcp.call"]
    assert demo[0]["detail"].get("plugin") == "demo" or demo[1]["detail"].get("plugin") == "demo"
    core = [e["action"] for e in c.get("/api/admin/activity?agent=assistant").json()["items"]]
    assert "admin.login" in core and "mcp.call" not in core and "demo.note_added" not in core
    assert c.get("/api/admin/activity?agent=nobody").status_code == 404
    u = c.get("/api/admin/usage").json()
    by = {a["id"]: a for a in u["agents"]}
    assert set(by) == {"assistant", "demo"} and u["costTracked"] is False
    assert by["assistant"]["today"]["messages"] == 1 and by["assistant"]["today"]["conversations"] == 1
    assert by["demo"]["today"] == {"messages": 0, "conversations": 0, "actions": 1, "toolCalls": 1, "modelCalls": 0, "cost": 0.0}
    assert u["history"][0]["toolCalls"] == 1 and len(u["history"]) == 6
    one = c.get("/api/admin/usage?agent=demo").json()
    assert [a["id"] for a in one["agents"]] == ["demo"] and one["history"][0]["messages"] == 0


def test_console_name_setting(tmp_path):
    c = make(tmp_path, [])
    assert c.get("/api/config").json()["brand"]["consoleName"] == "InAgentic Command"


def test_tool_calls_are_logged_with_timing_and_what_they_were_about(tmp_path):
    from app.routers.mcp import summarize_args
    c = make(tmp_path, [plugin])
    login(c)
    rpc(c, "tools/call", {"name": "add_note", "arguments": {"text": "secret text", "title": "Hi"}})
    e = next(x for x in c.get("/api/admin/activity?agent=demo").json()["items"] if x["action"] == "mcp.call")
    assert e["detail"]["ms"] >= 0 and e["detail"]["args"] == "title=Hi" and "secret" not in str(e)
    assert summarize_args({"repos": ["a/b", "c/d"], "files": {"a/b": ["x"]}, "content": "no"}) == "repos=a/b, c/d · files: 1"
