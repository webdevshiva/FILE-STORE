# keyboards.py
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def get_force_join_keyboard(channels):
    """Create force-join keyboard with channel buttons"""
    keyboard = []
    
    for channel in channels:
        keyboard.append([
            InlineKeyboardButton(
                f"Join {channel['title']}",
                url=channel['invite_link']
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton("🔄 Recheck Status", callback_data="recheck_join")
    ])
    
    return InlineKeyboardMarkup(keyboard)

def get_verification_keyboard(short_url):
    """Create verification keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("🌐 Open Verification Link", url=short_url),
            InlineKeyboardButton("📋 Copy Link", callback_data="copy_link")
        ],
        [
            InlineKeyboardButton("🔄 Retry Verification", callback_data="retry_verification")
        ]
    ]
    
    return InlineKeyboardMarkup(keyboard)

def get_bypass_keyboard():
    """Keyboard shown when bypass detected"""
    keyboard = [
        [
            InlineKeyboardButton("🌐 Verify Again", callback_data="verify_again"),
            InlineKeyboardButton("📋 Copy Link", callback_data="copy_link")
        ],
        [
            InlineKeyboardButton("🔄 Retry Verification", callback_data="retry_verification")
        ]
    ]
    
    return InlineKeyboardMarkup(keyboard)

def get_shortener_failed_keyboard():
    """Keyboard when shortener API fails"""
    keyboard = [
        [
            InlineKeyboardButton("🔄 Retry Verification", callback_data="retry_verification"),
            InlineKeyboardButton("⏳ Try Later", callback_data="try_later")
        ]
    ]
    
    return InlineKeyboardMarkup(keyboard)

def get_time_left_keyboard():
    """Single button for active session"""
    keyboard = [
        [InlineKeyboardButton("⏱ Time Left", callback_data="time_left")]
    ]
    
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    """Admin main menu"""
    keyboard = [
        [InlineKeyboardButton("🔗 Link Management", callback_data="admin_links")],
        [InlineKeyboardButton("📂 Database Channels", callback_data="admin_channels")],
        [InlineKeyboardButton("🔔 Force-Join Channels", callback_data="admin_force_join")],
        [InlineKeyboardButton("📝 Auto Caption", callback_data="admin_caption")],
        [InlineKeyboardButton("🌐 Short Link System", callback_data="admin_shortener")],
        [InlineKeyboardButton("📊 Analytics", callback_data="admin_analytics")]
    ]
    
    return InlineKeyboardMarkup(keyboard)

def get_batch_result_keyboard(batch_link):
    """Keyboard after batch link generation"""
    keyboard = [
        [InlineKeyboardButton("📋 Copy Link", callback_data=f"copy_{batch_link}")],
        [InlineKeyboardButton("❌ Cancel Batch", callback_data="cancel_batch")]
    ]
    
    return InlineKeyboardMarkup(keyboard)
