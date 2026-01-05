import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
import re
from collections import defaultdict

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)
from telegram.error import TelegramError, BadRequest

import config
from database import Database
from keyboards import *
from utils import ShortenerAPI, Validators, CaptionManager, AlertSystem, TimeUtils

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
ASK_START_MSG, ASK_END_MSG = range(2)
# Admin conversation states
ADD_DB_CHANNEL, ADD_FORCE_JOIN_CHANNEL, SET_CAPTION, SET_SHORTENER = range(2, 6)

class MembershipChecker:
    @staticmethod
    async def check_membership(bot, user_id: int, channel_info: Dict) -> bool:
        """Check if user is member of a channel"""
        try:
            chat_id = channel_info.get('channel_id')
            if not chat_id and 'username' in channel_info:
                chat = await bot.get_chat(f"@{channel_info['username']}")
                chat_id = chat.id
            elif chat_id:
                if chat_id > 0:
                    chat_id = f"-100{chat_id}"
            
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

class TelegramBot:
    def __init__(self):
        self.db = Database(config.DATABASE_PATH)
        self.shortener = ShortenerAPI()
        self.caption_manager = None
        self.alert_system = None
        self.time_utils = TimeUtils()
        self.membership_checker = MembershipChecker()
        self.user_requests = defaultdict(list)
        
    async def initialize(self):
        """Initialize database and components"""
        await self.db.init_db()
        self.caption_manager = CaptionManager(self.db)
    
    # ==================== RATE LIMITING ====================
    async def check_rate_limit(self, user_id: int, action: str = "message") -> Tuple[bool, str]:
        """Check if user is rate limited"""
        now = datetime.now()
        
        # Clean old requests
        self.user_requests[user_id] = [
            req_time for req_time in self.user_requests[user_id]
            if (now - req_time).seconds < 60
        ]
        
        # Check limits
        if len(self.user_requests[user_id]) >= 20:
            return False, "⚠️ Rate limit exceeded. Please wait 1 minute."
        
        if action == "verification" and len([r for r in self.user_requests[user_id] if (now - r).seconds < 30]) >= 3:
            return False, "⚠️ Too many verification attempts. Wait 30 seconds."
        
        # Add current request
        self.user_requests[user_id].append(now)
        return True, ""
    
    # ==================== MEMBERSHIP CHECKING ====================
    async def check_force_join(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                              link_id: str, is_batch: bool = False):
        """Check if user has joined all required channels"""
        user = update.effective_user
        channels = await self.db.get_force_join_channels()
        
        if not channels:
            await self.start_verification(update, context, link_id, is_batch)
            return
        
        membership_results = await self.membership_checker.check_all_memberships(
            context.bot, user.id, channels
        )
        
        all_joined = all(channel['is_member'] for channel in membership_results)
        
        if all_joined:
            await self.start_verification(update, context, link_id, is_batch)
        else:
            keyboard = []
            for channel in membership_results:
                status = "✅" if channel['is_member'] else "❌"
                keyboard.append([
                    InlineKeyboardButton(
                        f"{status} Join {channel['title']}",
                        url=channel['invite_link']
                    )
                ])
            
            keyboard.append([
                InlineKeyboardButton("🔄 Recheck Status", callback_data="recheck_join")
            ])
            
            await update.message.reply_text(
                "🔔 Join all channels to continue\n\n"
                "You must join all required channels before accessing content.\n"
                "✅ = Joined\n❌ = Not Joined",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
            context.user_data["pending_link"] = link_id
            context.user_data["is_batch"] = is_batch
    
    # ==================== FILE COPYING WITH CAPTION ====================
    async def send_single_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                              link_info: Dict, user_id: int):
        """Send single file to user with auto caption"""
        try:
            original_msg = await context.bot.copy_message(
                chat_id=update.effective_chat.id,
                from_chat_id=link_info["channel_id"],
                message_id=link_info["message_id"]
            )
            
            auto_caption = await self.db.get_setting("auto_caption_enabled", "0")
            if auto_caption == "1":
                caption = await self.caption_manager.apply_caption(
                    original_caption=original_msg.caption or "",
                    user_id=user_id,
                    file_name=link_info.get("file_name", "File"),
                    expiry_time=datetime.now().strftime("%Y-%m-%d %H:%M")
                )
                
                if caption != (original_msg.caption or ""):
                    await original_msg.edit_caption(caption=caption)
            
            keyboard = get_time_left_keyboard()
            await update.message.reply_text(
                "🔓 Unlimited Access Active\n"
                "You can open any valid file or batch link.",
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Error sending file: {e}")
            await update.message.reply_text("❌ Failed to retrieve file. Contact admin.")
    
    async def send_batch_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                              batch_info: Dict, user_id: int):
        """Send batch of files to user with auto caption"""
        try:
            start_id = batch_info["start_msg_id"]
            end_id = batch_info["end_msg_id"]
            channel_id = batch_info["channel_id"]
            
            auto_caption = await self.db.get_setting("auto_caption_enabled", "0")
            
            sent_count = 0
            for msg_id in range(start_id, end_id + 1):
                try:
                    msg = await context.bot.copy_message(
                        chat_id=update.effective_chat.id,
                        from_chat_id=channel_id,
                        message_id=msg_id
                    )
                    
                    if auto_caption == "1" and msg.caption:
                        caption = await self.caption_manager.apply_caption(
                            original_caption=msg.caption,
                            user_id=user_id,
                            file_name=f"Batch File {sent_count + 1}",
                            batch_name=f"Batch {batch_info['link_id']}",
                            expiry_time=datetime.now().strftime("%Y-%m-%d %H:%M")
                        )
                        
                        if caption != msg.caption:
                            await msg.edit_caption(caption=caption)
                    
                    sent_count += 1
                    
                    if sent_count % 10 == 0:
                        await asyncio.sleep(1)
                    
                except Exception as e:
                    logger.error(f"Error copying message {msg_id}: {e}")
                    continue
            
            keyboard = get_time_left_keyboard()
            await update.message.reply_text(
                f"✅ Sent {sent_count} files from batch.\n"
                "🔓 Unlimited Access Active\n"
                "You can open any valid file or batch link.",
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Error sending batch: {e}")
            await update.message.reply_text("❌ Failed to retrieve batch. Contact admin.")
    
    # ==================== VERIFICATION HANDLERS ====================
    async def start_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                link_id: str, is_batch: bool = False):
        """Start verification process with rate limiting"""
        user = update.effective_user
        
        allowed, message = await self.check_rate_limit(user.id, "verification")
        if not allowed:
            await update.message.reply_text(message)
            return
        
        token = await self.db.create_verification_token(user.id)
        
        verification_url = f"https://your-domain.com/verify/{token}"
        short_url = await self.shortener.shorten_url(verification_url)
        
        if not short_url:
            await self.handle_shortener_failure(update, user.id)
            return
        
        context.user_data["pending_link"] = link_id
        context.user_data["is_batch"] = is_batch
        context.user_data["verification_token"] = token
        
        keyboard = get_verification_keyboard(short_url)
        
        await update.message.reply_text(
            "🔐 Verify & Get Unlimited Access\n\n"
            "Unlock unlimited files & batches for the next 6 hours.\n\n"
            "⚠️ Open the link properly and wait 35+ seconds.\n"
            "Bypass attempts are detected.",
            reply_markup=keyboard
        )
    
    async def handle_shortener_failure(self, update: Update, user_id: int):
        """Handle shortener API failure"""
        keyboard = get_shortener_failed_keyboard()
        
        await update.message.reply_text(
            "⚠️ Verification temporarily unavailable\n\n"
            "Our verification system is facing an issue.\n"
            "Please try again shortly.",
            reply_markup=keyboard
        )
        
        if self.alert_system:
            await self.alert_system.send_admin_alert(
                "🚨 SHORTENER API FAILURE\nError: Timeout / Invalid response",
                "error"
            )
    
    async def verification_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle verification callback"""
        query = update.callback_query
        await query.answer()
        
        user = update.effective_user
        token = context.user_data.get("verification_token")
        link_id = context.user_data.get("pending_link")
        is_batch = context.user_data.get("is_batch", False)
        
        if not token or not link_id:
            await query.edit_message_text("❌ Session expired. Please try the link again.")
            return
        
        is_valid, is_bypassed = await self.db.verify_token(token, user.id, datetime.now())
        
        if not is_valid:
            await query.edit_message_text("❌ Invalid verification. Please try again.")
            return
        
        if is_bypassed:
            keyboard = get_bypass_keyboard()
            await query.edit_message_text(
                "❌ Verification Failed\n\n"
                "You completed verification too fast.\n"
                "This looks like a bypass attempt.\n\n"
                "Don't teach your father how to make babies.\n"
                "Complete the process properly.",
                reply_markup=keyboard
            )
            
            new_token = await self.db.create_verification_token(user.id)
            context.user_data["verification_token"] = new_token
            return
        
        session_id = await self.db.create_session(user.id, config.SESSION_DURATION)
        
        await query.edit_message_text(
            "✅ Verification Successful\n"
            "⏰ Unlimited access enabled for 6 hours"
        )
        
        if is_batch:
            batch_info = await self.db.get_link_info(link_id)
            if batch_info:
                await self.send_batch_files(update, context, batch_info, user.id)
                await self.db.increment_link_uses(link_id)
        else:
            link_info = await self.db.get_link_info(link_id)
            if link_info:
                await self.send_single_file(update, context, link_info, user.id)
                await self.db.increment_link_uses(link_id)
        
        await self.db.log_event("verification_success", user.id, {"session_id": session_id})
    
    async def handle_recheck_join(self, query, context):
        """Handle recheck join status"""
        await query.answer("Checking your status...")
        
        user = query.from_user
        link_id = context.user_data.get("pending_link")
        is_batch = context.user_data.get("is_batch", False)
        channels = await self.db.get_force_join_channels()
        
        membership_results = await self.membership_checker.check_all_memberships(
            context.bot, user.id, channels
        )
        
        all_joined = all(channel['is_member'] for channel in membership_results)
        
        if all_joined:
            await query.edit_message_text("✅ All channels joined successfully!")
            await self.start_verification(
                update=Update(update_id=query.id, message=query.message),
                context=context,
                link_id=link_id,
                is_batch=is_batch
            )
        else:
            keyboard = []
            for channel in membership_results:
                status = "✅" if channel['is_member'] else "❌"
                keyboard.append([
                    InlineKeyboardButton(
                        f"{status} Join {channel['title']}",
                        url=channel['invite_link']
                    )
                ])
            
            keyboard.append([
                InlineKeyboardButton("🔄 Recheck Status", callback_data="recheck_join")
            ])
            
            await query.edit_message_text(
                "🔔 Join all channels to continue\n\n"
                "You must join all required channels before accessing content.\n"
                "✅ = Joined\n❌ = Not Joined",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    async def handle_retry_verification(self, query, context):
        """Handle verification retry"""
        await query.answer("Starting verification...")
        
        link_id = context.user_data.get("pending_link")
        is_batch = context.user_data.get("is_batch", False)
        
        if link_id:
            await self.start_verification(
                update=Update(update_id=query.id, message=query.message),
                context=context,
                link_id=link_id,
                is_batch=is_batch
            )
    
    async def handle_verify_again(self, query, context):
        """Handle verify again after bypass"""
        await query.answer("Starting new verification...")
        
        user = query.from_user
        new_token = await self.db.create_verification_token(user.id)
        context.user_data["verification_token"] = new_token
        
        verification_url = f"https://your-domain.com/verify?token={new_token}"
        short_url = await self.shortener.shorten_url(verification_url)
        
        if short_url:
            keyboard = get_verification_keyboard(short_url)
            await query.edit_message_text(
                "🔐 New Verification Started\n\n"
                "Complete the verification properly.",
                reply_markup=keyboard
            )
    
    async def handle_copy_link(self, query, context, link):
        """Handle copy link button"""
        await query.answer("Link copied to clipboard!", show_alert=True)
    
    async def time_left_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show remaining session time"""
        query = update.callback_query
        await query.answer()
        
        user = update.effective_user
        session = await self.db.get_active_session(user.id)
        
        if session:
            expiry_time = datetime.fromisoformat(session["expiry_time"])
            time_left = self.time_utils.format_time_left(expiry_time)
            expiry_str = expiry_time.strftime("%H:%M %d/%m/%Y")
            
            await query.edit_message_text(
                f"⏳ Time Remaining\n"
                f"{time_left} left\n"
                f"Expires at {expiry_str}"
            )
        else:
            await query.edit_message_text("❌ No active session.")
    
    # ==================== MAIN HANDLERS ====================
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command with rate limiting"""
        user = update.effective_user
        
        allowed, message = await self.check_rate_limit(user.id)
        if not allowed:
            await update.message.reply_text(message)
            return
        
        await self.db.get_or_create_user(user.id, user.username, user.full_name)
        
        args = context.args
        if not args:
            await update.message.reply_text("👋 Welcome! Access files using admin-generated links only.")
            return
        
        param = args[0]
        
        if param.startswith("verify_"):
            await self.handle_verification_webhook(update, context)
        elif param.startswith("batch_"):
            await self.handle_batch_link(update, context, param[6:])
        else:
            await self.handle_single_link(update, context, param)
    
    async def handle_single_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE, link_id: str):
        """Handle single file link access with rate limiting"""
        user = update.effective_user
        
        allowed, message = await self.check_rate_limit(user.id, "link_access")
        if not allowed:
            await update.message.reply_text(message)
            return
        
        await self.db.log_event("link_access", user.id, {"link_id": link_id})
        
        session = await self.db.get_active_session(user.id)
        if session:
            link_info = await self.db.get_link_info(link_id)
            if not link_info:
                await update.message.reply_text("❌ Invalid or expired link.")
                return
            
            await self.send_single_file(update, context, link_info, user.id)
            await self.db.increment_link_uses(link_id)
            return
        
        await self.check_force_join(update, context, link_id)
    
    async def handle_batch_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE, batch_id: str):
        """Handle batch link access"""
        user = update.effective_user
        
        await self.db.log_event("batch_access", user.id, {"batch_id": batch_id})
        
        session = await self.db.get_active_session(user.id)
        if session:
            batch_info = await self.db.get_link_info(batch_id)
            if not batch_info:
                await update.message.reply_text("❌ Invalid or expired batch link.")
                return
            
            await self.send_batch_files(update, context, batch_info, user.id)
            await self.db.increment_link_uses(batch_id)
            return
        
        await self.check_force_join(update, context, batch_id, is_batch=True)
    
    # ==================== ADMIN COMMANDS ====================
    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin panel"""
        user = update.effective_user
        
        if user.id not in config.ADMIN_IDS:
            await update.message.reply_text("❌ Access denied.")
            return
        
        keyboard = get_admin_keyboard()
        await update.message.reply_text(
            "👑 Admin Panel\n"
            "Select an option:",
            reply_markup=keyboard
        )
    
    async def batch_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start batch link creation"""
        user = update.effective_user
        
        if user.id not in config.ADMIN_IDS:
            await update.message.reply_text("❌ Access denied.")
            return
        
        await update.message.reply_text(
            "📦 Batch Link Creation\n\n"
            "Please send the Telegram message link\n"
            "of the FIRST file in the batch."
        )
        
        return ASK_START_MSG
    
    async def ask_start_msg(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process start message link"""
        link = update.message.text
        parsed = Validators.parse_telegram_link(link)
        
        if not parsed:
            await update.message.reply_text("❌ Invalid link format. Try again.")
            return ASK_START_MSG
        
        context.user_data["batch_start"] = parsed
        
        await update.message.reply_text(
            "✅ First message saved.\n\n"
            "Now send the Telegram message link\n"
            "of the LAST file in the batch."
        )
        
        return ASK_END_MSG
    
    async def ask_end_msg(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process end message link and create batch"""
        link = update.message.text
        parsed = Validators.parse_telegram_link(link)
        
        if not parsed:
            await update.message.reply_text("❌ Invalid link format. Try again.")
            return ASK_END_MSG
        
        start_info = context.user_data.get("batch_start")
        
        if not start_info:
            await update.message.reply_text("❌ Session expired. Start over.")
            return ConversationHandler.END
        
        if parsed.get("message_id", 0) <= start_info.get("message_id", 0):
            await update.message.reply_text("❌ End message must come after start message.")
            return ConversationHandler.END
        
        batch_id = await self.db.create_batch_link(
            start_info.get("channel_id", 0),
            start_info.get("message_id", 0),
            parsed.get("message_id", 0),
            update.effective_user.id
        )
        
        bot_username = (await context.bot.get_me()).username
        batch_link = f"https://t.me/{bot_username}?start=batch_{batch_id}"
        
        keyboard = get_batch_result_keyboard(batch_link)
        await update.message.reply_text(
            f"✅ Batch Link Generated Successfully\n\n"
            f"📦 Files Included:\n"
            f"From message ID {start_info.get('message_id', 0)} → {parsed.get('message_id', 0)}\n\n"
            f"🔗 Batch Link:\n{batch_link}",
            reply_markup=keyboard
        )
        
        return ConversationHandler.END
    
    async def cancel_batch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel batch creation"""
        await update.message.reply_text("❌ Batch creation cancelled.")
        return ConversationHandler.END
    
    # ==================== CALLBACK HANDLER ====================
    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle all callback queries"""
        query = update.callback_query
        data = query.data
        
        if data == "recheck_join":
            await self.handle_recheck_join(query, context)
        elif data == "retry_verification":
            await self.handle_retry_verification(query, context)
        elif data == "time_left":
            await self.time_left_callback(update, context)
        elif data == "verify_again":
            await self.handle_verify_again(query, context)
        elif data.startswith("copy_"):
            await self.handle_copy_link(query, context, data[5:])
        elif data == "cancel_batch":
            await self.cancel_batch(update, context)
        elif data.startswith("admin_"):
            await self.handle_admin_callback(query, context, data[6:])
        else:
            await query.answer("✅ Action completed")
    
    async def handle_admin_callback(self, query, context, action):
        """Handle admin panel callbacks"""
        await query.answer()
        
        if action == "links":
            await self.show_link_management(query)
        elif action == "channels":
            await self.show_database_channels(query)
        elif action == "force_join":
            await self.show_force_join_channels(query)
        elif action == "caption":
            await self.show_caption_management(query)
        elif action == "shortener":
            await self.show_shortener_config(query)
        elif action == "analytics":
            await self.show_analytics_dashboard(query)
        elif action == "back":
            keyboard = get_admin_keyboard()
            await query.edit_message_text(
                "👑 Admin Panel\nSelect an option:",
                reply_markup=keyboard
            )
        else:
            await query.edit_message_text("✅ Admin action processed")
    
    # ==================== ADMIN PANEL METHODS ====================
    async def show_link_management(self, query):
        """Show link management interface"""
        links = await self.db.get_recent_links(limit=5)
        
        text = "🔗 Link Management\n\nRecent Links:\n"
        for link in links:
            text += f"• ID: {link['link_id'][:8]} | Uses: {link['uses']}\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_database_channels(self, query):
        """Show database channels management"""
        channels = await self.db.get_all_database_channels()
        
        text = "📂 Database Channels\n\n"
        if not channels:
            text += "No database channels added.\n"
        else:
            for channel in channels:
                text += f"• {channel['title']}\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_force_join_channels(self, query):
        """Show force-join channels management"""
        channels = await self.db.get_force_join_channels()
        
        text = "🔔 Force-Join Channels\n\n"
        if not channels:
            text += "No force-join channels added.\n"
        else:
            for channel in channels:
                status = "✅ Active" if channel['is_active'] else "❌ Inactive"
                text += f"• {channel['title']} - {status}\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_caption_management(self, query):
        """Show auto-caption management"""
        caption_enabled = await self.db.get_setting("auto_caption_enabled", "0")
        current_caption = await self.db.get_setting("auto_caption", "")
        
        text = "📝 Auto Caption Management\n\n"
        text += f"Status: {'✅ Enabled' if caption_enabled == '1' else '❌ Disabled'}\n\n"
        text += "Current Caption:\n"
        text += f"{current_caption[:100]}...\n" if current_caption else "Not set\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_shortener_config(self, query):
        """Show shortener configuration"""
        api_url = await self.db.get_setting("shortener_api_url", "Not set")
        api_key = await self.db.get_setting("shortener_api_key", "Not set")
        
        text = "🌐 Short Link System\n\n"
        text += f"API URL: {api_url[:30]}...\n" if api_url != "Not set" else "API URL: Not set\n"
        text += "API Key: Configured\n" if api_key != "Not set" else "API Key: Not set\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_analytics_dashboard(self, query):
        """Show analytics dashboard"""
        total_users = await self.db.get_total_users()
        active_sessions = await self.db.get_active_sessions_count()
        
        text = "📊 Analytics Dashboard\n\n"
        text += f"👥 Total Users: {total_users}\n"
        text += f"🔓 Active Sessions: {active_sessions}\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def handle_verification_webhook(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle verification from web page"""
        if len(context.args) != 1:
            await update.message.reply_text("❌ Invalid verification link.")
            return
        
        token = context.args[0]
        user_id = await self.db.get_user_by_token(token)
        
        if not user_id or update.effective_user.id != user_id:
            await update.message.reply_text("❌ Invalid or expired token.")
            return
        
        is_valid, is_bypassed = await self.db.verify_token(token, user_id, datetime.now())
        
        if not is_valid:
            await update.message.reply_text("❌ Invalid verification.")
            return
        
        if is_bypassed:
            await update.message.reply_text("❌ Verification too fast! Bypass detected.")
            return
        
        session_id = await self.db.create_session(user_id, config.SESSION_DURATION)
        
        await update.message.reply_text(
            "✅ Verification Successful!\n"
            "⏰ Unlimited access enabled for 6 hours"
        )
        
        await self.db.log_event("web_verification_success", user_id, {"session_id": session_id})
    
    # ==================== SETUP HANDLERS ====================
    def setup_handlers(self, application):
        """Setup all bot handlers"""
        # Start command
        application.add_handler(CommandHandler("start", self.start_command))
        
        # Admin commands
        application.add_handler(CommandHandler("admin", self.admin_command))
        
        # Batch creation conversation
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("batch", self.batch_command)],
            states={
                ASK_START_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.ask_start_msg)],
                ASK_END_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.ask_end_msg)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_batch)],
        )
        application.add_handler(conv_handler)
        
        # Callback queries
        application.add_handler(CallbackQueryHandler(self.callback_handler))
        
        # Unknown commands
        application.add_handler(MessageHandler(filters.COMMAND, self.unknown_command))
    
    async def unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle unknown commands"""
        await update.message.reply_text("❌ Unknown command. Use /start")
    
    async def handle_admin_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle admin text messages"""
        if update.effective_user.id not in config.ADMIN_IDS:
            return
        
        if context.user_data.get("editing_caption"):
            await self.process_caption_edit(update, context)
        elif context.user_data.get("configuring_shortener"):
            await self.process_shortener_config(update, context)
    
    async def process_caption_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process caption edit"""
        new_caption = update.message.text
        
        if len(new_caption) > 1000:
            await update.message.reply_text("❌ Caption too long (max 1000 chars).")
            return
        
        await self.db.set_setting("auto_caption", new_caption)
        await update.message.reply_text("✅ Caption updated successfully!")
        
        del context.user_data["editing_caption"]
    
    async def process_shortener_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process shortener configuration"""
        if context.user_data.get("configuring_shortener") == "url":
            api_url = update.message.text
            
            if not api_url.startswith(("http://", "https://")):
                await update.message.reply_text("❌ Invalid URL.")
                return
            
            context.user_data["shortener_api_url"] = api_url
            context.user_data["configuring_shortener"] = "key"
            
            await update.message.reply_text("✅ API URL saved.\n\nNow send the API Key:")
        
        elif context.user_data.get("configuring_shortener") == "key":
            api_key = update.message.text
            
            await self.db.set_setting("shortener_api_url", context.user_data["shortener_api_url"])
            await self.db.set_setting("shortener_api_key", api_key)
            
            await update.message.reply_text("✅ Shortener API configured successfully!")
            
            del context.user_data["configuring_shortener"]
            del context.user_data["shortener_api_url"]
    
    async def config_shortener_callback(self, query, context):
        """Configure shortener API"""
        await query.answer()
        await query.edit_message_text("⚙️ Configure Shortener API\n\nPlease send the API URL:")
        context.user_data["configuring_shortener"] = "url"
    
    async def edit_caption_callback(self, query, context):
        """Start editing caption"""
        await query.answer()
        await query.edit_message_text("✏️ Edit Auto Caption\n\nSend the new caption text:")
        context.user_data["editing_caption"] = True
    
    async def add_database_channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start adding database channel"""
        await update.message.reply_text("📂 Add Database Channel\n\nSend channel link or forward a message:")
        return ADD_DB_CHANNEL
    
    async def process_add_database_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process database channel addition"""
        if update.message.forward_from_chat:
            chat = update.message.forward_from_chat
            await self.db.add_database_channel(chat.id, chat.username, chat.title)
            await update.message.reply_text(f"✅ Channel '{chat.title}' added!")
        else:
            link = update.message.text
            parsed = Validators.parse_telegram_link(link)
            if parsed:
                await self.db.add_database_channel(parsed.get("channel_id", 0), parsed.get("username", ""), "Channel")
                await update.message.reply_text("✅ Channel added!")
            else:
                await update.message.reply_text("❌ Invalid channel link.")
        
        return ConversationHandler.END
    
    async def cancel_admin_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel admin action"""
        await update.message.reply_text("❌ Action cancelled.")
        
        keys = ["editing_caption", "configuring_shortener", "shortener_api_url"]
        for key in keys:
            if key in context.user_data:
                del context.user_data[key]
        
        return ConversationHandler.END
    
    # ==================== CLEANUP SCHEDULER ====================
    async def start_cleanup_scheduler(self):
        """Start periodic cleanup tasks"""
        while True:
            await asyncio.sleep(3600)  # 1 hour
            try:
                await self.db.cleanup_expired_sessions()
                await self.db.cleanup_old_tokens()
                print(f"✅ Cleanup completed at {datetime.now()}")
            except Exception as e:
                print(f"❌ Cleanup error: {e}")

# ==================== MAIN FUNCTION ====================
async def main():
    """Start the bot"""
    # Create bot instance
    bot = TelegramBot()
    await bot.initialize()
    
    # Start cleanup scheduler
    asyncio.create_task(bot.start_cleanup_scheduler())
    
    # Create application
    application = Application.builder().token(config.BOT_TOKEN).build()
    
    # Initialize alert system
    bot.alert_system = AlertSystem(application.bot, config.ADMIN_IDS)
    
    # Setup handlers
    bot.setup_handlers(application)
    
    # Start polling
    print("🤖 Bot is starting...")
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
