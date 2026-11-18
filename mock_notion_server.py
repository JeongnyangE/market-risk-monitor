"""
테스트용 가짜 Notion API 서버 — notion_sync.py 가 쓰는 엔드포인트만, 실제 API의 주요 제약을 검사한다.

  검사하는 것: 인증·버전 헤더, rich_text 2000자, 요청당 블록 100개, 중첩 2단계,
              표 행의 셀 수 = table_width, DB 속성 이름·유형, 업로드 완료된 파일만 이미지로 첨부,
              429(요청 과다) 응답 1회 — 클라이언트 재시도 확인용
  사용: tests/test_notion_sync.py 참고 (python -m unittest 또는 직접 실행)
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "secret_test_token"


class Bad(Exception):
    def __init__(self, status, code, msg):
        self.status, self.code, self.msg = status, code, msg


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.blocks: dict[str, dict] = {}       # id → {type, data, parent, children[], archived}
        self.pages: dict[str, dict] = {}        # id → {parent, properties, is_db_row, db}
        self.dbs: dict[str, dict] = {}
        self.uploads: dict[str, str] = {}
        self.requests = 0
        self.throttled_once = False
        self.log: list[str] = []

    def new_id(self):
        return str(uuid.uuid4())

    def add_root_page(self, title="리스크 대시보드"):
        pid = self.new_id()
        self.pages[pid] = {"parent": None, "properties": {"title": title}, "children": [], "archived": False}
        return pid

    def container(self, cid):
        if cid in self.pages:
            return self.pages[cid]
        if cid in self.blocks:
            return self.blocks[cid]
        raise Bad(404, "object_not_found", f"Could not find block with ID: {cid}.")


def _rich(lst, where):
    if not isinstance(lst, list):
        raise Bad(400, "validation_error", f"{where} should be an array")
    if len(lst) > 100:
        raise Bad(400, "validation_error", f"{where}.length should be ≤ 100")
    for i, t in enumerate(lst):
        c = t.get("text", {}).get("content")
        if c is None:
            raise Bad(400, "validation_error", f"{where}[{i}].text.content missing")
        if len(c) > 2000:
            raise Bad(400, "validation_error", f"{where}[{i}].text.content.length should be ≤ 2000, instead was {len(c)}")


TEXT_BLOCKS = {"paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item",
               "callout", "quote", "toggle"}


def validate_block(b, st: State, depth: int, path: str):
    t = b.get("type")
    if not t or t not in b:
        raise Bad(400, "validation_error", f"{path}: block type/payload missing")
    d = b[t]
    if t in TEXT_BLOCKS:
        _rich(d.get("rich_text"), f"{path}.{t}.rich_text")
        if t == "callout" and d.get("icon", {}).get("type") not in (None, "emoji", "external"):
            raise Bad(400, "validation_error", f"{path}: bad icon")
    elif t == "table":
        w = d.get("table_width")
        kids = d.get("children") or []
        if not isinstance(w, int) or not kids:
            raise Bad(400, "validation_error", f"{path}: table needs table_width and children")
        for i, r in enumerate(kids):
            if r.get("type") != "table_row" or len(r["table_row"]["cells"]) != w:
                raise Bad(400, "validation_error", f"{path}.children[{i}]: table_row cells != table_width {w}")
            for j, cell in enumerate(r["table_row"]["cells"]):
                _rich(cell, f"{path}.row{i}.cell{j}")
        if depth + 1 > 2:
            raise Bad(400, "validation_error", f"{path}: nesting too deep")
    elif t == "image":
        if d.get("type") == "file_upload":
            fid = d["file_upload"]["id"]
            if st.uploads.get(fid) != "uploaded":
                raise Bad(400, "validation_error", f"{path}: file_upload {fid} not uploaded")
        elif d.get("type") == "external":
            if not str(d["external"].get("url", "")).startswith("http"):
                raise Bad(400, "validation_error", f"{path}: external url invalid")
        else:
            raise Bad(400, "validation_error", f"{path}: image type")
        _rich(d.get("caption", []), f"{path}.caption")
    elif t == "embed":
        if not str(d.get("url", "")).startswith("http"):
            raise Bad(400, "validation_error", f"{path}: embed url")
    elif t == "divider":
        pass
    else:
        raise Bad(400, "validation_error", f"{path}: unsupported block type {t} in mock")
    kids = d.get("children") if t != "table" else None
    if kids:
        if depth + 1 > 2:
            raise Bad(400, "validation_error", f"{path}: nesting too deep")
        for i, k in enumerate(kids):
            validate_block(k, st, depth + 1, f"{path}.children[{i}]")


def store_block(b, parent, st: State):
    bid = st.new_id()
    t = b["type"]
    data = {k: v for k, v in b[t].items() if k != "children"}
    st.blocks[bid] = {"type": t, "data": data, "parent": parent, "children": [], "archived": False}
    for k in (b[t].get("children") or []):
        st.blocks[bid]["children"].append(store_block(k, bid, st))
    if t == "image" and data.get("type") == "file_upload":
        st.uploads[data["file_upload"]["id"]] = "attached"
    return bid


def block_json(bid, st: State):
    b = st.blocks[bid]
    out = {"object": "block", "id": bid, "type": b["type"], "has_children": bool(b["children"]), "archived": b["archived"]}
    if b["type"] == "child_page":
        out["child_page"] = {"title": b["data"]["title"]}
    elif b["type"] == "child_database":
        out["child_database"] = {"title": b["data"]["title"]}
    else:
        out[b["type"]] = b["data"]
    return out


PROP_TYPES = {"title", "rich_text", "number", "select", "date"}


def validate_props(props, schema, creating: bool):
    for name, val in props.items():
        if name not in schema:
            raise Bad(400, "validation_error", f"{name} is not a property that exists.")
        typ = schema[name]["type"]
        if typ not in val:
            raise Bad(400, "validation_error", f"{name} is expected to be {typ}.")
        v = val[typ]
        if typ in ("title", "rich_text"):
            _rich(v, name)
        elif typ == "number" and v is not None and not isinstance(v, (int, float)):
            raise Bad(400, "validation_error", f"{name} is expected to be number.")
        elif typ == "select" and v is not None and not isinstance(v.get("name"), str):
            raise Bad(400, "validation_error", f"{name}.select.name should be a string")
        elif typ == "date" and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(v.get("start", ""))):
            raise Bad(400, "validation_error", f"{name}.date.start invalid")
    if creating and not any(schema[n]["type"] == "title" for n in props):
        raise Bad(400, "validation_error", "title property missing")


def page_json(pid, st: State):
    p = st.pages[pid]
    props = {}
    if p.get("db"):
        schema = st.dbs[p["db"]]["properties"]
        for n, v in p["properties"].items():
            t = schema[n]["type"]
            props[n] = {"type": t, t: v[t]}
    return {"object": "page", "id": pid, "url": f"https://www.notion.so/{pid.replace('-', '')}", "properties": props,
            "archived": p.get("archived", False)}


class Handler(BaseHTTPRequestHandler):
    st: State = None

    def log_message(self, *a):
        pass

    def _send(self, code, obj, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _handle(self, method):
        st = self.st
        raw = self._body()
        try:
            with st.lock:
                st.requests += 1
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    raise Bad(401, "unauthorized", "API token is invalid.")
                if not self.headers.get("Notion-Version"):
                    raise Bad(400, "missing_version", "Notion-Version header missing")
                if st.requests == 4 and not st.throttled_once:
                    st.throttled_once = True
                    self._send(429, {"object": "error", "status": 429, "code": "rate_limited", "message": "slow down"},
                               {"Retry-After": "0.2"})
                    return
                u = urlparse(self.path)
                parts = [p for p in u.path.split("/") if p][1:]      # drop 'v1'
                st.log.append(f"{method} /{'/'.join(parts)}")
                body = json.loads(raw) if raw and self.headers.get("Content-Type", "").startswith("application/json") else None
                res = self.route(method, parts, parse_qs(u.query), body, raw)
            self._send(200, res)
        except Bad as e:
            self._send(e.status, {"object": "error", "status": e.status, "code": e.code, "message": e.msg})

    def route(self, method, p, q, body, raw):
        st = self.st
        if p[0] == "blocks" and len(p) == 3 and p[2] == "children":
            parent = st.container(p[1])
            if method == "GET":
                kids = [k for k in parent["children"] if not st.blocks[k]["archived"]]
                size = int(q.get("page_size", ["100"])[0])
                start = int(q.get("start_cursor", ["0"])[0])
                chunk = kids[start:start + size]
                more = start + size < len(kids)
                return {"object": "list", "results": [block_json(k, st) for k in chunk], "has_more": more,
                        "next_cursor": str(start + size) if more else None}
            if method == "PATCH":
                kids = body.get("children", [])
                if len(kids) > 100:
                    raise Bad(400, "validation_error", "body.children.length should be ≤ 100")
                for i, b in enumerate(kids):
                    validate_block(b, st, 1, f"children[{i}]")
                ids = [store_block(b, p[1], st) for b in kids]
                parent["children"] += ids
                return {"object": "list", "results": [block_json(i, st) for i in ids]}
        if p[0] == "blocks" and len(p) == 2 and method == "DELETE":
            if p[1] not in st.blocks:
                raise Bad(404, "object_not_found", "no block")
            st.blocks[p[1]]["archived"] = True
            return block_json(p[1], st)
        if p[0] == "pages" and len(p) == 1 and method == "POST":
            par = body["parent"]
            if "database_id" in par:
                db = st.dbs.get(par["database_id"]) or (_ for _ in ()).throw(Bad(404, "object_not_found", "db"))
                validate_props(body["properties"], db["properties"], True)
                pid = st.new_id()
                st.pages[pid] = {"parent": par["database_id"], "db": par["database_id"],
                                 "properties": body["properties"], "children": []}
                db["rows"].append(pid)
                return page_json(pid, st)
            parent = st.container(par["page_id"])
            title = body["properties"]["title"]["title"][0]["text"]["content"]
            pid = st.new_id()
            st.pages[pid] = {"parent": par["page_id"], "properties": {"title": title}, "children": []}
            st.blocks[pid] = {"type": "child_page", "data": {"title": title}, "parent": par["page_id"], "children": [],
                              "archived": False}
            st.pages[pid]["children"] = st.blocks[pid]["children"]
            parent["children"].append(pid)
            return page_json(pid, st)
        if p[0] == "pages" and len(p) == 2 and method == "PATCH":
            pg = st.pages.get(p[1]) or (_ for _ in ()).throw(Bad(404, "object_not_found", "page"))
            validate_props(body["properties"], st.dbs[pg["db"]]["properties"], False)
            pg["properties"].update(body["properties"])
            return page_json(p[1], st)
        if p[0] == "databases" and len(p) == 1 and method == "POST":
            parent = st.container(body["parent"]["page_id"])
            props = body["properties"]
            if sum(1 for v in props.values() if "title" in v) != 1:
                raise Bad(400, "validation_error", "exactly one title property required")
            schema = {}
            for n, v in props.items():
                t = next(iter(v))
                if t not in PROP_TYPES:
                    raise Bad(400, "validation_error", f"unsupported property type {t}")
                schema[n] = {"type": t, t: v[t]}
            did = st.new_id()
            title = body["title"][0]["text"]["content"]
            st.dbs[did] = {"properties": schema, "rows": [], "title": title}
            st.blocks[did] = {"type": "child_database", "data": {"title": title}, "parent": body["parent"]["page_id"],
                              "children": [], "archived": False}
            parent["children"].append(did)
            return {"object": "database", "id": did, "properties": schema}
        if p[0] == "databases" and len(p) == 2:
            db = st.dbs.get(p[1]) or (_ for _ in ()).throw(Bad(404, "object_not_found", "db"))
            if method == "GET":
                return {"object": "database", "id": p[1], "properties": db["properties"]}
            if method == "PATCH":
                for n, v in body.get("properties", {}).items():
                    t = next(iter(v))
                    db["properties"][n] = {"type": t, t: v[t]}
                return {"object": "database", "id": p[1], "properties": db["properties"]}
        if p[0] == "databases" and len(p) == 3 and p[2] == "query":
            db = st.dbs[p[1]]
            f = (body or {}).get("filter")
            rows = []
            for pid in db["rows"]:
                pg = st.pages[pid]
                if f:
                    d = pg["properties"][f["property"]]["date"]["start"]
                    cond = f["date"]
                    if "on_or_after" in cond and d < cond["on_or_after"]:
                        continue
                    if "equals" in cond and d != cond["equals"]:
                        continue
                rows.append(pid)
            size = int(body.get("page_size", 100))
            start = int(body.get("start_cursor", "0"))
            more = start + size < len(rows)
            return {"object": "list", "results": [page_json(r, st) for r in rows[start:start + size]],
                    "has_more": more, "next_cursor": str(start + size) if more else None}
        if p[0] == "file_uploads" and len(p) == 1 and method == "POST":
            fid = st.new_id()
            st.uploads[fid] = "pending"
            return {"object": "file_upload", "id": fid, "status": "pending",
                    "upload_url": f"https://api.notion.com/v1/file_uploads/{fid}/send"}
        if p[0] == "file_uploads" and len(p) == 3 and p[2] == "send":
            ct = self.headers.get("Content-Type", "")
            if not ct.startswith("multipart/form-data") or b'name="file"' not in raw or b"\x89PNG" not in raw:
                raise Bad(400, "validation_error", "multipart 'file' part with PNG data required")
            st.uploads[p[1]] = "uploaded"
            return {"object": "file_upload", "id": p[1], "status": "uploaded"}
        raise Bad(400, "invalid_request_url", f"{method} {'/'.join(p)} not supported by mock")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")


def start(port: int = 0):
    st = State()
    handler = type("H", (Handler,), {"st": st})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st
