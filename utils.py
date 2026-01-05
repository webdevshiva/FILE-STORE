import aiohttp
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import re
from telegram import Message
from config import SHORTENER_API_URL, SHORTENER_API_KEY, VERIFICATION_MIN_TIME

class ShortenerAPI:
    @staticmethod
    async def shorten_url(long_url: str) -> Optional[str]:
        """Call custom shortener API"""
        if not SHORTENER_API_URL:
            return None
        
        try:
            async with aiohttp.ClientSession() as session:
                # Modify this based on your API requirements
                payload = {
                    "url": long_url,
                    "api_key": SHORTENER_API_KEY
                }
                
                async with session.post(SHORTENER_API_URL, json=payload, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        # Adjust based on your API response format
                        return data.get("short_url") or data.get("url")
                    return None
        except Exception as e:
            print(f"Shortener API error: {e}")
            return None

class Validators:
    @staticmethod
    def parse_telegram_link(link: str) -> Optional[Dict]:
        """Parse Telegram message link to extract channel and message ID"""
        patterns = [
            r"https://t\.me/(c/)?(\d+)/(\d+)",  # Private channels
            r"https://t\.me/([a-zA-Z0-9_]+)/(\d+)"  # Public channels
        ]
        
        for pattern in patterns:
            match = re.match(pattern, link)
            if match:
                groups = match.groups()
                if len(groups) == 3 and groups[0] == "c/":
                    # Private channel: t.me/c/123456789/45
                    return {"channel_id": int(groups[1]), "message_id": int(groups[2])}
                elif len(groups) == 2:
                    # Public channel: t.me/username/123
                    return {"username": groups[0], "message_id": int(groups[1])}
        
        return None

class CaptionManager:
    def __init__(self, db):
        self.db = db
    
    async def apply_caption(self, original_caption: str = "", user_id: int = None, 
                           file_name: str = "", batch_name: str = "", 
                           expiry_time: str = "") -> str:
        """Apply auto-caption with placeholders"""
        auto_caption = await self.db.get_setting("auto_caption", "")
        
        if not auto_caption:
            return original_caption
        
        # Replace placeholders
        replacements = {
            "{file_name}": file_name,
            "{batch_name}": batch_name,
            "{user_id}": str(user_id) if user_id else "",
            "{expiry_time}": expiry_time
        }
        
        for placeholder, value in replacements.items():
            auto_caption = auto_caption.replace(placeholder, value)
        
        # Combine with original caption if exists
        if original_caption:
            return f"{auto_caption}\n\n{original_caption}"
        
        return auto_caption

class AlertSystem:
    def __init__(self, bot, admin_ids):
        self.bot = bot
        self.admin_ids = admin_ids
    
    async def send_admin_alert(self, message: str, alert_type: str = "info"):
        """Send alert to all admins"""
        emoji = {
            "error": "🚨",
            "warning": "⚠️",
            "info": "ℹ️",
            "success": "✅"
        }.get(alert_type, "ℹ️")
        
        formatted_message = f"{emoji} {message}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(chat_id=admin_id, text=formatted_message)
            except Exception as e:
                print(f"Failed to send alert to admin {admin_id}: {e}")

class TimeUtils:
    @staticmethod
    def format_time_left(expiry_time: datetime) -> str:
        """Format remaining time in human-readable format"""
        now = datetime.now()
        if expiry_time <= now:
            return "Expired"
        
        diff = expiry_time - now
        hours, remainder = divmod(diff.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    
    @staticmethod
    def format_datetime(dt: datetime) -> str:
        """Format datetime for display"""
        return dt.strftime("%H:%M %d/%m/%Y")

        # Add to utils.py
class MembershipChecker:
    @staticmethod
    async def check_membership(bot, user_id: int, channel_info: Dict) -> bool:
        """Check if user is member of a channel"""
        try:
            # Handle both channel_id and username
            chat_id = channel_info.get('channel_id')
            if not chat_id and 'username' in channel_info:
                # Get channel by username
                chat = await bot.get_chat(f"@{channel_info['username']}")
                chat_id = chat.id
            elif chat_id:
                # Ensure negative ID for private channels
                if chat_id > 0:
                    chat_id = f"-100{chat_id}"
            
            # Check membership
            member = await bot.get_chat_member(chat_id, user_id)
            return member.status in ['member', 'administrator', 'creator']
        except Exception as e:
            logger.error(f"Membership check error: {e}")
            return False
    
    @staticmethod
    async def check_all_memberships(bot, user_id: int, channels: List[Dict]) -> List[Dict]:
        """Check membership for multiple channels"""
        results = []
        for channel in channels:
            is_member = await MembershipChecker.check_membership(bot, user_id, channel)
            results.append({
                **channel,
                'is_member': is_member
            })
        return results