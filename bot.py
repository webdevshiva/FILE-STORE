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
from utils import ShortenerAPI, Validators, CaptionManager, AlertSystem, TimeUtils, MembershipChecker

import os
import logging
import asyncio
from threading import Thread

# Render ke liye port settings
PORT = int(os.environ.get('PORT', 8080))

# Flask app for health checks (optional but recommended)
try:
    from flask import Flask
    app = Flask('')
    @app.route('/')
    def home():
        return "🤖 Bot is running on Render"
    
    def run_flask():
        app.run(host='0.0.0.0', port=PORT)
    
    # Start Flask in background thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logging.info(f"Flask health check running on port {PORT}")
except ImportError:
    pass

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

class TelegramBot:
    def __init__(self):
        self.db = Database(config.DATABASE_PATH)
        self.shortener = ShortenerAPI()
        self.caption_manager = None
        self.alert_system = None
        self.time_utils = TimeUtils()
        self.membership_checker = MembershipChecker()
        self.user_requests = defaultdict(list)  # For rate limiting
        
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
            if (now - req_time).seconds < 60  # Keep last minute only
        ]
        
        # Check limits
        if len(self.user_requests[user_id]) >= 20:  # 20 requests per minute
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
            # No force join required, go directly to verification
            await self.start_verification(update, context, link_id, is_batch)
            return
        
        # Check actual membership
        membership_results = await self.membership_checker.check_all_memberships(
            context.bot, user.id, channels
        )
        
        all_joined = all(channel['is_member'] for channel in membership_results)
        
        if all_joined:
            # All channels joined, proceed to verification
            await self.start_verification(update, context, link_id, is_batch)
        else:
            # Show channels to join
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
            
            # Store link_id in context for later use
            context.user_data["pending_link"] = link_id
            context.user_data["is_batch"] = is_batch
    
    async def handle_recheck_join(self, query, context):
        """Handle recheck join status with actual membership check"""
        await query.answer("Checking your status...")
        
        user = query.from_user
        link_id = context.user_data.get("pending_link")
        is_batch = context.user_data.get("is_batch", False)
        channels = await self.db.get_force_join_channels()
        
        # Check actual membership
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
            # Update button status
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
    
    # ==================== FILE COPYING WITH CAPTION ====================
    async def send_single_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                              link_info: Dict, user_id: int):
        """Send single file to user with auto caption"""
        try:
            # Get original message
            original_msg = await context.bot.copy_message(
                chat_id=update.effective_chat.id,
                from_chat_id=link_info["channel_id"],
                message_id=link_info["message_id"]
            )
            
            # Apply auto caption if enabled
            auto_caption = await self.db.get_setting("auto_caption_enabled", "0")
            if auto_caption == "1":
                caption = await self.caption_manager.apply_caption(
                    original_caption=original_msg.caption or "",
                    user_id=user_id,
                    file_name=link_info.get("file_name", "File"),
                    expiry_time=datetime.now().strftime("%Y-%m-%d %H:%M")
                )
                
                if caption != (original_msg.caption or ""):
                    # Edit caption
                    await original_msg.edit_caption(caption=caption)
            
            # Show unlimited access message
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
            
            # Check auto caption setting
            auto_caption = await self.db.get_setting("auto_caption_enabled", "0")
            
            # Send files in batches
            sent_count = 0
            for msg_id in range(start_id, end_id + 1):
                try:
                    # Copy message
                    msg = await context.bot.copy_message(
                        chat_id=update.effective_chat.id,
                        from_chat_id=channel_id,
                        message_id=msg_id
                    )
                    
                    # Apply auto caption if enabled
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
                    
                    # Rate limiting delay
                    if sent_count % 10 == 0:
                        await asyncio.sleep(1)
                    
                except Exception as e:
                    logger.error(f"Error copying message {msg_id}: {e}")
                    continue
            
            # Show unlimited access message
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
    
    # ==================== COMPLETE ADMIN PANEL ====================
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
    
    async def show_link_management(self, query):
        """Show link management interface"""
        # Get recent links
        links = await self.db.get_recent_links(limit=10)
        
        text = "🔗 Link Management\n\n"
        text += "Recent Links:\n"
        
        for link in links:
            text += f"• ID: {link['link_id'][:8]} | Uses: {link['uses']} | Type: {link['link_type']}\n"
        
        keyboard = [
            [InlineKeyboardButton("📊 Link Analytics", callback_data="link_analytics")],
            [InlineKeyboardButton("🗑 Delete Expired", callback_data="delete_expired")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]
        ]
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_database_channels(self, query):
        """Show database channels management"""
        channels = await self.db.get_all_database_channels()
        
        text = "📂 Database Channels\n\n"
        
        if not channels:
            text += "No database channels added.\n"
        else:
            for channel in channels:
                text += f"• {channel['title']} (ID: {channel['channel_id']})\n"
        
        keyboard = [
            [InlineKeyboardButton("➕ Add Channel", callback_data="add_db_channel")],
            [InlineKeyboardButton("🗑 Remove Channel", callback_data="remove_db_channel")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]
        ]
        
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
        
        keyboard = [
            [InlineKeyboardButton("➕ Add Channel", callback_data="add_force_join")],
            [InlineKeyboardButton("⚙️ Toggle Status", callback_data="toggle_force_join")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]
        ]
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    async def show_caption_management(self, query):
        """Show auto-caption management"""
        caption_enabled = await self.db.get_setting("auto_caption_enabled", "0")
        current_caption = await self.db.get_setting("auto_caption", "")
        
        text = "📝 Auto Caption Management\n\n"
        text += f"Status: {'✅ Enabled' if caption_enabled == '1' else '❌ Disabled'}\n\n"
        text += "Current Caption:\n"
        text += f"<code>{current_caption[:200]}</code>\n\n" if current_caption else "Not set\n\n"
        text += "Available placeholders:\n"
        text += "<code>{file_name}</code> - File name\n"
        text += "<code>{batch_name}</code> - Batch name\n"
        text += "<code>{user_id}</code> - User ID\n"
        text += "<code>{expiry_time}</code> - Session expiry\n"
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Enable" if caption_enabled == '0' else "❌ Disable", 
                                   callback_data="toggle_caption"),
                InlineKeyboardButton("✏️ Edit Caption", callback_data="edit_caption")
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]
        ]
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), 
                                     parse_mode="HTML")
    
    async def show_shortener_config(self, query):
        """Show shortener configuration"""
        api_url = await self.db.get_setting("shortener_api_url", "Not set")
        api_key = await self.db.get_setting("shortener_api_key", "Not set")
        
        text = "🌐 Short Link System\n\n"
        text += f"API URL: <code>{api_url[:50]}...</code>\n" if api_url != "Not set" else "API URL: Not set\n"
        text += f"API Key: <code>{api_key[:10]}***</code>\n\n" if api_key != "Not set" else "API Key: Not set\n\n"
        
        # Test status
        if api_url != "Not set":
            text += "Test Status: "
            # Test API
            test_url = await self.shortener.shorten_url("https://example.com")
            if test_url:
                text += "✅ Working\n"
            else:
                text += "❌ Failed\n"
        
        keyboard = [
            [InlineKeyboardButton("⚙️ Configure API", callback_data="config_shortener")],
            [InlineKeyboardButton("🔄 Test Connection", callback_data="test_shortener")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]
        ]
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), 
                                     parse_mode="HTML")
    
    async def show_analytics_dashboard(self, query):
        """Show analytics dashboard"""
        # Get analytics data
        total_users = await self.db.get_total_users()
        active_sessions = await self.db.get_active_sessions_count()
        today_verifications = await self.db.get_today_verifications()
        bypass_attempts = await self.db.get_bypass_attempts_count(days=7)
        
        text = "📊 Analytics Dashboard\n\n"
        text += f"👥 Total Users: {total_users}\n"
        text += f"🔓 Active Sessions: {active_sessions}\n"
        text += f"✅ Today's Verifications: {today_verifications}\n"
        text += f"🚫 Bypass Attempts (7 days): {bypass_attempts}\n\n"
        
        # Popular links
        popular_links = await self.db.get_popular_links(limit=5)
        if popular_links:
            text += "🔥 Popular Links:\n"
            for link in popular_links:
                text += f"• {link['link_id'][:8]}: {link['uses']} uses\n"
        
        keyboard = [
            [InlineKeyboardButton("📈 Detailed Report", callback_data="detailed_analytics")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="admin_analytics")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]
        ]
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ==================== ADMIN CRUD OPERATIONS ====================
    async def add_database_channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start adding database channel"""
        await update.message.reply_text(
            "📂 Add Database Channel\n\n"
            "Please send:\n"
            "1. Channel link (t.me/username)\n"
            "2. Or forward a message from the channel"
        )
        return ADD_DB_CHANNEL
    
    async def process_add_database_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process database channel addition"""
        if update.message.forward_from_chat:
            # Forwarded message
            chat = update.message.forward_from_chat
            channel_id = chat.id
            username = chat.username
            title = chat.title
        else:
            # Channel link
            link = update.message.text
            parsed = Validators.parse_telegram_link(link)
            if not parsed:
                await update.message.reply_text("❌ Invalid channel link. Try again.")
                return ADD_DB_CHANNEL
            
            # Get channel info
            try:
                chat = await context.bot.get_chat(parsed.get('username', parsed.get('channel_id')))
                channel_id = chat.id
                username = chat.username
                title = chat.title
            except Exception as e:
                await update.message.reply_text(f"❌ Error: {e}. Make sure bot is admin in channel.")
                return ADD_DB_CHANNEL
        
        # Save to database
        await self.db.add_database_channel(channel_id, username or "", title or "")
        
        await update.message.reply_text(f"✅ Channel '{title}' added successfully!")
        return ConversationHandler.END
    
    # ==================== CAPTION MANAGEMENT ====================
    async def edit_caption_callback(self, query, context):
        """Start editing caption"""
        await query.answer()
        await query.edit_message_text(
            "✏️ Edit Auto Caption\n\n"
            "Send the new caption text.\n"
            "Use placeholders: {file_name}, {batch_name}, {user_id}, {expiry_time}\n\n"
            "Current caption will be replaced."
        )
        context.user_data["editing_caption"] = True
    
    async def process_caption_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process caption edit"""
        if context.user_data.get("editing_caption"):
            new_caption = update.message.text
            
            # Validate length
            if len(new_caption) > 1000:
                await update.message.reply_text("❌ Caption too long (max 1000 chars).")
                return
            
            # Save to database
            await self.db.set_setting("auto_caption", new_caption)
            await update.message.reply_text("✅ Caption updated successfully!")
            
            del context.user_data["editing_caption"]
    
    # ==================== SHORTENER CONFIGURATION ====================
    async def config_shortener_callback(self, query, context):
        """Configure shortener API"""
        await query.answer()
        await query.edit_message_text(
            "⚙️ Configure Shortener API\n\n"
            "Please send the API URL (one message):"
        )
        context.user_data["configuring_shortener"] = "url"
    
    async def process_shortener_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process shortener configuration"""
        if context.user_data.get("configuring_shortener") == "url":
            api_url = update.message.text
            
            # Validate URL
            if not api_url.startswith(("http://", "https://")):
                await update.message.reply_text("❌ Invalid URL. Must start with http:// or https://")
                return
            
            context.user_data["shortener_api_url"] = api_url
            context.user_data["configuring_shortener"] = "key"
            
            await update.message.reply_text(
                "✅ API URL saved.\n\n"
                "Now send the API Key:"
            )
        
        elif context.user_data.get("configuring_shortener") == "key":
            api_key = update.message.text
            
            # Save to database
            await self.db.set_setting("shortener_api_url", context.user_data["shortener_api_url"])
            await self.db.set_setting("shortener_api_key", api_key)
            
            # Update config
            import config
            config.SHORTENER_API_URL = context.user_data["shortener_api_url"]
            config.SHORTENER_API_KEY = api_key
            
            await update.message.reply_text(
                "✅ Shortener API configured successfully!\n"
                "Use /admin to test the connection."
            )
            
            # Clean up
            del context.user_data["configuring_shortener"]
            del context.user_data["shortener_api_url"]
    
    # ==================== VERIFICATION LANDING PAGE HANDLER ====================
    async def handle_verification_webhook(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle verification from web page (simulated)"""
        # This would be called from your web server
        # For now, we'll simulate it with a command
        
        if len(context.args) != 1:
            await update.message.reply_text("❌ Invalid verification link.")
            return
        
        token = context.args[0]
        
        # Find user by token
        user_id = await self.db.get_user_by_token(token)
        if not user_id:
            await update.message.reply_text("❌ Invalid or expired token.")
            return
        
        # Check if it's the current user
        if update.effective_user.id != user_id:
            await update.message.reply_text("❌ This verification link is for a different user.")
            return
        
        # Process verification
        await self.process_verification_completion(update, token)
    
    async def process_verification_completion(self, update: Update, token: str):
        """Process verification completion from web"""
        user = update.effective_user
        
        # Verify token
        is_valid, is_bypassed = await self.db.verify_token(token, user.id, datetime.now())
        
        if not is_valid:
            await update.message.reply_text("❌ Invalid verification. Please try again.")
            return
        
        if is_bypassed:
            await update.message.reply_text(
                "❌ Verification too fast! Bypass detected.\n"
                "Please complete the verification properly."
            )
            return
        
        # Create session
        session_id = await self.db.create_session(user.id, config.SESSION_DURATION)
        
        await update.message.reply_text(
            "✅ Verification Successful!\n"
            "⏰ Unlimited access enabled for 6 hours\n\n"
            "You can now use any valid file or batch link."
        )
        
        # Log event
        await self.db.log_event("web_verification_success", user.id, {"session_id": session_id})
    
    # ==================== MAIN HANDLERS ====================
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command with rate limiting"""
        user = update.effective_user
        
        # Rate limit check
        allowed, message = await self.check_rate_limit(user.id)
        if not allowed:
            await update.message.reply_text(message)
            return
        
        await self.db.get_or_create_user(user.id, user.username, user.full_name)
        
        # Check for link parameter
        args = context.args
        if not args:
            await update.message.reply_text("👋 Welcome! Access files using admin-generated links only.")
            return
        
        param = args[0]
        
        # Handle different start parameters
        if param.startswith("verify_"):
            await self.handle_verification_webhook(update, context)
        elif param.startswith("batch_"):
            await self.handle_batch_link(update, context, param[6:])
        else:
            await self.handle_single_link(update, context, param)
    
    async def handle_single_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE, link_id: str):
        """Handle single file link access with rate limiting"""
        user = update.effective_user
        
        # Rate limit check
        allowed, message = await self.check_rate_limit(user.id, "link_access")
        if not allowed:
            await update.message.reply_text(message)
            return
        
        # Log access attempt
        await self.db.log_event("link_access", user.id, {"link_id": link_id})
        
        # Check active session
        session = await self.db.get_active_session(user.id)
        if session:
            # Active session - send file directly
            link_info = await self.db.get_link_info(link_id)
            if not link_info:
                await update.message.reply_text("❌ Invalid or expired link.")
                return
            
            await self.send_single_file(update, context, link_info, user.id)
            await self.db.increment_link_uses(link_id)
            return
        
        # No active session - check force join
        await self.check_force_join(update, context, link_id)
    
    async def start_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                link_id: str, is_batch: bool = False):
        """Start verification process with rate limiting"""
        user = update.effective_user
        
        # Rate limit check
        allowed, message = await self.check_rate_limit(user.id, "verification")
        if not allowed:
            await update.message.reply_text(message)
            return
        
        # Create verification token
        token = await self.db.create_verification_token(user.id)
        
        # Generate verification URL for web page
        # In production, this points to your verification landing page
        verification_url = f"https://your-domain.com/verify/{token}"
        
        # Shorten URL
        short_url = await self.shortener.shorten_url(verification_url)
        
        if not short_url:
            # Shortener API failed
            await self.handle_shortener_failure(update, user.id)
            return
        
        # Store data
        context.user_data["pending_link"] = link_id
        context.user_data["is_batch"] = is_batch
        context.user_data["verification_token"] = token
        
        # Send verification message
        keyboard = get_verification_keyboard(short_url)
        
        await update.message.reply_text(
            "🔐 Verify & Get Unlimited Access\n\n"
            "Unlock unlimited files & batches for the next 6 hours.\n\n"
            "⚠️ Open the link properly and wait 35+ seconds.\n"
            "Bypass attempts are detected.",
            reply_markup=keyboard
        )
    
    def setup_handlers(self, application):
        """Setup all bot handlers"""
        # Start command
        application.add_handler(CommandHandler("start", self.start_command))
        
        # Admin commands
        application.add_handler(CommandHandler("admin", self.admin_command))
        
        # Admin conversation handlers
        admin_conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.config_shortener_callback, pattern="^config_shortener$"),
                CallbackQueryHandler(self.edit_caption_callback, pattern="^edit_caption$"),
                CommandHandler("addchannel", self.add_database_channel_command)
            ],
            states={
                ADD_DB_CHANNEL: [MessageHandler(filters.TEXT | filters.FORWARDED, self.process_add_database_channel)],
                SET_CAPTION: [MessageHandler(filters.TEXT, self.process_caption_edit)],
                SET_SHORTENER: [MessageHandler(filters.TEXT, self.process_shortener_config)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel_admin_action)],
        )
        application.add_handler(admin_conv)
        
        # Batch creation conversation
        batch_conv = ConversationHandler(
            entry_points=[CommandHandler("batch", self.batch_command)],
            states={
                ASK_START_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.ask_start_msg)],
                ASK_END_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.ask_end_msg)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_batch)],
        )
        application.add_handler(batch_conv)
        
        # Callback queries
        application.add_handler(CallbackQueryHandler(self.callback_handler))
        
        # Message handlers for admin edits
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            self.handle_admin_messages
        ))
        
        # Unknown commands
        application.add_handler(MessageHandler(filters.COMMAND, self.unknown_command))
    
    async def handle_admin_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle admin text messages for configurations"""
        if update.effective_user.id not in config.ADMIN_IDS:
            return
        
        if context.user_data.get("editing_caption"):
            await self.process_caption_edit(update, context)
        elif context.user_data.get("configuring_shortener"):
            await self.process_shortener_config(update, context)
    
    async def cancel_admin_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel admin action"""
        await update.message.reply_text("❌ Action cancelled.")
        
        # Clean up
        keys = ["editing_caption", "configuring_shortener", "shortener_api_url"]
        for key in keys:
            if key in context.user_data:
                del context.user_data[key]
        
        return ConversationHandler.END

async def start_cleanup_scheduler(self):
    """Start periodic cleanup tasks"""
    while True:
        await asyncio.sleep(3600)  # 1 hour
        try:
            await self.db.cleanup_expired_sessions()
            await self.db.cleanup_old_tokens()
            await self.db.cleanup_old_analytics(30)
            print(f"✅ Cleanup completed at {datetime.now()}")
        except Exception as e:
            print(f"❌ Cleanup error: {e}")

async def main():
    """Start the bot"""
    # Create bot instance
    bot = TelegramBot()
    await bot.initialize()

    # ✅ Start cleanup scheduler
    asyncio.create_task(bot.start_cleanup_scheduler())
    
    # Create application
    application = Application.builder().token(config.BOT_TOKEN).build()
    
    # Initialize alert system
    bot.alert_system = AlertSystem(application.bot, config.ADMIN_IDS)
    
    # Setup handlers
    bot.setup_handlers(application)
    
    # Start polling
    print("🤖 Bot is starting with ALL features...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    # Keep running
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())