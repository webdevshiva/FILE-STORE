import sqlite3
import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
import asyncio
import aiosqlite

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        
    async def init_db(self):
        """Initialize database tables"""
        async with aiosqlite.connect(self.db_path) as db:
            # Users table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP,
                    total_requests INTEGER DEFAULT 0
                )
            ''')
            
            # Sessions table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    start_time TIMESTAMP,
                    expiry_time TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # Database channels
            await db.execute('''
                CREATE TABLE IF NOT EXISTS database_channels (
                    channel_id INTEGER PRIMARY KEY,
                    username TEXT,
                    title TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1
                )
            ''')
            
            # Force-join channels
            await db.execute('''
                CREATE TABLE IF NOT EXISTS force_join_channels (
                    channel_id INTEGER PRIMARY KEY,
                    username TEXT,
                    title TEXT,
                    invite_link TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    required BOOLEAN DEFAULT 1
                )
            ''')
            
            # Links table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS links (
                    link_id TEXT PRIMARY KEY,
                    channel_id INTEGER,
                    message_id INTEGER,
                    start_msg_id INTEGER,
                    end_msg_id INTEGER,
                    link_type TEXT,  -- 'single' or 'batch'
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_by INTEGER,
                    uses INTEGER DEFAULT 0,
                    last_used TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users (user_id)
                )
            ''')
            
            # Verification tokens
            await db.execute('''
                CREATE TABLE IF NOT EXISTS verification_tokens (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    short_url TEXT,
                    is_used BOOLEAN DEFAULT 0,
                    is_bypassed BOOLEAN DEFAULT 0,
                    callback_time TIMESTAMP
                )
            ''')
            
            # Settings
            await db.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Analytics
            await db.execute('''
                CREATE TABLE IF NOT EXISTS analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT,
                    user_id INTEGER,
                    data TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Bypass attempts log
            await db.execute('''
                CREATE TABLE IF NOT EXISTS bypass_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    token TEXT,
                    attempt_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    time_diff REAL
                )
            ''')
            
            # Admin logs
            await db.execute('''
                CREATE TABLE IF NOT EXISTS admin_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    action TEXT,
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await db.commit()
            
            # Insert default settings
            default_settings = [
                ('auto_caption', ''),
                ('auto_caption_enabled', '0'),
                ('shortener_api_url', ''),
                ('shortener_api_key', ''),
                ('verification_min_time', '35'),
                ('session_duration', '21600'),  # 6 hours in seconds
                ('max_requests_per_minute', '20'),
                ('bypass_threshold', '35')
            ]
            
            for key, value in default_settings:
                await db.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value)
                )
            await db.commit()
    
    # ==================== USER OPERATIONS ====================
    async def get_or_create_user(self, user_id: int, username: str, full_name: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
                (user_id, username, full_name)
            )
            await db.execute(
                "UPDATE users SET username = ?, full_name = ?, last_active = CURRENT_TIMESTAMP, total_requests = total_requests + 1 WHERE user_id = ?",
                (username, full_name, user_id)
            )
            await db.commit()
    
    async def get_user(self, user_id: int) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
        return None
    
    # ==================== SESSION OPERATIONS ====================
    async def create_session(self, user_id: int, duration_seconds: int) -> str:
        import secrets
        session_id = secrets.token_hex(16)
        start_time = datetime.now()
        expiry_time = start_time + timedelta(seconds=duration_seconds)
        
        async with aiosqlite.connect(self.db_path) as db:
            # Deactivate any existing sessions
            await db.execute(
                "UPDATE sessions SET is_active = 0 WHERE user_id = ?",
                (user_id,)
            )
            
            # Create new session
            await db.execute(
                "INSERT INTO sessions (session_id, user_id, start_time, expiry_time) VALUES (?, ?, ?, ?)",
                (session_id, user_id, start_time.isoformat(), expiry_time.isoformat())
            )
            await db.commit()
        
        return session_id
    
    async def get_active_session(self, user_id: int) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND is_active = 1 AND expiry_time > ?",
                (user_id, datetime.now().isoformat())
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
        return None
    
    async def deactivate_session(self, user_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET is_active = 0 WHERE user_id = ?",
                (user_id,)
            )
            await db.commit()
    
    async def cleanup_expired_sessions(self):
        """Remove expired sessions"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE sessions SET is_active = 0 WHERE expiry_time <= ?",
                (datetime.now().isoformat(),)
            )
            await db.commit()
    
    # ==================== CHANNEL OPERATIONS ====================
    async def add_database_channel(self, channel_id: int, username: str = "", title: str = ""):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO database_channels (channel_id, username, title) VALUES (?, ?, ?)",
                (channel_id, username, title)
            )
            await self.log_admin_action(
                admin_id=0,  # System
                action="ADD_DB_CHANNEL",
                details=f"Channel {channel_id} added"
            )
            await db.commit()
    
    async def remove_database_channel(self, channel_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM database_channels WHERE channel_id = ?",
                (channel_id,)
            )
            await db.commit()
    
    async def get_all_database_channels(self) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM database_channels ORDER BY added_at DESC"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(zip([col[0] for col in cursor.description], row)) for row in rows]
    
    async def add_force_join_channel(self, channel_id: int, username: str, title: str, invite_link: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO force_join_channels (channel_id, username, title, invite_link) VALUES (?, ?, ?, ?)",
                (channel_id, username, title, invite_link)
            )
            await self.log_admin_action(
                admin_id=0,
                action="ADD_FORCE_JOIN_CHANNEL",
                details=f"Force join channel {title} added"
            )
            await db.commit()
    
    async def update_force_join_channel(self, channel_id: int, is_active: bool = None, required: bool = None):
        async with aiosqlite.connect(self.db_path) as db:
            updates = []
            params = []
            
            if is_active is not None:
                updates.append("is_active = ?")
                params.append(is_active)
            
            if required is not None:
                updates.append("required = ?")
                params.append(required)
            
            if updates:
                params.append(channel_id)
                await db.execute(
                    f"UPDATE force_join_channels SET {', '.join(updates)} WHERE channel_id = ?",
                    params
                )
                await db.commit()
    
    async def get_force_join_channels(self, active_only: bool = True) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            query = "SELECT * FROM force_join_channels"
            if active_only:
                query += " WHERE is_active = 1"
            query += " ORDER BY added_at DESC"
            
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [dict(zip([col[0] for col in cursor.description], row)) for row in rows]
    
    async def remove_force_join_channel(self, channel_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM force_join_channels WHERE channel_id = ?",
                (channel_id,)
            )
            await db.commit()
    
    # ==================== LINK OPERATIONS ====================
    async def create_single_link(self, channel_id: int, message_id: int, creator_id: int) -> str:
        import secrets
        link_id = secrets.token_hex(8)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO links (link_id, channel_id, message_id, link_type, created_by) 
                   VALUES (?, ?, ?, 'single', ?)""",
                (link_id, channel_id, message_id, creator_id)
            )
            await self.log_admin_action(
                admin_id=creator_id,
                action="CREATE_SINGLE_LINK",
                details=f"Link {link_id} created for message {message_id}"
            )
            await db.commit()
        
        return link_id
    
    async def create_batch_link(self, channel_id: int, start_msg_id: int, end_msg_id: int, creator_id: int) -> str:
        import secrets
        link_id = secrets.token_hex(8)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO links (link_id, channel_id, start_msg_id, end_msg_id, link_type, created_by) 
                   VALUES (?, ?, ?, ?, 'batch', ?)""",
                (link_id, channel_id, start_msg_id, end_msg_id, creator_id)
            )
            await self.log_admin_action(
                admin_id=creator_id,
                action="CREATE_BATCH_LINK",
                details=f"Batch {link_id}: {start_msg_id}-{end_msg_id}"
            )
            await db.commit()
        
        return link_id
    
    async def get_link_info(self, link_id: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM links WHERE link_id = ?",
                (link_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
        return None
    
    async def increment_link_uses(self, link_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE links SET uses = uses + 1, last_used = CURRENT_TIMESTAMP WHERE link_id = ?",
                (link_id,)
            )
            await db.commit()
    
    async def get_recent_links(self, limit: int = 10) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM links ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(zip([col[0] for col in cursor.description], row)) for row in rows]
    
    async def get_popular_links(self, limit: int = 5) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT link_id, uses FROM links ORDER BY uses DESC LIMIT ?",
                (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(zip([col[0] for col in cursor.description], row)) for row in rows]
    
    async def delete_link(self, link_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM links WHERE link_id = ?",
                (link_id,)
            )
            await db.commit()
    
    # ==================== VERIFICATION TOKENS ====================
    async def create_verification_token(self, user_id: int, short_url: str = None) -> str:
        import secrets
        token = secrets.token_hex(16)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO verification_tokens (token, user_id, short_url) VALUES (?, ?, ?)",
                (token, user_id, short_url)
            )
            await db.commit()
        
        return token
    
    async def get_token_info(self, token: str) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM verification_tokens WHERE token = ?",
                (token,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
        return None
    
    async def verify_token(self, token: str, user_id: int, callback_time: datetime) -> Tuple[bool, bool, float]:
        """Returns (is_valid, is_bypassed, time_diff)"""
        async with aiosqlite.connect(self.db_path) as db:
            # Get token creation time
            async with db.execute(
                "SELECT created_at FROM verification_tokens WHERE token = ? AND user_id = ? AND is_used = 0",
                (token, user_id)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return False, False, 0
                
                created_at = datetime.fromisoformat(row[0])
                time_diff = (callback_time - created_at).total_seconds()
                
                # Check if bypassed (< 35 seconds by default)
                bypass_threshold = float(await self.get_setting("bypass_threshold", "35"))
                is_bypassed = time_diff < bypass_threshold
                
                # Update token
                await db.execute(
                    "UPDATE verification_tokens SET is_used = 1, is_bypassed = ?, callback_time = ? WHERE token = ?",
                    (is_bypassed, callback_time.isoformat(), token)
                )
                
                # Log bypass attempt if detected
                if is_bypassed:
                    await db.execute(
                        "INSERT INTO bypass_logs (user_id, token, time_diff) VALUES (?, ?, ?)",
                        (user_id, token, time_diff)
                    )
                
                await db.commit()
                
                return True, is_bypassed, time_diff
    
    async def get_user_by_token(self, token: str) -> Optional[int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT user_id FROM verification_tokens WHERE token = ? AND is_used = 0",
                (token,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None
    
    # ==================== SETTINGS ====================
    async def get_setting(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else default
    
    async def set_setting(self, key: str, value: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (key, value)
            )
            await db.commit()
    
    async def get_all_settings(self) -> Dict[str, str]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT key, value FROM settings") as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}
    
    # ==================== ANALYTICS ====================
    async def log_event(self, event_type: str, user_id: int, data: Dict = None):
        async with aiosqlite.connect(self.db_path) as db:
            data_str = json.dumps(data) if data else "{}"
            await db.execute(
                "INSERT INTO analytics (event_type, user_id, data) VALUES (?, ?, ?)",
                (event_type, user_id, data_str)
            )
            await db.commit()
    
    async def get_total_users(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
    
    async def get_active_sessions_count(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM sessions WHERE is_active = 1 AND expiry_time > ?",
                (datetime.now().isoformat(),)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
    
    async def get_today_verifications(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            today = datetime.now().date().isoformat()
            async with db.execute(
                "SELECT COUNT(*) FROM verification_tokens WHERE DATE(created_at) = ? AND is_used = 1 AND is_bypassed = 0",
                (today,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
    
    async def get_bypass_attempts_count(self, days: int = 7) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            async with db.execute(
                "SELECT COUNT(*) FROM verification_tokens WHERE created_at > ? AND is_bypassed = 1",
                (cutoff,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
    
    async def get_daily_stats(self, days: int = 7) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
            async with db.execute('''
                SELECT 
                    DATE(timestamp) as date,
                    COUNT(CASE WHEN event_type = 'verification_success' THEN 1 END) as verifications,
                    COUNT(CASE WHEN event_type = 'link_access' THEN 1 END) as link_accesses,
                    COUNT(CASE WHEN event_type = 'batch_access' THEN 1 END) as batch_accesses
                FROM analytics 
                WHERE DATE(timestamp) >= ?
                GROUP BY DATE(timestamp)
                ORDER BY date DESC
            ''', (cutoff,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(zip([col[0] for col in cursor.description], row)) for row in rows]
    
    async def get_top_users(self, limit: int = 10) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('''
                SELECT 
                    u.user_id,
                    u.username,
                    u.full_name,
                    u.total_requests,
                    COUNT(DISTINCT s.session_id) as total_sessions,
                    MAX(s.start_time) as last_session
                FROM users u
                LEFT JOIN sessions s ON u.user_id = s.user_id
                GROUP BY u.user_id
                ORDER BY u.total_requests DESC
                LIMIT ?
            ''', (limit,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(zip([col[0] for col in cursor.description], row)) for row in rows]
    
    # ==================== ADMIN LOGS ====================
    async def log_admin_action(self, admin_id: int, action: str, details: str = ""):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO admin_logs (admin_id, action, details) VALUES (?, ?, ?)",
                (admin_id, action, details)
            )
            await db.commit()
    
    async def get_admin_logs(self, limit: int = 50) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM admin_logs ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(zip([col[0] for col in cursor.description], row)) for row in rows]
    
    # ==================== CLEANUP OPERATIONS ====================
    async def cleanup_old_tokens(self, days: int = 1):
        """Remove verification tokens older than X days"""
        async with aiosqlite.connect(self.db_path) as db:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            await db.execute(
                "DELETE FROM verification_tokens WHERE created_at < ?",
                (cutoff,)
            )
            await db.commit()
    
    async def cleanup_old_analytics(self, days: int = 30):
        """Remove old analytics data"""
        async with aiosqlite.connect(self.db_path) as db:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            await db.execute(
                "DELETE FROM analytics WHERE timestamp < ?",
                (cutoff,)
            )
            await db.commit()
    
    # ==================== BACKUP & MAINTENANCE ====================
    async def get_database_stats(self) -> Dict[str, int]:
        stats = {}
        tables = ['users', 'sessions', 'database_channels', 'force_join_channels', 
                 'links', 'verification_tokens', 'analytics', 'bypass_logs', 'admin_logs']
        
        async with aiosqlite.connect(self.db_path) as db:
            for table in tables:
                async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                    row = await cursor.fetchone()
                    stats[table] = row[0] if row else 0
        
        # Add some calculated stats
        stats['active_sessions'] = await self.get_active_sessions_count()
        stats['today_verifications'] = await self.get_today_verifications()
        
        return stats
    
    async def export_data(self, table: str) -> List[Dict]:
        """Export table data (for backup)"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(f"SELECT * FROM {table}") as cursor:
                rows = await cursor.fetchall()
                columns = [col[0] for col in cursor.description]
                return [dict(zip(columns, row)) for row in rows]