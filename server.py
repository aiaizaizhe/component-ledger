#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
元件库 —— 本地单机版
一个人录入、一个人记账：清单批量入库、手动借还、位置管理。

启动：  python server.py
        python server.py --data-dir "D:\\元件库数据" --open
数据：  默认 data/components.db，可在 config.json 里改 data_dir
"""

import argparse
import csv
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.parse
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from PIL import Image, ImageOps
except Exception:      # 没有 Pillow 也能跑，只是不帮忙摆正/缩放
    Image = None
    ImageOps = None

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
CONFIG_PATH = BASE_DIR / "config.json"
OCR_SCRIPT = BASE_DIR / "ocr.ps1"
BACKUP_DIR = BASE_DIR / "backup"

DB_LOCK = threading.RLock()
CONFIG = {}
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "components.db"

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return date.today().isoformat()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def clean(value, maxlen: int = 200) -> str:
    if value is None:
        return ""
    return str(value).strip()[:maxlen]


def to_int(value, field: str, minimum: int = 0) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ApiError(400, f"「{field}」必须是整数")
    if number < minimum:
        raise ApiError(400, f"「{field}」不能小于 {minimum}")
    return number


def norm_dt(value, fallback: str = "") -> str:
    """把 datetime-local / 日期 / 空值统一成 'YYYY-MM-DD HH:MM:SS'。"""
    text = clean(value, 30).replace("T", " ")
    if not text:
        return fallback or now_str()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ApiError(400, "时间格式不对，应为 2026-10-22 23:30")


# --------------------------------------------------------------------------
# 配置与数据目录
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {"data_dir": "", "port": 8787, "host": "127.0.0.1"}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in DEFAULT_CONFIG:
                    if key in raw:
                        cfg[key] = raw[key]
        except Exception:
            pass
    else:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def apply_paths(data_dir_override: str = "") -> None:
    global CONFIG, DATA_DIR, DB_PATH
    CONFIG = load_config()
    chosen = data_dir_override or CONFIG.get("data_dir") or "data"
    path = Path(chosen)
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    DATA_DIR = path
    DB_PATH = DATA_DIR / "components.db"


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS components (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT UNIQUE,
    name        TEXT NOT NULL,
    spec        TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    room_id     INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    unit        TEXT NOT NULL DEFAULT '个',
    quantity    INTEGER NOT NULL DEFAULT 0,
    min_stock   INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    deleted     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id  INTEGER NOT NULL REFERENCES components(id),
    borrower      TEXT NOT NULL,
    qty           INTEGER NOT NULL CHECK (qty > 0),
    returned_qty  INTEGER NOT NULL DEFAULT 0,
    lost_qty      INTEGER NOT NULL DEFAULT 0,
    taken_at      TEXT NOT NULL,
    due_date      TEXT,
    purpose       TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    returned_at   TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    room_id     INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
    reverted    INTEGER NOT NULL DEFAULT 0,
    reverted_at TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS movements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    type            TEXT NOT NULL,          -- new / in / out / return / adjust / loss / delete / revert
    component_id    INTEGER,
    component_name  TEXT NOT NULL DEFAULT '',
    component_code  TEXT NOT NULL DEFAULT '',
    qty             INTEGER NOT NULL DEFAULT 0,
    actor           TEXT NOT NULL DEFAULT '',
    borrower        TEXT NOT NULL DEFAULT '',
    loan_id         INTEGER,
    batch_id        INTEGER,
    reverted        INTEGER NOT NULL DEFAULT 0,
    revert_of       INTEGER,
    reason          TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_loans_component ON loans(component_id);
CREATE INDEX IF NOT EXISTS idx_loans_open ON loans(returned_qty, qty);
CREATE INDEX IF NOT EXISTS idx_moves_ts ON movements(ts);
CREATE INDEX IF NOT EXISTS idx_moves_batch ON movements(batch_id);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.commit()
    finally:
        conn.close()


def migrate(conn) -> None:
    """兼容更早的版本（位置是纯文本、没有入库单）。"""
    comp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(components)")}
    if "deleted" not in comp_cols:
        conn.execute("ALTER TABLE components ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    if "location_id" not in comp_cols:
        conn.execute("ALTER TABLE components ADD COLUMN location_id INTEGER REFERENCES locations(id)")
        if "location" in comp_cols:
            rows = conn.execute("SELECT id, location FROM components WHERE location <> ''").fetchall()
            for row in rows:
                name = row["location"].strip()
                if not name:
                    continue
                loc = conn.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()
                if not loc:
                    cur = conn.execute(
                        "INSERT INTO locations (name, created_at) VALUES (?, ?)", (name, now_str())
                    )
                    loc_id = cur.lastrowid
                else:
                    loc_id = loc["id"]
                conn.execute("UPDATE components SET location_id = ? WHERE id = ?", (loc_id, row["id"]))

    # 老版本把「分类」存成一段文字，这里转成可维护的分类表
    if "category_id" not in comp_cols:
        conn.execute("ALTER TABLE components ADD COLUMN category_id INTEGER REFERENCES categories(id)")
        if "category" in comp_cols:
            rows = conn.execute("SELECT id, category FROM components WHERE category <> ''").fetchall()
            for row in rows:
                name = row["category"].strip()
                if not name:
                    continue
                cat = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
                if not cat:
                    cur = conn.execute(
                        "INSERT INTO categories (name, created_at) VALUES (?, ?)", (name, now_str())
                    )
                    cat_id = cur.lastrowid
                else:
                    cat_id = cat["id"]
                conn.execute("UPDATE components SET category_id = ? WHERE id = ?", (cat_id, row["id"]))

    if "room_id" not in comp_cols:
        conn.execute("ALTER TABLE components ADD COLUMN room_id INTEGER REFERENCES rooms(id)")

    move_cols = {r["name"] for r in conn.execute("PRAGMA table_info(movements)")}
    if "batch_id" not in move_cols:
        conn.execute("ALTER TABLE movements ADD COLUMN batch_id INTEGER")
    if "reverted" not in move_cols:
        conn.execute("ALTER TABLE movements ADD COLUMN reverted INTEGER NOT NULL DEFAULT 0")
    if "revert_of" not in move_cols:
        conn.execute("ALTER TABLE movements ADD COLUMN revert_of INTEGER")
    if "reason" not in move_cols:
        conn.execute("ALTER TABLE movements ADD COLUMN reason TEXT NOT NULL DEFAULT ''")

    loan_cols = {r["name"] for r in conn.execute("PRAGMA table_info(loans)")}
    if "lost_qty" not in loan_cols:
        conn.execute("ALTER TABLE loans ADD COLUMN lost_qty INTEGER NOT NULL DEFAULT 0")

    batch_cols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)")}
    if "reverted" not in batch_cols:
        conn.execute("ALTER TABLE batches ADD COLUMN reverted INTEGER NOT NULL DEFAULT 0")
    if "reverted_at" not in batch_cols:
        conn.execute("ALTER TABLE batches ADD COLUMN reverted_at TEXT")
    if "room_id" not in batch_cols:
        conn.execute("ALTER TABLE batches ADD COLUMN room_id INTEGER REFERENCES rooms(id)")


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
COMP_SELECT = """
SELECT c.*, l.name AS location_name, cg.name AS category_name, r.name AS room_name,
       COALESCE((SELECT SUM(l2.qty - l2.returned_qty) FROM loans l2
                 WHERE l2.component_id = c.id AND l2.returned_qty < l2.qty), 0) AS out_qty,
       COALESCE((SELECT SUM(m3.qty) FROM movements m3
                 WHERE m3.component_id = c.id AND m3.type = 'loss'), 0) AS lost_qty,
       um.id AS undo_id, um.type AS undo_type, um.qty AS undo_qty,
       um.ts AS undo_ts, um.note AS undo_note
FROM components c
LEFT JOIN locations l ON l.id = c.location_id
LEFT JOIN categories cg ON cg.id = c.category_id
LEFT JOIN rooms r ON r.id = c.room_id
LEFT JOIN (
    SELECT component_id, MAX(id) AS mid FROM movements
    WHERE type IN ('in', 'adjust') AND reverted = 0 AND batch_id IS NULL
          AND component_id IS NOT NULL
    GROUP BY component_id
) u ON u.component_id = c.id
LEFT JOIN movements um ON um.id = u.mid
"""


def comp_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "code": row["code"] or "",
        "name": row["name"],
        "spec": row["spec"],
        "category_id": row["category_id"],
        "category": row["category_name"] or "",
        "room_id": row["room_id"],
        "room": row["room_name"] or "",
        "location_id": row["location_id"],
        "location": row["location_name"] or "",
        "unit": row["unit"],
        "quantity": row["quantity"],
        "min_stock": row["min_stock"],
        "note": row["note"],
        "out_qty": row["out_qty"],
        "lost": row["lost_qty"],
        "low": row["quantity"] <= row["min_stock"],
        "undo": ({
            "id": row["undo_id"],
            "type": row["undo_type"],
            "qty": row["undo_qty"],
            "ts": row["undo_ts"],
            "note": row["undo_note"] or "",
        } if row["undo_id"] else None),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def loan_to_dict(row) -> dict:
    remaining = row["qty"] - row["returned_qty"]
    due = row["due_date"]
    days_left = None
    if due:
        try:
            days_left = (date.fromisoformat(due) - date.today()).days
        except ValueError:
            days_left = None
    if remaining <= 0:
        status = "returned"
    elif row["returned_qty"] > 0:
        status = "partial"
    else:
        status = "open"
    return {
        "id": row["id"],
        "component_id": row["component_id"],
        "component_name": row["component_name"] or "",
        "component_code": row["component_code"] or "",
        "component_unit": row["component_unit"] or "个",
        "borrower": row["borrower"],
        "qty": row["qty"],
        "returned_qty": row["returned_qty"],
        "lost_qty": row["lost_qty"],
        "remaining": remaining,
        "taken_at": row["taken_at"],
        "due_date": due or "",
        "purpose": row["purpose"],
        "note": row["note"],
        "returned_at": row["returned_at"] or "",
        "status": status,
        "overdue": bool(remaining > 0 and due and due < today_str()),
        "days_left": days_left,
    }


LOAN_SELECT = """
SELECT l.*, c.name AS component_name, c.code AS component_code, c.unit AS component_unit
FROM loans l JOIN components c ON c.id = l.component_id
"""


def fetch_components(conn) -> list:
    rows = conn.execute(
        COMP_SELECT + """ WHERE c.deleted = 0
        ORDER BY (c.room_id IS NULL), r.name, (c.location_id IS NULL), l.name,
                 cg.name, c.name COLLATE NOCASE"""
    ).fetchall()
    return [comp_to_dict(r) for r in rows]


def fetch_lookup(conn, table: str) -> list:
    """locations / categories / rooms 三张表结构一样，统一取。"""
    column = lookup_column(table)
    rows = conn.execute(
        f"""SELECT t.*, (SELECT COUNT(*) FROM components c
                         WHERE c.{column} = t.id AND c.deleted = 0) AS count
            FROM {table} t ORDER BY t.id"""
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "note": r["note"], "count": r["count"]} for r in rows]


def fetch_locations(conn) -> list:
    return fetch_lookup(conn, "locations")


def fetch_categories(conn) -> list:
    return fetch_lookup(conn, "categories")


def fetch_rooms(conn) -> list:
    return fetch_lookup(conn, "rooms")


def fetch_open_loans(conn) -> list:
    rows = conn.execute(
        LOAN_SELECT + " WHERE l.returned_qty < l.qty ORDER BY l.due_date IS NULL, l.due_date, l.id"
    ).fetchall()
    return [loan_to_dict(r) for r in rows]


def compute_stats(components, loans) -> dict:
    return {
        "kinds": len(components),
        "in_stock": sum(c["quantity"] for c in components),
        "out_units": sum(l["remaining"] for l in loans),
        "open_loans": len(loans),
        "overdue": sum(1 for l in loans if l["overdue"]),
        "borrowers": len({l["borrower"] for l in loans if l["borrower"]}),
        "low_stock": sum(1 for c in components if c["low"]),
        "locations": 0,
    }


def log_move(conn, *, type_, comp=None, qty=0, actor="", borrower="", loan_id=None,
             batch_id=None, note="", reason="", ts=None):
    conn.execute(
        """INSERT INTO movements
           (ts, type, component_id, component_name, component_code, qty, actor, borrower,
            loan_id, batch_id, reason, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts or now_str(),
            type_,
            comp["id"] if comp else None,
            comp["name"] if comp else "",
            (comp["code"] or "") if comp else "",
            qty,
            actor,
            borrower,
            loan_id,
            batch_id,
            reason,
            note,
        ),
    )


# --------------------------------------------------------------------------
# 分类 / 物理储存 / 存放位置（三张表的增删改一样）
# --------------------------------------------------------------------------
LOOKUP_LABEL = {"locations": "位置", "categories": "分类", "rooms": "房间"}
LOOKUP_COLUMN = {"locations": "location_id", "categories": "category_id", "rooms": "room_id"}


def lookup_column(table: str) -> str:
    return LOOKUP_COLUMN[table]


def create_lookup(table: str, payload: dict) -> dict:
    label = LOOKUP_LABEL[table]
    name = clean(payload.get("name"), 40)
    if not name:
        raise ApiError(400, f"{label}名称不能为空")
    note = clean(payload.get("note"), 100)
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    f"INSERT INTO {table} (name, note, created_at) VALUES (?,?,?)",
                    (name, note, now_str()),
                )
            except sqlite3.IntegrityError:
                raise ApiError(409, f"这个{label}已经存在了")
            conn.commit()
            return {"id": cur.lastrowid, "name": name, "note": note, "count": 0}
        finally:
            conn.close()


def rename_lookup(table: str, lid: int, payload: dict) -> dict:
    label = LOOKUP_LABEL[table]
    name = clean(payload.get("name"), 40)
    if not name:
        raise ApiError(400, f"{label}名称不能为空")
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (lid,)).fetchone():
                raise ApiError(404, f"{label}不存在")
            try:
                conn.execute(f"UPDATE {table} SET name = ? WHERE id = ?", (name, lid))
            except sqlite3.IntegrityError:
                raise ApiError(409, f"已经有一个同名{label}了")
            conn.commit()
            column = lookup_column(table)
            row = conn.execute(
                f"""SELECT t.*, (SELECT COUNT(*) FROM components c
                                  WHERE c.{column} = t.id AND c.deleted = 0) AS count
                    FROM {table} t WHERE t.id = ?""", (lid,)
            ).fetchone()
            return {"id": row["id"], "name": row["name"], "note": row["note"], "count": row["count"]}
        finally:
            conn.close()


def delete_lookup(table: str, lid: int) -> dict:
    label = LOOKUP_LABEL[table]
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (lid,)).fetchone():
                raise ApiError(404, f"{label}不存在")
            column = lookup_column(table)
            count = conn.execute(
                f"SELECT COUNT(*) FROM components WHERE {column} = ? AND deleted = 0", (lid,)
            ).fetchone()[0]
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (lid,))
            conn.commit()
            return {"ok": True, "detached": count}
        finally:
            conn.close()


# --------------------------------------------------------------------------
# 元件
# --------------------------------------------------------------------------
def resolve_lookup(conn, table: str, value, label: str):
    if value in (None, "", 0):
        return None
    vid = to_int(value, label, 1)
    if not conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (vid,)).fetchone():
        raise ApiError(400, f"{label}不存在")
    return vid


def create_component(payload: dict, conn=None, batch_id=None) -> dict:
    name = clean(payload.get("name"), 80)
    if not name:
        raise ApiError(400, "元件名称不能为空")
    quantity = to_int(payload.get("quantity", 0), "在库数量", 0)
    unit = clean(payload.get("unit"), 10) or "个"
    ts = now_str()

    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        if own_conn:
            conn.execute("BEGIN IMMEDIATE")

        category_id = resolve_lookup(conn, "categories", payload.get("category_id"), "分类")
        room_id = resolve_lookup(conn, "rooms", payload.get("room_id"), "物理储存")
        location_id = resolve_lookup(conn, "locations", payload.get("location_id"), "位置")

        cur = conn.execute(
            """INSERT INTO components
               (code, name, spec, category_id, room_id, location_id, unit, quantity,
                min_stock, note, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                clean(payload.get("code"), 40) or None,
                name,
                clean(payload.get("spec"), 120),
                category_id,
                room_id,
                location_id,
                unit,
                quantity,
                to_int(payload.get("min_stock", 0), "安全库存", 0),
                clean(payload.get("note"), 300),
                ts,
                ts,
            ),
        )
        new_id = cur.lastrowid
        if not clean(payload.get("code"), 40):
            conn.execute("UPDATE components SET code = ? WHERE id = ?", (f"YJ{new_id:04d}", new_id))
        row = conn.execute(COMP_SELECT + " WHERE c.id = ?", (new_id,)).fetchone()
        log_move(conn, type_="new", comp=row, qty=quantity, actor="我", batch_id=batch_id,
                 note=clean(payload.get("note"), 100) or ("" if batch_id else "新增元件"))
        if own_conn:
            conn.commit()
        return comp_to_dict(row)
    except sqlite3.IntegrityError:
        if own_conn:
            conn.rollback()
        raise ApiError(409, "编号已存在，请换一个")
    finally:
        if own_conn:
            conn.close()


def update_component(cid: int, payload: dict) -> dict:
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
            if not row:
                raise ApiError(404, "元件不存在")
            fields, values, adjustments = [], [], []

            for key, limit in (("name", 80), ("spec", 120), ("unit", 10),
                               ("note", 300), ("code", 40)):
                if key in payload:
                    val = clean(payload[key], limit)
                    if key == "name" and not val:
                        raise ApiError(400, "元件名称不能为空")
                    if key == "code" and not val:
                        continue
                    fields.append(f"{key} = ?")
                    values.append(val)

            if "category_id" in payload:
                fields.append("category_id = ?")
                values.append(resolve_lookup(conn, "categories", payload["category_id"], "分类"))

            if "room_id" in payload:
                fields.append("room_id = ?")
                values.append(resolve_lookup(conn, "rooms", payload["room_id"], "物理储存"))

            if "location_id" in payload:
                fields.append("location_id = ?")
                values.append(resolve_lookup(conn, "locations", payload["location_id"], "位置"))

            if "min_stock" in payload:
                fields.append("min_stock = ?")
                values.append(to_int(payload["min_stock"], "安全库存", 0))

            if "quantity" in payload:
                new_qty = to_int(payload["quantity"], "在库数量", 0)
                delta = new_qty - row["quantity"]
                fields.append("quantity = ?")
                values.append(new_qty)
                if delta:
                    adjustments.append((delta, new_qty))

            if not fields:
                return comp_to_dict(row)

            fields.append("updated_at = ?")
            values.append(now_str())
            values.append(cid)
            conn.execute(f"UPDATE components SET {', '.join(fields)} WHERE id = ?", values)

            for delta, new_qty in adjustments:
                log_move(conn, type_="adjust", comp=row, qty=delta, actor="我",
                         note=f"盘点：{row['quantity']} → {new_qty}")

            new_row = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
            conn.commit()
            return comp_to_dict(new_row)
        except sqlite3.IntegrityError:
            conn.rollback()
            raise ApiError(409, "编号已存在，请换一个")
        finally:
            conn.close()


def stock_in(cid: int, payload: dict) -> dict:
    qty = to_int(payload.get("qty"), "入库数量", 1)
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
            if not row:
                raise ApiError(404, "元件不存在")
            conn.execute("UPDATE components SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                         (qty, now_str(), cid))
            log_move(conn, type_="in", comp=row, qty=qty, actor="我",
                     note=clean(payload.get("note"), 200) or "手动入库")
            new_row = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
            conn.commit()
            return comp_to_dict(new_row)
        finally:
            conn.close()


def adjust_stock(cid: int, payload: dict) -> dict:
    delta = to_int(payload.get("qty"), "调整数量", -1000000)
    if delta == 0:
        raise ApiError(400, "调整数量不能为 0")
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
            if not row:
                raise ApiError(404, "元件不存在")
            new_qty = row["quantity"] + delta
            if new_qty < 0:
                raise ApiError(400, f"调整后库存会变成负数（当前 {row['quantity']}）")
            conn.execute("UPDATE components SET quantity = ?, updated_at = ? WHERE id = ?",
                         (new_qty, now_str(), cid))
            log_move(conn, type_="adjust", comp=row, qty=delta, actor="我",
                     note=clean(payload.get("note"), 200) or f"盘点：{row['quantity']} → {new_qty}")
            new_row = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
            conn.commit()
            return comp_to_dict(new_row)
        finally:
            conn.close()


def delete_component(cid: int) -> dict:
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
            if not row:
                raise ApiError(404, "元件不存在")
            if row["out_qty"] > 0:
                raise ApiError(409, f"还有 {row['out_qty']} {row['unit']}没归还，不能删除")
            conn.execute("UPDATE components SET deleted = 1, updated_at = ? WHERE id = ?",
                         (now_str(), cid))
            log_move(conn, type_="delete", comp=row, qty=0, actor="我", note="删除元件")
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()


def revert_component_last(cid: int) -> dict:
    """撤回这个元件最近一次「手动入库 / 盘点」。"""
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            comp = conn.execute("SELECT * FROM components WHERE id = ? AND deleted = 0", (cid,)).fetchone()
            if not comp:
                raise ApiError(404, "元件不存在")
            move = conn.execute(
                """SELECT * FROM movements
                   WHERE component_id = ? AND type IN ('in', 'adjust')
                         AND batch_id IS NULL AND reverted = 0
                   ORDER BY id DESC LIMIT 1""", (cid,)
            ).fetchone()
            if not move:
                raise ApiError(400, "这个元件没有可撤回的入库记录")

            delta = -move["qty"]
            new_qty = comp["quantity"] + delta
            if new_qty < 0:
                raise ApiError(409, f"撤回后库存会变成负数（现在 {comp['quantity']}，这次是 {move['qty']:+d}）")

            conn.execute("UPDATE components SET quantity = ?, updated_at = ? WHERE id = ?",
                         (new_qty, now_str(), cid))
            conn.execute("UPDATE movements SET reverted = 1 WHERE id = ?", (move["id"],))
            label = "入库" if move["type"] == "in" else "盘点"
            log_move(conn, type_="revert", comp=comp, qty=delta, actor="我",
                     note=f"撤回 {move['ts'][:16]} 的{label}（{move['qty']:+d}）")
            conn.commit()
            return {"ok": True, "quantity": new_qty, "reverted_qty": delta}
        finally:
            conn.close()


# --------------------------------------------------------------------------
# 借出 / 归还
# --------------------------------------------------------------------------
def create_loan(payload: dict) -> dict:
    cid = to_int(payload.get("component_id"), "元件", 1)
    qty = to_int(payload.get("qty"), "借出数量", 1)
    borrower = clean(payload.get("borrower"), 40)
    if not borrower:
        raise ApiError(400, "请填写借用人")
    taken_at = norm_dt(payload.get("taken_at"))
    due = clean(payload.get("due_date"), 10)
    if due:
        try:
            date.fromisoformat(due)
        except ValueError:
            raise ApiError(400, "归还日期格式应为 YYYY-MM-DD")

    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
            if not row:
                raise ApiError(404, "元件不存在")
            if qty > row["quantity"]:
                raise ApiError(400, f"库存不足：{row['name']} 当前只剩 {row['quantity']} {row['unit']}")
            conn.execute("UPDATE components SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
                         (qty, now_str(), cid))
            cur = conn.execute(
                """INSERT INTO loans (component_id, borrower, qty, taken_at, due_date, purpose, note)
                   VALUES (?,?,?,?,?,?,?)""",
                (cid, borrower, qty, taken_at, due or None,
                 clean(payload.get("purpose"), 100), clean(payload.get("note"), 200)),
            )
            loan_id = cur.lastrowid
            log_move(conn, type_="out", comp=row, qty=qty, actor="我", borrower=borrower,
                     loan_id=loan_id, note=clean(payload.get("purpose"), 100), ts=taken_at)
            loan = conn.execute(LOAN_SELECT + " WHERE l.id = ?", (loan_id,)).fetchone()
            conn.commit()
            return loan_to_dict(loan)
        finally:
            conn.close()


def return_loan(lid: int, payload: dict) -> dict:
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            loan = conn.execute(LOAN_SELECT + " WHERE l.id = ?", (lid,)).fetchone()
            if not loan:
                raise ApiError(404, "借出记录不存在")
            remaining = loan["qty"] - loan["returned_qty"]
            if remaining <= 0:
                raise ApiError(400, "这笔已经还清了")
            qty = payload.get("qty")
            qty = remaining if qty in (None, "") else to_int(qty, "归还数量", 1)
            if qty > remaining:
                raise ApiError(400, f"最多只能归还 {remaining}")

            as_loss = clean(payload.get("mode"), 10) == "loss"
            reason = clean(payload.get("reason"), 40) or "损坏/丢失"
            returned_at = norm_dt(payload.get("returned_at"))
            new_returned = loan["returned_qty"] + qty
            finished = new_returned >= loan["qty"]

            if as_loss:
                conn.execute(
                    "UPDATE loans SET returned_qty = ?, lost_qty = lost_qty + ?, returned_at = ? WHERE id = ?",
                    (new_returned, qty, returned_at if finished else None, lid),
                )
            else:
                conn.execute(
                    "UPDATE loans SET returned_qty = ?, returned_at = ? WHERE id = ?",
                    (new_returned, returned_at if finished else None, lid),
                )
                conn.execute("UPDATE components SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                             (qty, now_str(), loan["component_id"]))

            comp = conn.execute(COMP_SELECT + " WHERE c.id = ?", (loan["component_id"],)).fetchone()
            note = clean(payload.get("note"), 200)
            if as_loss:
                log_move(conn, type_="loss", comp=comp, qty=qty, actor="我",
                         borrower=loan["borrower"], loan_id=lid, reason=reason,
                         note=note or f"{loan['borrower']} 归还时记为损耗", ts=returned_at)
            else:
                log_move(conn, type_="return", comp=comp, qty=qty, actor="我",
                         borrower=loan["borrower"], loan_id=lid, note=note, ts=returned_at)
            updated = conn.execute(LOAN_SELECT + " WHERE l.id = ?", (lid,)).fetchone()
            conn.commit()
            return loan_to_dict(updated)
        finally:
            conn.close()


def record_loss(cid: int, payload: dict) -> dict:
    """手动记一笔损耗：库存直接减掉，不进借还。"""
    qty = to_int(payload.get("qty"), "损耗数量", 1)
    reason = clean(payload.get("reason"), 40) or "其他"
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            comp = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
            if not comp:
                raise ApiError(404, "元件不存在")
            if qty > comp["quantity"]:
                raise ApiError(400, f"库存只有 {comp['quantity']} {comp['unit']}，不够记 {qty} 的损耗")
            conn.execute("UPDATE components SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
                         (qty, now_str(), cid))
            log_move(conn, type_="loss", comp=comp, qty=qty, actor="我", reason=reason,
                     note=clean(payload.get("note"), 200))
            updated = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
            conn.commit()
            return comp_to_dict(updated)
        finally:
            conn.close()


def loss_summary() -> dict:
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT component_id, component_name, component_code, qty, reason, ts, borrower, loan_id
               FROM movements WHERE type = 'loss' ORDER BY id DESC"""
        ).fetchall()
    finally:
        conn.close()

    month = today_str()[:7]
    total = sum(r["qty"] for r in rows)
    month_total = sum(r["qty"] for r in rows if (r["ts"] or "")[:7] == month)
    loan_total = sum(r["qty"] for r in rows if r["loan_id"])
    reasons, items = {}, {}

    for r in rows:
        reason = r["reason"] or "其他"
        reasons[reason] = reasons.get(reason, 0) + r["qty"]
        key = r["component_id"] or f"name:{r['component_name']}"
        item = items.get(key)
        if not item:
            item = {
                "component_id": r["component_id"], "name": r["component_name"],
                "code": r["component_code"], "qty": 0, "last_ts": r["ts"],
                "reasons": {},
            }
            items[key] = item
        if r["ts"] > item["last_ts"]:
            item["last_ts"] = r["ts"]
        item["qty"] += r["qty"]
        item["reasons"][reason] = item["reasons"].get(reason, 0) + r["qty"]

    result_items = sorted(items.values(), key=lambda x: -x["qty"])
    for item in result_items:
        item["reason_text"] = "、".join(f"{k}×{v}" for k, v in
                                        sorted(item["reasons"].items(), key=lambda kv: -kv[1]))
        item["reasons"] = [{"reason": k, "qty": v} for k, v in
                           sorted(item["reasons"].items(), key=lambda kv: -kv[1])]

    return {
        "total": total,
        "month": month_total,
        "components": len(result_items),
        "from_loans": loan_total,
        "manual": total - loan_total,
        "reasons": [{"reason": k, "qty": v} for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])],
        "items": result_items,
        "records": len(rows),
    }


# --------------------------------------------------------------------------
# 清单解析（批量入库）
# --------------------------------------------------------------------------
QTY_UNITS = "个|只|块|片|包|卷|米|套|根|盒|条|张|对|颗|支|台|件|粒|排|组|袋|瓶|筒|盘|本|把|副|双"
QTY_TOKEN_RE = re.compile(r"^[x×*]?\s*(\d+(?:\.\d+)?)\s*(" + QTY_UNITS + r")?$", re.I)
TAIL_QTY_RE = re.compile(
    r"(?:^|[\s:：])(?:[x×*]\s*)?(\d+(?:\.\d+)?)\s*(" + QTY_UNITS + r")?\s*$", re.I
)
LIST_MARK_RE = re.compile(r"^(?:\d+\s*[.、)）]|[-*•·])\s*")


def normalize_text(text: str) -> str:
    table = str.maketrans({"（": "(", "）": ")", "，": ",", "：": ":", "；": ";",
                           "、": ",", "　": " ", "［": "[", "］": "]", "ｘ": "x"})
    return text.translate(table)


def qty_token(token: str):
    m = QTY_TOKEN_RE.match(token.strip())
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    if value <= 0:
        return None
    unit = m.group(2) or ""
    return (int(value) if value == int(value) else value, unit)


def parse_line(raw: str):
    """把一行清单拆成 {name, spec, qty, unit}，拆不出来就返回 None。"""
    line = normalize_text(raw).strip()
    if not line or line.startswith("#"):
        return None
    line = LIST_MARK_RE.sub("", line).strip()
    if not line:
        return None
    # 表头、单据标题、分隔线、合计行：跳过
    if re.search(r"(清单|明细|明细表|列表|汇总|采购|入库|送货|收料|装箱|报价|订单|发票)\s*$",
                 line) and len(line) <= 24:
        return None
    if re.match(r"^(合计|总计|金额|税额|价税|应付|实付|日期|供应商|供方|需方|备注|序号|"
                r"制表|审核|经办|收货|地址|电话|开户|账号|税号|第\s*\d+\s*页)", line):
        return None
    if re.search(r"(数量|个数|数目|qty)\s*$", line, re.I) and not re.search(r"\d", line):
        return None
    if re.fullmatch(r"[-=_*~·\s]{3,}", line):
        return None

    name, spec, qty, unit = "", "", None, ""

    # 1) 制表符 / 竖线：名称  规格  数量
    if "\t" in line or "|" in line:
        parts = [p.strip() for p in re.split(r"[\t|]+", line) if p.strip()]
        if len(parts) >= 2:
            got = qty_token(parts[-1])
            if got:
                qty, unit = got
                name = parts[0]
                spec = " ".join(parts[1:-1])

    # 2) 逗号 / 分号：名称, 数量   或   名称, 规格, 数量
    if qty is None and ("," in line or ";" in line):
        parts = [p.strip() for p in re.split(r"[,;]+", line) if p.strip()]
        if 2 <= len(parts) <= 3:
            got = qty_token(parts[-1])
            if got:
                qty, unit = got
                name = parts[0]
                spec = parts[1] if len(parts) == 3 else ""

    # 3) 行尾数量：电阻 10k 0603 100 / 杜邦线 40根
    if qty is None:
        m = TAIL_QTY_RE.search(line)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                value = 0
            if value > 0:
                rest = line[: m.start()].strip(" \t,:：")
                if rest:
                    qty = int(value) if value == int(value) else value
                    unit = m.group(2) or ""
                    name = rest

    guessed = qty is None
    if not name:
        # 没写数量，或者整行就是一个型号（如 STM32F103C8T6）
        name = line.strip()
        qty = 1

    name = re.sub(r"\s{2,}", " ", name).strip(" \t,:：")
    spec = re.sub(r"\s{2,}", " ", spec).strip()
    return {"name": name, "spec": spec, "qty": qty, "unit": unit,
            "raw": raw.strip(), "guessed": guessed}


def norm_key(text: str) -> str:
    return re.sub(r"[\s\-_/()（）\[\]·,，.。]+", "", text or "").lower()


def match_component(name: str, spec: str, components: list):
    """在已有元件里找最像的一个。"""
    key = norm_key(name)
    if not key:
        return None, ""

    exact = [c for c in components if norm_key(c["name"]) == key]
    if spec:
        same_spec = [c for c in exact if norm_key(c["spec"]) == norm_key(spec)]
        if same_spec:
            return same_spec[0], "exact"
    if exact:
        return exact[0], "exact"

    def common_prefix(a: str, b: str) -> int:
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n

    def before_number(text: str) -> str:
        m = re.search(r"\d", text)
        return text[: m.start()] if m else text

    def first_number(text: str) -> str:
        m = re.search(r"\d+", text)
        return m.group(0) if m else ""

    best, best_score = None, 0.0
    for c in components:
        ck = norm_key(c["name"])
        if not ck:
            continue
        if key in ck or ck in key:
            score = 0.8 + min(len(key), len(ck)) / (max(len(key), len(ck)) + 10)
        else:
            from difflib import SequenceMatcher
            score = SequenceMatcher(None, key, ck).ratio() * 0.75

        # OCR 常把 Ω 认成 0、把「金」认成「全」，用「数字前的汉字 + 第一个数字」兜底
        pk, pck = before_number(key), before_number(ck)
        if (len(pk) >= 3 and len(pck) >= 3
                and first_number(key) and first_number(key) == first_number(ck)):
            from difflib import SequenceMatcher
            if SequenceMatcher(None, pk, pck).ratio() >= 0.75:
                score = max(score, 0.9)

        # 公共前缀兜底（型号后半截多认了几个字的情况）
        prefix = common_prefix(key, ck)
        if prefix >= 5 and prefix / min(len(key), len(ck)) >= 0.7:
            score = max(score, 0.85)

        if score > best_score:
            best, best_score = c, score
    if best and best_score >= 0.62:
        return best, "fuzzy"
    return None, ""


def parse_list(text: str) -> dict:
    raw_lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    if not raw_lines:
        raise ApiError(400, "请先粘贴或输入清单")
    if len(raw_lines) > 300:
        raise ApiError(400, "一次最多解析 300 行")

    conn = get_db()
    try:
        components = fetch_components(conn)
    finally:
        conn.close()

    items, merged = [], 0
    seen = {}
    for raw in raw_lines:
        parsed = parse_line(raw)
        if not parsed:
            continue
        matched, confidence = match_component(parsed["name"], parsed["spec"], components)
        row = {
            "name": parsed["name"],
            "spec": parsed["spec"],
            "qty": parsed["qty"],
            "unit": parsed["unit"] or (matched["unit"] if matched else "个"),
            "raw": parsed["raw"],
            "match_id": matched["id"] if matched else None,
            "match_name": matched["name"] if matched else "",
            "match_stock": matched["quantity"] if matched else 0,
            "confidence": confidence,
            "qty_guessed": parsed["guessed"],
        }
        key = (norm_key(row["name"]), row["match_id"])
        if key in seen:
            existing = seen[key]
            existing["qty"] += row["qty"]
            merged += 1
            continue
        seen[key] = row
        items.append(row)

    return {"items": items, "merged": merged, "lines": len(raw_lines)}


# --------------------------------------------------------------------------
# 图片识别：调用 Windows 自带 OCR（离线，不联网）
# --------------------------------------------------------------------------
OCR_SUFFIX = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/gif": ".gif",
    "image/tiff": ".tif",
}


def words_to_lines(words: list) -> list:
    """把 OCR 的单词按坐标拼回行：中文之间不留空格，列之间留制表符。"""
    if not words:
        return []
    heights = sorted(w["h"] for w in words if w.get("h", 0) > 0) or [20]
    unit = heights[len(heights) // 2] or 20

    rows = []
    for w in sorted(words, key=lambda x: (x["y"] + x["h"] / 2, x["x"])):
        cy = w["y"] + w["h"] / 2
        if rows and abs(cy - rows[-1]["cy"]) <= 0.55 * unit:
            rows[-1]["words"].append(w)
            ws = rows[-1]["words"]
            rows[-1]["cy"] = sum(x["y"] + x["h"] / 2 for x in ws) / len(ws)
        else:
            rows.append({"cy": cy, "words": [w]})

    lines = []
    for row in rows:
        ws = sorted(row["words"], key=lambda x: x["x"])
        text, prev_end = "", None
        for w in ws:
            token = (w.get("t") or "").strip()
            if not token:
                continue
            if not text:
                text = token
            else:
                gap = w["x"] - prev_end
                if gap > 3.2 * unit:
                    text += "\t" + token
                elif gap > 0.38 * max(w["h"], 1):
                    text += " " + token
                else:
                    text += token
            prev_end = w["x"] + w["w"]
        text = text.strip()
        if text:
            lines.append(text)
    return lines


def ocr_image(data: bytes, suffix: str) -> dict:
    if os.name != "nt":
        raise ApiError(501, "图片识别目前只在 Windows 上可用")
    if not OCR_SCRIPT.exists():
        raise ApiError(501, "缺少 ocr.ps1，没法识别图片")

    tmpdir = Path(tempfile.mkdtemp(prefix="yj-ocr-"))
    try:
        raw = tmpdir / ("input" + (suffix or ".png"))
        raw.write_bytes(data)

        target = raw
        if Image is not None:
            try:
                im = Image.open(raw)
                im = ImageOps.exif_transpose(im)
                im = im.convert("RGB")
                if max(im.size) > 2400:
                    im.thumbnail((2400, 2400), Image.LANCZOS)
                target = tmpdir / "clean.png"
                im.save(target)
            except Exception:
                target = raw

        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(OCR_SCRIPT), "-Path", str(target)]
        proc = subprocess.run(
            cmd, capture_output=True, timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        out = proc.stdout.decode("utf-8", "replace").strip()
        if not out:
            err = proc.stderr.decode("utf-8", "replace").strip()[:300]
            raise ApiError(500, "识别失败" + (f"：{err}" if err else "，换张清晰点的图片试试"))

        payload = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
        if payload is None:
            raise ApiError(500, "识别服务返回了看不懂的内容")
        if not payload.get("ok"):
            raise ApiError(501, "系统没有可用的中文 OCR 引擎")

        lines = words_to_lines(payload.get("words") or [])
        if not lines:
            raise ApiError(422, "没识别出文字，换一张更清晰、正着的图片试试")

        return {"text": "\n".join(lines), "lines": lines,
                "engine": payload.get("engine", ""), "count": len(lines)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Excel / Word 导入（xlsx / docx 都是 zip + xml，标准库直接拆）
# --------------------------------------------------------------------------
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _col_index(ref: str) -> int:
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n


def _trim_number(value: str) -> str:
    text = (value or "").strip()
    if re.fullmatch(r"-?\d+\.0+", text):
        return text.split(".")[0]
    return text


def parse_xlsx(data: bytes) -> list:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ApiError(400, "这个文件不是正常的 .xlsx")

    with zf:
        names = zf.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{XLSX_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{XLSX_NS}t")))

        sheets = [n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
        if not sheets:
            raise ApiError(400, "Excel 里没有工作表")
        sheets.sort(key=lambda p: int(re.search(r"(\d+)", p).group(1)))
        root = ET.fromstring(zf.read(sheets[0]))

    rows = []
    for row in root.iter(f"{XLSX_NS}row"):
        cells = {}
        for cell in row.findall(f"{XLSX_NS}c"):
            idx = _col_index(cell.get("r") or "")
            ctype = cell.get("t")
            v = cell.find(f"{XLSX_NS}v")
            inline = cell.find(f"{XLSX_NS}is")
            value = ""
            if ctype == "s" and v is not None and v.text is not None:
                try:
                    value = shared[int(v.text)]
                except (ValueError, IndexError):
                    value = ""
            elif ctype == "inlineStr" and inline is not None:
                value = "".join(t.text or "" for t in inline.iter(f"{XLSX_NS}t"))
            elif v is not None and v.text is not None:
                value = v.text
            value = _trim_number(str(value))
            if value:
                cells[idx] = value
        if cells:
            rows.append(cells)

    if not rows:
        return []

    # 前几行里找表头：有「数量」就当表头，按列名取数
    header_idx, header = None, None
    for i, row in enumerate(rows[:5]):
        if re.search(r"(数量|个数|数目|件数)", " ".join(row.values())):
            header_idx, header = i, row
            break

    lines = []
    if header is not None:
        qty_col = name_col = spec_col = unit_col = None
        for idx, text in header.items():
            if re.search(r"(数量|个数|数目|件数)", text):
                qty_col = idx
            elif re.search(r"(名称|品名|物料|元件|器件|物品|货品)", text):
                name_col = idx
            elif re.search(r"(规格|型号|封装|参数)", text):
                spec_col = idx
            elif re.search(r"单位", text):
                unit_col = idx
        if qty_col and name_col:
            for row in rows[header_idx + 1:]:
                name = (row.get(name_col) or "").strip()
                qty = (row.get(qty_col) or "").strip()
                if not name or not qty:
                    continue
                spec = (row.get(spec_col) or "").strip() if spec_col else ""
                unit = (row.get(unit_col) or "").strip() if unit_col else ""
                if unit and re.fullmatch(r"\d+(\.\d+)?", qty):
                    qty += unit
                lines.append("\t".join([name] + ([spec] if spec else []) + [qty]))
            return lines

    # 没表头：按列顺序拼成「名称 规格 数量」
    for row in rows:
        ordered = [row[k] for k in sorted(row) if row[k]]
        if ordered:
            lines.append("\t".join(ordered))
    return lines


def parse_docx(data: bytes) -> list:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ApiError(400, "这个文件不是正常的 .docx")

    with zf:
        if "word/document.xml" not in zf.namelist():
            raise ApiError(400, "Word 文件里没有正文")
        root = ET.fromstring(zf.read("word/document.xml"))

    body = root.find(f"{DOCX_NS}body")
    if body is None:
        return []

    lines = []
    for child in list(body):
        if child.tag == f"{DOCX_NS}p":
            text = "".join(t.text or "" for t in child.iter(f"{DOCX_NS}t")).strip()
            if text:
                lines.append(text)
        elif child.tag == f"{DOCX_NS}tbl":
            for tr in child.findall(f"{DOCX_NS}tr"):
                cells = []
                for tc in tr.findall(f"{DOCX_NS}tc"):
                    cells.append("".join(t.text or "" for t in tc.iter(f"{DOCX_NS}t")).strip())
                while cells and not cells[-1]:
                    cells.pop()
                if cells and any(cells):
                    lines.append("\t".join(cells))
    return lines


def extract_document(data: bytes, kind: str) -> dict:
    lines = parse_xlsx(data) if kind == "excel" else parse_docx(data)
    if not lines:
        raise ApiError(422, "文件里没读到内容，检查一下是不是空表")
    return {"text": "\n".join(lines), "count": len(lines), "kind": kind}


def create_batch(payload: dict) -> dict:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError(400, "没有可入库的条目")
    if len(items) > 300:
        raise ApiError(400, "一次最多入库 300 条")

    name = clean(payload.get("name"), 60) or f"{today_str()} 入库"

    created, merged, total_qty = 0, 0, 0
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            location_id = resolve_lookup(conn, "locations", payload.get("location_id"), "位置")
            room_id = resolve_lookup(conn, "rooms", payload.get("room_id"), "物理储存")
            category_id = resolve_lookup(conn, "categories", payload.get("category_id"), "分类")
            cur = conn.execute(
                "INSERT INTO batches (name, note, location_id, room_id, created_at) VALUES (?,?,?,?,?)",
                (name, clean(payload.get("note"), 200), location_id, room_id, now_str()),
            )
            batch_id = cur.lastrowid

            for item in items:
                qty = to_int(item.get("qty"), "数量", 1)
                cid = item.get("component_id")
                cid = to_int(cid, "元件", 1) if cid not in (None, "", 0) else None

                if cid:
                    row = conn.execute(COMP_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
                    if not row:
                        raise ApiError(404, "列表里有元件已经不存在了")
                    conn.execute("UPDATE components SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
                                 (qty, now_str(), cid))
                    log_move(conn, type_="in", comp=row, qty=qty, actor="我", batch_id=batch_id)
                    merged += 1
                else:
                    comp = create_component(
                        {
                            "name": clean(item.get("name"), 80),
                            "spec": clean(item.get("spec"), 120),
                            "unit": clean(item.get("unit"), 10) or "个",
                            "quantity": qty,
                            "location_id": location_id,
                            "room_id": room_id,
                            "category_id": category_id,
                        },
                        conn=conn, batch_id=batch_id,
                    )
                    created += 1
                total_qty += qty

            conn.commit()
            return {"id": batch_id, "name": name, "created": created, "merged": merged,
                    "total_qty": total_qty, "location_id": location_id, "room_id": room_id}
        finally:
            conn.close()


def fetch_batches(conn, limit: int = 60) -> list:
    rows = conn.execute(
        """SELECT b.*, l.name AS location_name, r.name AS room_name,
                  (SELECT COUNT(*) FROM movements m
                   WHERE m.batch_id = b.id AND m.type <> 'revert') AS item_count,
                  (SELECT COALESCE(SUM(m.qty),0) FROM movements m
                   WHERE m.batch_id = b.id AND m.type <> 'revert') AS total_qty
           FROM batches b
           LEFT JOIN locations l ON l.id = b.location_id
           LEFT JOIN rooms r ON r.id = b.room_id
           ORDER BY b.id DESC LIMIT ?""", (limit,)
    ).fetchall()
    return [{
        "id": r["id"], "name": r["name"], "note": r["note"],
        "location": r["location_name"] or "", "room": r["room_name"] or "",
        "created_at": r["created_at"], "item_count": r["item_count"], "total_qty": r["total_qty"],
        "reverted": bool(r["reverted"]), "reverted_at": r["reverted_at"] or "",
    } for r in rows]


def fetch_batch_detail(conn, bid: int) -> dict:
    row = conn.execute(
        """SELECT b.*, l.name AS location_name, r.name AS room_name FROM batches b
           LEFT JOIN locations l ON l.id = b.location_id
           LEFT JOIN rooms r ON r.id = b.room_id WHERE b.id = ?""", (bid,)
    ).fetchone()
    if not row:
        raise ApiError(404, "入库单不存在")
    moves = conn.execute(
        "SELECT * FROM movements WHERE batch_id = ? AND type <> 'revert' ORDER BY id", (bid,)
    ).fetchall()
    return {
        "id": row["id"], "name": row["name"], "note": row["note"],
        "location": row["location_name"] or "", "room": row["room_name"] or "",
        "created_at": row["created_at"],
        "reverted": bool(row["reverted"]), "reverted_at": row["reverted_at"] or "",
        "items": [{"name": m["component_name"], "code": m["component_code"],
                   "qty": m["qty"], "type": m["type"], "note": m["note"],
                   "reverted": bool(m["reverted"])} for m in moves],
    }


def revert_batch(bid: int) -> dict:
    """撤回一整张入库单：加进来的数量原样减回去。"""
    with DB_LOCK:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute("SELECT * FROM batches WHERE id = ?", (bid,)).fetchone()
            if not batch:
                raise ApiError(404, "入库单不存在")
            if batch["reverted"]:
                raise ApiError(409, "这单已经撤回过了")

            moves = conn.execute(
                """SELECT * FROM movements
                   WHERE batch_id = ? AND type IN ('new', 'in') AND reverted = 0
                   ORDER BY id DESC""", (bid,)
            ).fetchall()
            if not moves:
                raise ApiError(400, "这单没有可撤回的内容")

            plan = []
            for m in moves:
                comp = conn.execute("SELECT * FROM components WHERE id = ?", (m["component_id"],)).fetchone()
                if not comp:
                    continue
                qty = abs(m["qty"])
                if comp["deleted"]:
                    # 元件已经被手动删了，不动库存，只把记录标掉
                    plan.append(("skip", m, comp, 0))
                    continue
                loan_count = conn.execute(
                    "SELECT COUNT(*) FROM loans WHERE component_id = ?", (comp["id"],)
                ).fetchone()[0]
                untouched = (comp["quantity"] == qty and loan_count == 0
                             and comp["updated_at"] == comp["created_at"])
                if m["type"] == "new" and untouched:
                    plan.append(("delete", m, comp, qty))
                else:
                    if comp["quantity"] - qty < 0:
                        raise ApiError(409, f"「{comp['name']}」现在只剩 {comp['quantity']}，"
                                            f"撤回要减 {qty}，不够了")
                    plan.append(("adjust", m, comp, qty))

            adjusted = deleted = 0
            for action, m, comp, qty in plan:
                if action == "delete":
                    conn.execute("UPDATE components SET deleted = 1, updated_at = ? WHERE id = ?",
                                 (now_str(), comp["id"]))
                    deleted += 1
                elif action == "skip":
                    conn.execute("UPDATE movements SET reverted = 1 WHERE id = ?", (m["id"],))
                    continue
                else:
                    conn.execute("UPDATE components SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
                                 (qty, now_str(), comp["id"]))
                    adjusted += 1
                conn.execute("UPDATE movements SET reverted = 1 WHERE id = ?", (m["id"],))
                log_move(conn, type_="revert", comp=comp, qty=-qty, actor="我", batch_id=bid,
                         note=f"撤回入库单：{batch['name']}")

            conn.execute("UPDATE batches SET reverted = 1, reverted_at = ? WHERE id = ?",
                         (now_str(), bid))
            conn.commit()
            return {"ok": True, "adjusted": adjusted, "deleted": deleted}
        finally:
            conn.close()


# --------------------------------------------------------------------------
# 数据清理
# --------------------------------------------------------------------------
def _db_size() -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


def cleanup_stats() -> dict:
    conn = get_db()
    try:
        components = conn.execute("SELECT COUNT(*) FROM components WHERE deleted = 0").fetchone()[0]
        deleted_components = conn.execute("SELECT COUNT(*) FROM components WHERE deleted = 1").fetchone()[0]
        purgable = conn.execute(
            """SELECT COUNT(*) FROM components c WHERE c.deleted = 1
               AND NOT EXISTS (SELECT 1 FROM loans l WHERE l.component_id = c.id)"""
        ).fetchone()[0]
        movements = conn.execute("SELECT COUNT(*) FROM movements").fetchone()[0]
        batches = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        reverted_batches = conn.execute("SELECT COUNT(*) FROM batches WHERE reverted = 1").fetchone()[0]
        loans = conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0]
    finally:
        conn.close()

    backups = sorted(BACKUP_DIR.glob("components_*.db")) if BACKUP_DIR.exists() else []
    return {
        "components": components,
        "deleted_components": deleted_components,
        "purgable_components": purgable,
        "movements": movements,
        "batches": batches,
        "reverted_batches": reverted_batches,
        "loans": loans,
        "db_bytes": _db_size(),
        "backup_count": len(backups),
        "backup_bytes": sum(f.stat().st_size for f in backups),
        "backup_dir": str(BACKUP_DIR),
        "db_path": str(DB_PATH),
    }


def auto_backup(tag: str) -> str:
    BACKUP_DIR.mkdir(exist_ok=True)
    dst = BACKUP_DIR / f"components_{tag}_{datetime.now():%Y%m%d_%H%M%S}.db"
    src = sqlite3.connect(DB_PATH)
    out = sqlite3.connect(dst)
    try:
        with out:
            src.backup(out)
    finally:
        src.close()
        out.close()
    return str(dst)


def cleanup_action(payload: dict) -> dict:
    action = clean(payload.get("action"), 30)

    if action == "vacuum":
        with DB_LOCK:
            conn = sqlite3.connect(DB_PATH, isolation_level=None)
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()
        return {"ok": True, "db_bytes": _db_size(), "message": "数据库已压缩"}

    with DB_LOCK:
        conn = get_db()
        try:
            if action == "purge_deleted":
                rows = conn.execute(
                    """SELECT id FROM components c WHERE c.deleted = 1
                       AND NOT EXISTS (SELECT 1 FROM loans l WHERE l.component_id = c.id)"""
                ).fetchall()
                ids = [r["id"] for r in rows]
                for cid in ids:
                    conn.execute("DELETE FROM components WHERE id = ?", (cid,))
                conn.commit()
                skipped = conn.execute("SELECT COUNT(*) FROM components WHERE deleted = 1").fetchone()[0]
                return {"ok": True, "removed": len(ids), "skipped": skipped,
                        "message": f"清掉 {len(ids)} 个已删除的元件"}

            if action == "purge_reverted":
                ids = [r["id"] for r in conn.execute("SELECT id FROM batches WHERE reverted = 1").fetchall()]
                moves = 0
                for bid in ids:
                    moves += conn.execute("SELECT COUNT(*) FROM movements WHERE batch_id = ?", (bid,)).fetchone()[0]
                    conn.execute("DELETE FROM movements WHERE batch_id = ?", (bid,))
                    conn.execute("DELETE FROM batches WHERE id = ?", (bid,))
                conn.commit()
                return {"ok": True, "batches": len(ids), "movements": moves,
                        "message": f"清掉 {len(ids)} 张已撤回的入库单、{moves} 条流水"}

            if action == "purge_movements":
                days = max(to_int(payload.get("days", 180), "保留天数", 1), 1)
                cutoff = (date.today() - timedelta(days=days)).isoformat() + " 00:00:00"
                count = conn.execute("SELECT COUNT(*) FROM movements WHERE ts < ?", (cutoff,)).fetchone()[0]
                conn.execute("DELETE FROM movements WHERE ts < ?", (cutoff,))
                conn.commit()
                return {"ok": True, "removed": count, "cutoff": cutoff,
                        "message": f"清掉 {count} 条 {cutoff[:10]} 之前的流水"}

            if action == "purge_backups":
                keep = max(to_int(payload.get("keep", 5), "保留个数", 1), 1)
                files = sorted(BACKUP_DIR.glob("components_*.db")) if BACKUP_DIR.exists() else []
                removed, freed = 0, 0
                for f in files[:-keep] if len(files) > keep else []:
                    freed += f.stat().st_size
                    f.unlink()
                    removed += 1
                return {"ok": True, "removed": removed, "freed": freed,
                        "message": f"清掉 {removed} 个旧备份，释放 {freed // 1024} KB"}

            if action == "purge_temp":
                removed = 0
                for p in BASE_DIR.rglob("__pycache__"):
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
                for p in Path(tempfile.gettempdir()).glob("yj-ocr-*"):
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
                return {"ok": True, "removed": removed, "message": f"清掉 {removed} 个临时目录"}

            if action in ("reset_data", "reset_all"):
                if clean(payload.get("confirm"), 20) != "清空":
                    raise ApiError(400, "请输入「清空」两个字确认")
                backup = auto_backup("before-reset")
                conn.execute("DELETE FROM movements")
                conn.execute("DELETE FROM loans")
                conn.execute("DELETE FROM batches")
                conn.execute("DELETE FROM components")
                if action == "reset_all":
                    conn.execute("DELETE FROM locations")
                    conn.execute("DELETE FROM categories")
                    conn.execute("DELETE FROM rooms")
                    conn.execute("DELETE FROM sqlite_sequence")
                else:
                    conn.execute(
                        "DELETE FROM sqlite_sequence WHERE name IN ('movements','loans','batches','components')"
                    )
                conn.commit()
                return {"ok": True, "backup": backup,
                        "message": "已清空，清空前的数据备份在 " + Path(backup).name}

            raise ApiError(400, "未知的清理动作")
        finally:
            conn.close()


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------
def export_csv(kind: str) -> bytes:
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    conn = get_db()
    try:
        if kind == "components":
            writer.writerow(["编号", "名称", "型号/规格", "分类", "物理储存", "存放位置", "单位",
                             "在库数量", "外借数量", "累计损耗", "安全库存", "备注"])
            for c in fetch_components(conn):
                writer.writerow([c["code"], c["name"], c["spec"], c["category"], c["room"],
                                 c["location"], c["unit"], c["quantity"], c["out_qty"],
                                 c["lost"], c["min_stock"], c["note"]])
        elif kind == "loans":
            writer.writerow(["记录号", "元件", "编号", "借用人", "借出数量", "已还", "未还",
                             "借出时间", "应还日期", "归还时间", "用途", "备注"])
            rows = conn.execute(LOAN_SELECT + " ORDER BY l.id DESC").fetchall()
            for r in rows:
                d = loan_to_dict(r)
                writer.writerow([d["id"], d["component_name"], d["component_code"], d["borrower"],
                                 d["qty"], d["returned_qty"], d["remaining"], d["taken_at"],
                                 d["due_date"], d["returned_at"], d["purpose"], d["note"]])
        elif kind == "movements":
            writer.writerow(["时间", "类型", "元件", "编号", "数量", "原因", "经手人", "借用人", "备注"])
            rows = conn.execute("SELECT * FROM movements ORDER BY ts DESC, id DESC").fetchall()
            labels = {"new": "新增", "in": "入库", "out": "借出", "return": "归还",
                      "adjust": "盘点", "loss": "损耗", "delete": "删除", "revert": "撤回"}
            for r in rows:
                writer.writerow([r["ts"], labels.get(r["type"], r["type"]), r["component_name"],
                                 r["component_code"], r["qty"], r["reason"] or "",
                                 r["actor"], r["borrower"], r["note"]])
        else:
            raise ApiError(404, "未知的导出类型")
    finally:
        conn.close()
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "ComponentLedge/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if args and str(args[0]).startswith(("POST", "PATCH", "DELETE")):
            sys.stdout.write(f"  {args[0]}\n")

    def _send(self, body: bytes, status: int = 200, ctype: str = "application/json; charset=utf-8",
              headers: dict = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            raise ApiError(400, "请求内容不是合法的 JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "请求内容格式错误")
        return data

    def _raw(self, limit: int = 20 * 1024 * 1024) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(400, "没有收到图片")
        if length > limit:
            raise ApiError(413, "图片太大了（最大 20MB）")
        return self.rfile.read(length)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self, method: str):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            if path.startswith("/api/"):
                return self._api(method, path, query)
            if path.startswith("/export/"):
                return self._export(path)
            return self._static(path)
        except ApiError as exc:
            self._json({"error": exc.message}, exc.status)
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            try:
                self._json({"error": "服务器内部错误，请查看启动窗口"}, 500)
            except Exception:
                pass

    def _api(self, method: str, path: str, query: dict):
        if method == "GET" and path == "/api/ping":
            return self._json({"app": "component-ledger", "version": 2, "ok": True})

        if method == "GET" and path == "/api/bootstrap":
            conn = get_db()
            try:
                comps = fetch_components(conn)
                loans = fetch_open_loans(conn)
                locations = fetch_locations(conn)
                categories = fetch_categories(conn)
                rooms = fetch_rooms(conn)
                stats = compute_stats(comps, loans)
                stats["locations"] = len(locations)
                stats["categories"] = len(categories)
                stats["rooms"] = len(rooms)
                return self._json({
                    "components": comps, "loans": loans,
                    "locations": locations, "categories": categories, "rooms": rooms,
                    "stats": stats, "today": today_str(),
                    "meta": {"data_dir": str(DATA_DIR), "db_path": str(DB_PATH), "version": 2},
                })
            finally:
                conn.close()

        if method == "GET" and path == "/api/components":
            conn = get_db()
            try:
                return self._json({"components": fetch_components(conn)})
            finally:
                conn.close()

        if method == "POST" and path == "/api/components":
            return self._json(create_component(self._body()), 201)

        m = re.fullmatch(r"/api/components/(\d+)", path)
        if m and method == "PATCH":
            return self._json(update_component(int(m.group(1)), self._body()))
        if m and method == "DELETE":
            return self._json(delete_component(int(m.group(1))))

        m = re.fullmatch(r"/api/components/(\d+)/stock", path)
        if m and method == "POST":
            return self._json(stock_in(int(m.group(1)), self._body()))

        m = re.fullmatch(r"/api/components/(\d+)/adjust", path)
        if m and method == "POST":
            return self._json(adjust_stock(int(m.group(1)), self._body()))

        m = re.fullmatch(r"/api/components/(\d+)/loss", path)
        if m and method == "POST":
            return self._json(record_loss(int(m.group(1)), self._body()))

        if method == "GET" and path == "/api/losses":
            return self._json(loss_summary())

        m = re.fullmatch(r"/api/(locations|categories|rooms)", path)
        if m:
            table = m.group(1)
            if method == "GET":
                conn = get_db()
                try:
                    return self._json({table: fetch_lookup(conn, table)})
                finally:
                    conn.close()
            if method == "POST":
                return self._json(create_lookup(table, self._body()), 201)

        m = re.fullmatch(r"/api/(locations|categories|rooms)/(\d+)", path)
        if m:
            table, lid = m.group(1), int(m.group(2))
            if method == "PATCH":
                return self._json(rename_lookup(table, lid, self._body()))
            if method == "DELETE":
                return self._json(delete_lookup(table, lid))

        if method == "GET" and path == "/api/loans":
            status = (query.get("status") or ["open"])[0]
            conn = get_db()
            try:
                if status == "open":
                    return self._json({"loans": fetch_open_loans(conn)})
                rows = conn.execute(LOAN_SELECT + " ORDER BY l.id DESC LIMIT 300").fetchall()
                return self._json({"loans": [loan_to_dict(r) for r in rows]})
            finally:
                conn.close()

        if method == "POST" and path == "/api/loans":
            return self._json(create_loan(self._body()), 201)

        m = re.fullmatch(r"/api/loans/(\d+)/return", path)
        if m and method == "POST":
            return self._json(return_loan(int(m.group(1)), self._body()))

        if method == "GET" and path == "/api/movements":
            try:
                limit = min(max(int((query.get("limit") or ["300"])[0]), 1), 2000)
            except ValueError:
                limit = 300
            conn = get_db()
            try:
                rows = conn.execute(
                    """SELECT m.*, b.name AS batch_name FROM movements m
                       LEFT JOIN batches b ON b.id = m.batch_id
                       ORDER BY m.ts DESC, m.id DESC LIMIT ?""", (limit,)
                ).fetchall()
                return self._json({"movements": [{
                    "id": r["id"], "ts": r["ts"], "type": r["type"],
                    "component_name": r["component_name"], "component_code": r["component_code"],
                    "qty": r["qty"], "actor": r["actor"], "borrower": r["borrower"],
                    "loan_id": r["loan_id"], "batch_id": r["batch_id"],
                    "batch_name": r["batch_name"] or "", "note": r["note"],
                    "reason": r["reason"] or "", "reverted": bool(r["reverted"]),
                } for r in rows]})
            finally:
                conn.close()

        if method == "GET" and path == "/api/cleanup":
            return self._json(cleanup_stats())

        if method == "POST" and path == "/api/cleanup":
            return self._json(cleanup_action(self._body()))

        if method == "POST" and path == "/api/ocr":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            suffix = OCR_SUFFIX.get(ctype)
            if not suffix:
                raise ApiError(400, "只支持 JPG / PNG / WEBP / BMP 图片")
            return self._json(ocr_image(self._raw(), suffix))

        if method == "POST" and path == "/api/import":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            filename = urllib.parse.unquote(self.headers.get("X-File-Name") or "")
            ext = Path(filename).suffix.lower()
            if ext == ".xlsx" or ctype.endswith("spreadsheetml.sheet"):
                kind = "excel"
            elif ext == ".docx" or ctype.endswith("wordprocessingml.document"):
                kind = "word"
            elif ext in (".xls", ".doc"):
                raise ApiError(400, "老版 .xls / .doc 不支持，请先另存为 .xlsx / .docx")
            else:
                raise ApiError(400, "只支持 .xlsx / .docx 文件")
            return self._json(extract_document(self._raw(), kind))

        m = re.fullmatch(r"/api/components/(\d+)/revert-last", path)
        if m and method == "POST":
            return self._json(revert_component_last(int(m.group(1))))

        m = re.fullmatch(r"/api/batches/(\d+)/revert", path)
        if m and method == "POST":
            return self._json(revert_batch(int(m.group(1))))

        if method == "POST" and path == "/api/batches/parse":
            return self._json(parse_list(self._body().get("text", "")))

        if method == "GET" and path == "/api/batches":
            conn = get_db()
            try:
                return self._json({"batches": fetch_batches(conn)})
            finally:
                conn.close()

        if method == "POST" and path == "/api/batches":
            return self._json(create_batch(self._body()), 201)

        if method == "GET":
            m = re.fullmatch(r"/api/batches/(\d+)", path)
            if m:
                conn = get_db()
                try:
                    return self._json(fetch_batch_detail(conn, int(m.group(1))))
                finally:
                    conn.close()

        raise ApiError(404, "接口不存在")

    def _export(self, path: str):
        m = re.fullmatch(r"/export/(components|loans|movements)\.csv", path)
        if not m:
            raise ApiError(404, "文件不存在")
        kind = m.group(1)
        names = {"components": "元件库存", "loans": "借还记录", "movements": "操作流水"}
        filename = f"{names[kind]}_{datetime.now():%Y%m%d_%H%M}.csv"
        body = export_csv(kind)
        self._send(body, ctype="text/csv; charset=utf-8",
                   headers={"Content-Disposition":
                            f"attachment; filename=\"export.csv\"; filename*=UTF-8''{urllib.parse.quote(filename)}"})

    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            raise ApiError(404, "文件不存在")
        if not target.is_file():
            raise ApiError(404, "文件不存在")
        ctype = STATIC_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(target.read_bytes(), ctype=ctype)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def lan_ips() -> list:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ips)


def open_browser(url: str) -> bool:
    """尽量可靠地打开默认浏览器：Windows 先走 ShellExecute，再退回 webbrowser。"""
    if os.environ.get("COMPONENT_LEDGER_NO_BROWSER"):
        return False
    if hasattr(os, "startfile"):
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except Exception:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def find_running(base_port: int) -> int:
    """看看 8787 起的前 10 个端口里，是不是已经有一个自己在跑。"""
    import urllib.request
    for offset in range(10):
        port = base_port + offset
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.4) as res:
                data = json.loads(res.read().decode("utf-8"))
                if data.get("app") == "component-ledger":
                    return port
        except Exception:
            continue
    return 0


def main():
    parser = argparse.ArgumentParser(description="元件库（本地单机版）")
    parser.add_argument("--port", type=int, default=0, help="默认读取 config.json，缺失则 8787")
    parser.add_argument("--host", default="", help="默认 127.0.0.1（仅本机）；0.0.0.0 可供局域网访问")
    parser.add_argument("--data-dir", default="", help="数据目录，默认 data/")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--force", action="store_true", help="端口上已有实例时，仍然另起一个")
    args = parser.parse_args()

    apply_paths(args.data_dir)

    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("元件库 · 本地台账")
        except Exception:
            pass

    base_port = args.port or int(CONFIG.get("port") or 8787)

    # 已经开着的话，不再起第二个，直接把浏览器指过去
    if not args.force and not args.data_dir:
        running = find_running(base_port)
        if running:
            url = f"http://127.0.0.1:{running}"
            print("=" * 62)
            print("  元件库已经在运行了，这次就不重复启动")
            print(f"  正在打开：{url}")
            print("  浏览器没反应的话，手动复制这个网址")
            print("=" * 62)
            sys.stdout.flush()
            open_browser(url)
            sys.exit(3)   # 3 = 已经在运行，只是把浏览器叫过来

    init_db()

    host = args.host or CONFIG.get("host") or "127.0.0.1"
    httpd = None
    port = base_port
    for offset in range(10):
        try:
            httpd = Server((host, port + offset), Handler)
            port += offset
            break
        except OSError:
            continue
    if httpd is None:
        print("端口都被占用了，请用 --port 指定其他端口")
        sys.exit(1)

    local_url = f"http://127.0.0.1:{port}"
    print("=" * 62)
    print("  元件库 已启动（本地单机版）")
    print("=" * 62)
    print(f"  打开网址   {local_url}")
    if host not in ("127.0.0.1", "localhost"):
        for ip in lan_ips():
            print(f"  局域网     http://{ip}:{port}")
    print(f"  数据文件   {DB_PATH}")
    print(f"  备份方法   复制整个数据目录：{DATA_DIR}")
    print("-" * 62)
    print("  关闭这个窗口即停止；数据不会丢。")
    print("  想换存放位置：改 config.json 里的 data_dir，或用 --data-dir 指定")
    print("=" * 62)
    sys.stdout.flush()

    if args.open:
        def _launch():
            if open_browser(local_url):
                print(f"  已打开浏览器：{local_url}")
            else:
                print(f"  （没能自动打开浏览器，请手动访问 {local_url}）")
            sys.stdout.flush()
        print("  正在打开浏览器…")
        sys.stdout.flush()
        threading.Timer(1.2, _launch).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
