"""
会话持久化 — SQLite FTS5 全文搜索
等价于 Hermes 的 session_search 能力

精简自 Hermes hermes_state.py 的 SessionDB 模式。
"""

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass, field


@dataclass
class SessionMessage:
    id: int
    role: str        # user | assistant | tool
    content: str
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class Session:
    id: str
    title: str = ""
    created_at: float = 0.0
    last_active: float = 0.0
    message_count: int = 0


class SessionStore:
    """
    SQLite FTS5 会话存储

    功能:
    - 会话创建/追加消息
    - FTS5 全文搜索（跨会话）
    - 上下文窗口获取
    - 会话压缩（保留最近 N 条）
    """

    def __init__(self, db_path: str = "data/sessions.db", backup_dir: str = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self.backup_dir = Path(backup_dir) if backup_dir else self.db_path.parent / "backups"
        self._backup_in_progress = False

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def init_db(self):
        """初始化数据库表（首次使用）"""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                created_at REAL,
                last_active REAL,
                message_count INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL,
                metadata TEXT DEFAULT '{}',
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(session_id, role, content, content=messages, content_rowid=id);
        """)
        self.conn.commit()

    # ─── 写入 ───

    def create_session(self, session_id: str, title: str = "") -> Session:
        """创建新会话"""
        now = time.time()
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions (id, title, created_at, last_active) VALUES (?, ?, ?, ?)",
            (session_id, title, now, now),
        )
        self.conn.commit()
        return Session(id=session_id, title=title, created_at=now, last_active=now)

    def append_message(self, session_id: str, role: str, content: str,
                       metadata: dict = None) -> int:
        """追加消息到会话"""
        now = time.time()
        cursor = self.conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, now, json.dumps(metadata or {})),
        )

        # 更新会话最后活跃时间
        self.conn.execute(
            "UPDATE sessions SET last_active = ?, message_count = message_count + 1 WHERE id = ?",
            (now, session_id),
        )
        self.conn.commit()
        return cursor.lastrowid

    def append_exchange(self, session_id: str, user_msg: str, assistant_msg: str):
        """追加一轮对话（用户+助手）"""
        self.append_message(session_id, "user", user_msg)
        self.append_message(session_id, "assistant", assistant_msg)

    # ─── 读取 ───

    def get_context(self, session_id: str, limit: int = 30) -> list[dict]:
        """获取最近 N 条消息作为上下文"""
        rows = self.conn.execute(
            "SELECT role, content FROM messages "
            "WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def get_messages(self, session_id: str, offset: int = 0, limit: int = 50) -> list[SessionMessage]:
        """分页获取消息"""
        rows = self.conn.execute(
            "SELECT id, role, content, timestamp, metadata FROM messages "
            "WHERE session_id = ? ORDER BY id LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        ).fetchall()
        return [
            SessionMessage(
                id=r["id"], role=r["role"], content=r["content"],
                timestamp=r["timestamp"],
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in rows
        ]

    # ─── 搜索 ───

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """FTS5 全文搜索（跨会话）"""
        # FTS5 查询语法：多词默认 AND
        fts_query = " AND ".join(query.split())

        rows = self.conn.execute(
            "SELECT msg.session_id, msg.role, msg.content, msg.timestamp, s.title "
            "FROM messages_fts fts "
            "JOIN messages msg ON fts.content_rowid = msg.id "
            "JOIN sessions s ON msg.session_id = s.id "
            "WHERE messages_fts MATCH ? "
            "ORDER BY rank "
            "LIMIT ?",
            (fts_query, limit),
        ).fetchall()

        return [
            {
                "session_id": r["session_id"],
                "title": r["title"],
                "role": r["role"],
                "content": r["content"][:500],  # 截断长内容
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    def search_sessions(self, query: str, limit: int = 3) -> list[Session]:
        """搜索会话（返回会话摘要）"""
        fts_query = " AND ".join(query.split())
        rows = self.conn.execute(
            "SELECT DISTINCT s.id, s.title, s.created_at, s.last_active, s.message_count "
            "FROM messages_fts m "
            "JOIN sessions s ON m.session_id = s.id "
            "WHERE messages_fts MATCH ? "
            "ORDER BY s.last_active DESC "
            "LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        return [
            Session(
                id=r["id"], title=r["title"],
                created_at=r["created_at"], last_active=r["last_active"],
                message_count=r["message_count"],
            )
            for r in rows
        ]

    # ─── 管理 ───

    def list_sessions(self, limit: int = 20) -> list[Session]:
        """列出最近的会话"""
        rows = self.conn.execute(
            "SELECT id, title, created_at, last_active, message_count "
            "FROM sessions ORDER BY last_active DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            Session(id=r["id"], title=r["title"],
                    created_at=r["created_at"], last_active=r["last_active"],
                    message_count=r["message_count"])
            for r in rows
        ]

    def compact(self, session_id: str, keep: int = 120):
        """压缩会话：只保留最近 N 条消息"""
        total = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        if total > keep:
            delete_count = total - keep
            self.conn.execute(
                "DELETE FROM messages WHERE id IN ("
                "  SELECT id FROM messages WHERE session_id = ? ORDER BY id LIMIT ?"
                ")",
                (session_id, delete_count),
            )
            self.conn.execute(
                "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')"
            )
            self.conn.commit()

    # ─── 增量备份 ───

    def incremental_backup(self) -> Optional[Path]:
        """
        增量备份数据库

        使用 SQLite online backup API 创建副本。
        备份文件命名: sessions_YYYYMMDD_HHMMSS.db
        """
        if self._backup_in_progress:
            logger.warning("[SessionStore] Backup already in progress, skipping")
            return None

        self._backup_in_progress = True
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)

            timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            backup_path = self.backup_dir / f"sessions_{timestamp}.db"

            # 使用 SQLite backup API
            backup_conn = sqlite3.connect(str(backup_path))
            try:
                self.conn.backup(backup_conn)
                backup_conn.close()

                # 清理旧备份（保留最近 10 个）
                self._cleanup_old_backups(keep=10)

                logger.info(f"[SessionStore] Backup created: {backup_path}")
                return backup_path
            except Exception:
                backup_conn.close()
                raise

        except Exception as e:
            logger.error(f"[SessionStore] Backup failed: {e}")
            return None
        finally:
            self._backup_in_progress = False

    def restore_from_backup(self, backup_path: str) -> bool:
        """
        从备份恢复数据库

        警告: 会覆盖当前数据库！
        """
        backup_file = Path(backup_path)
        if not backup_file.exists():
            logger.error(f"[SessionStore] Backup not found: {backup_path}")
            return False

        try:
            # 关闭当前连接
            self.close()

            # 创建当前数据库的紧急备份
            if self.db_path.exists():
                emergency = self.db_path.with_suffix(".db.emergency")
                shutil.copy2(self.db_path, emergency)

            # 恢复备份
            shutil.copy2(backup_file, self.db_path)

            # 重新连接
            self._conn = None
            logger.info(f"[SessionStore] Restored from: {backup_path}")
            return True

        except Exception as e:
            logger.error(f"[SessionStore] Restore failed: {e}")
            return False

    def _cleanup_old_backups(self, keep: int = 10):
        """清理旧备份，保留最近 N 个"""
        try:
            backups = sorted(
                self.backup_dir.glob("sessions_*.db"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in backups[keep:]:
                old.unlink()
                logger.debug(f"[SessionStore] Removed old backup: {old.name}")
        except Exception as e:
            logger.warning(f"[SessionStore] Cleanup error: {e}")

    # ─── Parent Session 继承链 ───

    def create_child_session(self, parent_session_id: str,
                              child_id: str = None,
                              title: str = None) -> Session:
        """
        创建子会话（继承父会话上下文）

        子会话会标记 parent_session_id，可用于上下文回溯。
        """
        child_id = child_id or f"{parent_session_id}-child-{int(time.time())}"

        # 获取父会话信息
        parent = self.conn.execute(
            "SELECT title, metadata FROM sessions WHERE id = ?",
            (parent_session_id,),
        ).fetchone()

        if not parent:
            raise ValueError(f"Parent session not found: {parent_session_id}")

        parent_title = parent["title"]
        child_title = title or f"{parent_title} (fork)"

        # 构建 metadata（包含 parent 链接）
        metadata = {
            "parent_session": parent_session_id,
            "forked_at": time.time(),
            "depth": self._get_session_depth(parent_session_id) + 1,
        }

        now = time.time()
        self.conn.execute(
            "INSERT INTO sessions (id, title, created_at, last_active, metadata) VALUES (?, ?, ?, ?, ?)",
            (child_id, child_title, now, now, json.dumps(metadata)),
        )

        # 复制父会话的最近上下文（可选：默认不复制消息）
        self.conn.execute(
            "UPDATE sessions SET message_count = 0 WHERE id = ?",
            (child_id,),
        )
        self.conn.commit()

        logger.info(
            f"[SessionStore] Child session created: {child_id} ← {parent_session_id}"
        )

        return Session(
            id=child_id,
            title=child_title,
            created_at=now,
            last_active=now,
            message_count=0,
        )

    def get_parent_chain(self, session_id: str) -> List[Session]:
        """
        获取会话的父链（从当前会话回溯到根会话）

        返回: [当前会话, 父会话, 祖父会话, ...]
        """
        chain = []
        current_id = session_id
        visited = set()
        max_depth = 50  # 防止循环引用

        while current_id and len(chain) < max_depth:
            if current_id in visited:
                break
            visited.add(current_id)

            row = self.conn.execute(
                "SELECT id, title, created_at, last_active, message_count, metadata "
                "FROM sessions WHERE id = ?",
                (current_id,),
            ).fetchone()

            if not row:
                break

            chain.append(Session(
                id=row["id"],
                title=row["title"],
                created_at=row["created_at"],
                last_active=row["last_active"],
                message_count=row["message_count"],
            ))

            # 解析 parent_session
            try:
                meta = json.loads(row["metadata"] or "{}")
                current_id = meta.get("parent_session")
            except json.JSONDecodeError:
                break

        return chain

    def inherit_context(self, child_session_id: str,
                        parent_session_id: str,
                        max_messages: int = 50) -> int:
        """
        从父会话复制上下文到子会话

        复制最近的 max_messages 条消息（标记为继承）。
        """
        parent_msgs = self.conn.execute(
            "SELECT role, content, metadata FROM messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (parent_session_id, max_messages),
        ).fetchall()

        copied = 0
        now = time.time()
        for msg in reversed(parent_msgs):
            meta = json.loads(msg["metadata"] or "{}")
            meta["inherited_from"] = parent_session_id
            meta["inherited_at"] = now

            self.conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (child_session_id, msg["role"], msg["content"], now, json.dumps(meta)),
            )
            copied += 1

        self.conn.execute(
            "UPDATE sessions SET message_count = message_count + ? WHERE id = ?",
            (copied, child_session_id),
        )
        self.conn.commit()

        logger.info(
            f"[SessionStore] Inherited {copied} messages "
            f"from {parent_session_id} → {child_session_id}"
        )
        return copied

    def _get_session_depth(self, session_id: str) -> int:
        """获取会话深度（从根会话算起）"""
        depth = 0
        current = session_id
        visited = set()

        while current and depth < 100:
            if current in visited:
                break
            visited.add(current)

            row = self.conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (current,)
            ).fetchone()
            if not row:
                break

            try:
                meta = json.loads(row["metadata"] or "{}")
                parent = meta.get("parent_session")
                if not parent:
                    break
                current = parent
                depth += 1
            except json.JSONDecodeError:
                break

        return depth

    # ─── 数据库优化 ───

    def optimize(self):
        """执行数据库优化"""
        self.conn.executescript("""
            PRAGMA optimize;
            PRAGMA analysis_limit=1000;
            INSERT INTO messages_fts(messages_fts) VALUES('optimize');
        """)
        self.conn.commit()
        logger.info("[SessionStore] Database optimized")

    def vacuum(self):
        """压缩数据库（回收空间）"""
        self.conn.execute("VACUUM")
        logger.info("[SessionStore] Database vacuumed")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# ─── 使用示例 ───
def _demo():
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        store = SessionStore(db_path)
        store.init_db()

        # 创建会话
        store.create_session("test-001", "Demo Session")

        # 追加对话
        store.append_exchange("test-001", "你好", "你好！有什么可以帮你的？")
        store.append_exchange("test-001", "今天天气怎么样", "抱歉，我需要联网查询天气信息。")

        # 搜索
        results = store.search("天气")
        print(f"Search '天气': {len(results)} results")
        for r in results:
            print(f"  {r['session_id']}: {r['content'][:80]}")

        store.close()


if __name__ == "__main__":
    _demo()
