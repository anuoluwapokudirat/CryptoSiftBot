import os
import logging
import asyncio
import hashlib
import sqlite3
import random
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional
import aiohttp
import feedparser
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Configuration ---
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set!")

# Database file
DB_FILE = "crypto_bot.db"

# Free RSS feeds for crypto news (no API keys needed)
RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://cryptoslate.com/feed/",
    "https://dailyhodl.com/feed/",
    "https://coincodex.com/feed.rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cryptopotato.com/feed/",
    "https://www.newsbtc.com/feed/",
]

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Setup ---
def init_database():
    """Initialize SQLite database with required tables."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Table for subscribed channels/groups
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscribed_chats (
            chat_id INTEGER PRIMARY KEY,
            chat_type TEXT,
            title TEXT,
            subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
    ''')
    
    # Table for posted articles (to avoid duplicates)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posted_articles (
            article_hash TEXT PRIMARY KEY,
            title TEXT,
            link TEXT,
            posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Table for daily post count per chat
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_posts (
            chat_id INTEGER,
            post_date DATE,
            post_count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, post_date),
            FOREIGN KEY (chat_id) REFERENCES subscribed_chats(chat_id)
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully.")

# --- Database Helper Functions ---
def add_subscribed_chat(chat_id: int, chat_type: str, title: str = None):
    """Add a chat to the subscription list."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR REPLACE INTO subscribed_chats (chat_id, chat_type, title, is_active) VALUES (?, ?, ?, 1)",
            (chat_id, chat_type, title)
        )
        conn.commit()
        logger.info(f"Added chat {chat_id} ({title}) to subscriptions")
    except Exception as e:
        logger.error(f"Error adding chat {chat_id}: {e}")
    finally:
        conn.close()

def remove_subscribed_chat(chat_id: int):
    """Remove a chat from the subscription list."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM subscribed_chats WHERE chat_id = ?",
            (chat_id,)
        )
        conn.commit()
        logger.info(f"Removed chat {chat_id} from subscriptions")
    except Exception as e:
        logger.error(f"Error removing chat {chat_id}: {e}")
    finally:
        conn.close()

def get_all_subscribed_chats() -> List[int]:
    """Get all active subscribed chat IDs."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT chat_id FROM subscribed_chats WHERE is_active = 1"
        )
        chats = [row[0] for row in cursor.fetchall()]
        return chats
    except Exception as e:
        logger.error(f"Error fetching subscribed chats: {e}")
        return []
    finally:
        conn.close()

def is_article_posted(article_hash: str) -> bool:
    """Check if an article has already been posted."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT 1 FROM posted_articles WHERE article_hash = ?",
            (article_hash,)
        )
        return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Error checking article: {e}")
        return False
    finally:
        conn.close()

def mark_article_posted(article_hash: str, title: str, link: str):
    """Mark an article as posted."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR REPLACE INTO posted_articles (article_hash, title, link) VALUES (?, ?, ?)",
            (article_hash, title, link)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error marking article posted: {e}")
    finally:
        conn.close()

def get_daily_post_count(chat_id: int) -> int:
    """Get the number of posts sent to a chat today."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    today = datetime.now().date().isoformat()
    try:
        cursor.execute(
            "SELECT post_count FROM daily_posts WHERE chat_id = ? AND post_date = ?",
            (chat_id, today)
        )
        result = cursor.fetchone()
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"Error getting daily post count: {e}")
        return 0
    finally:
        conn.close()

def increment_daily_post_count(chat_id: int):
    """Increment the daily post count for a chat."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    today = datetime.now().date().isoformat()
    try:
        cursor.execute(
            "INSERT INTO daily_posts (chat_id, post_date, post_count) VALUES (?, ?, 1) "
            "ON CONFLICT(chat_id, post_date) DO UPDATE SET post_count = post_count + 1",
            (chat_id, today)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error incrementing daily post count: {e}")
    finally:
        conn.close()

def cleanup_old_articles(days: int = 30):
    """Remove articles older than specified days to keep database small."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        cursor.execute(
            "DELETE FROM posted_articles WHERE posted_at < ?",
            (cutoff,)
        )
        conn.commit()
        logger.info(f"Cleaned up articles older than {days} days")
    except Exception as e:
        logger.error(f"Error cleaning up articles: {e}")
    finally:
        conn.close()

# --- News Fetching Functions ---
def get_article_hash(article: Dict) -> str:
    """Generate a unique hash for an article based on title and link."""
    unique_string = f"{article.get('title', '')}{article.get('link', '')}"
    return hashlib.md5(unique_string.encode('utf-8')).hexdigest()

def is_article_relevant(article: Dict) -> bool:
    """Filter out non-crypto or irrelevant articles using keywords."""
    crypto_keywords = [
        'bitcoin', 'btc', 'ethereum', 'eth', 'crypto', 'blockchain', 
        'token', 'coin', 'defi', 'nft', 'web3', 'altcoin', 'mining',
        'wallet', 'exchange', 'bull', 'bear', 'market', 'price'
    ]
    title = article.get('title', '').lower()
    summary = article.get('summary', '').lower()
    
    # Check if title or summary contains any relevant keyword
    if any(keyword in title for keyword in crypto_keywords):
        return True
    if any(keyword in summary for keyword in crypto_keywords):
        return True
    return False

async def fetch_rss_feed(session: aiohttp.ClientSession, url: str) -> List[Dict]:
    """Fetch and parse a single RSS feed asynchronously."""
    try:
        async with session.get(url, timeout=10) as response:
            if response.status != 200:
                logger.warning(f"Failed to fetch {url}: HTTP {response.status}")
                return []
            
            text = await response.text()
            feed = feedparser.parse(text)
            articles = []
            for entry in feed.entries[:15]:  # Get more articles from each feed
                # Clean the summary
                summary = entry.get('summary', '')
                if summary:
                    # Remove HTML tags
                    import re
                    summary = re.sub(r'<[^>]+>', '', summary)
                    summary = summary[:300] + '...' if len(summary) > 300 else summary
                
                article = {
                    'title': entry.get('title', 'No Title'),
                    'link': entry.get('link', ''),
                    'published': entry.get('published', ''),
                    'summary': summary,
                    'source': feed.feed.get('title', url),
                }
                articles.append(article)
            return articles
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return []

async def get_all_news() -> List[Dict]:
    """Fetch news from all configured RSS feeds."""
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_rss_feed(session, feed_url) for feed_url in RSS_FEEDS]
        results = await asyncio.gather(*tasks)
    
    # Flatten list of articles
    all_articles = []
    for articles in results:
        all_articles.extend(articles)
    return all_articles

def filter_new_articles(articles: List[Dict]) -> List[Dict]:
    """Filter out articles that have already been posted or are irrelevant."""
    new_articles = []
    for article in articles:
        if not article['link']:  # Skip articles without a link
            continue
        
        article_hash = get_article_hash(article)
        if is_article_posted(article_hash):
            continue
        
        if not is_article_relevant(article):
            continue
        
        new_articles.append(article)
    
    return new_articles

# --- Posting Functions ---
async def post_article_to_chat(bot: Bot, chat_id: int, article: Dict):
    """Post a single article to a chat."""
    try:
        # Format the message
        message = (
            f"📰 *{article['title']}*\n\n"
            f"📌 Source: {article['source']}\n"
            f"📝 {article['summary']}\n\n"
            f"🔗 [Read More]({article['link']})"
        )
        
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode='Markdown',
            disable_web_page_preview=False
        )
        
        # Mark the article as posted
        article_hash = get_article_hash(article)
        mark_article_posted(article_hash, article['title'], article['link'])
        
        # Increment daily post count
        increment_daily_post_count(chat_id)
        
        logger.info(f"Posted article to chat {chat_id}: {article['title']}")
        return True
    except Exception as e:
        logger.error(f"Error posting to chat {chat_id}: {e}")
        return False

async def post_news_to_channels(app: Application):
    """The main background job: fetch news and post to channels."""
    try:
        logger.info("Checking for new crypto news...")
        
        # Get all subscribed chats
        subscribed_chats = get_all_subscribed_chats()
        if not subscribed_chats:
            logger.info("No subscribed chats found.")
            return
        
        # Fetch and filter new articles
        all_articles = await get_all_news()
        new_articles = filter_new_articles(all_articles)
        
        if not new_articles:
            logger.info("No new articles found.")
            return
        
        logger.info(f"Found {len(new_articles)} new articles to potentially post.")
        
        # Shuffle articles to get variety
        random.shuffle(new_articles)
        
        # Post articles to each chat (max 5 per day)
        for chat_id in subscribed_chats:
            daily_count = get_daily_post_count(chat_id)
            posts_needed = max(0, 5 - daily_count)
            
            if posts_needed <= 0:
                logger.info(f"Chat {chat_id} already received 5 posts today.")
                continue
            
            # Post up to 5 articles per day
            articles_to_post = new_articles[:posts_needed]
            posted_count = 0
            
            for article in articles_to_post:
                success = await post_article_to_chat(app.bot, chat_id, article)
                if success:
                    posted_count += 1
                    # Wait a bit between posts to avoid rate limits
                    await asyncio.sleep(2)
            
            logger.info(f"Posted {posted_count} articles to chat {chat_id}")
        
        # Clean up old articles from database weekly
        if datetime.now().weekday() == 0:  # Monday
            cleanup_old_articles(30)
            
    except Exception as e:
        logger.error(f"Error in news posting job: {e}")

async def post_job(context: ContextTypes.DEFAULT_TYPE):
    """Wrapper for the background job."""
    await post_news_to_channels(context.application)

# --- Bot Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    chat = update.effective_chat
    user = update.effective_user
    
    # Check if this is a group/channel or private chat
    if chat.type in ['group', 'supergroup', 'channel']:
        # For groups/channels, subscribe them
        add_subscribed_chat(
            chat_id=chat.id,
            chat_type=chat.type,
            title=chat.title or "Unknown"
        )
        
        await update.message.reply_text(
            f"✅ *CryptoSiftBot Activated!*\n\n"
            f"This chat is now subscribed to receive up to 5 crypto news posts per day.\n\n"
            f"📊 *Stats:*\n"
            f"• Posts per day: 5\n"
            f"• News sources: {len(RSS_FEEDS)}\n\n"
            f"To unsubscribe, use /unsubscribe",
            parse_mode='Markdown'
        )
    else:
        # For private chats, inform the user
        await update.message.reply_text(
            f"👋 Hello {user.first_name}!\n\n"
            f"I'm CryptoSiftBot - your automated crypto news aggregator.\n\n"
            f"📌 *How to use me:*\n"
            f"1. Add me to your channel or group\n"
            f"2. Make me an admin\n"
            f"3. I'll start posting up to 5 crypto news articles daily\n\n"
            f"Commands:\n"
            f"/start - Show this message\n"
            f"/status - Check bot status\n"
            f"/subscribe - Subscribe current chat\n"
            f"/unsubscribe - Unsubscribe current chat",
            parse_mode='Markdown'
        )

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /subscribe command."""
    chat = update.effective_chat
    
    if chat.type in ['group', 'supergroup', 'channel']:
        add_subscribed_chat(
            chat_id=chat.id,
            chat_type=chat.type,
            title=chat.title or "Unknown"
        )
        await update.message.reply_text(
            f"✅ This chat has been subscribed!\nI'll post up to 5 crypto news articles daily."
        )
    else:
        await update.message.reply_text(
            f"Please add me to a group or channel first, then use /subscribe there."
        )

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unsubscribe command."""
    chat = update.effective_chat
    
    if chat.type in ['group', 'supergroup', 'channel']:
        remove_subscribed_chat(chat.id)
        await update.message.reply_text(
            f"✅ This chat has been unsubscribed.\nI will no longer post crypto news here."
        )
    else:
        await update.message.reply_text(
            f"Please use /unsubscribe in the group or channel you want to unsubscribe."
        )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    chat = update.effective_chat
    subscribed_chats = get_all_subscribed_chats()
    
    # Get stats
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM posted_articles")
    total_posts = cursor.fetchone()[0]
    conn.close()
    
    today = datetime.now().date().isoformat()
    chat_daily = get_daily_post_count(chat.id) if chat.id in subscribed_chats else 0
    
    stats = (
        f"📊 *CryptoSiftBot Status*\n\n"
        f"📌 Current Chat: {'✅ Subscribed' if chat.id in subscribed_chats else '❌ Not Subscribed'}\n"
        f"📰 Daily Posts: {chat_daily}/5 (today)\n"
        f"📚 Total Articles Posted: {total_posts}\n"
        f"📡 Active RSS Feeds: {len(RSS_FEEDS)}\n"
        f"👥 Subscribed Chats: {len(subscribed_chats)}\n"
        f"🔄 Check Interval: Every {int(os.environ.get('UPDATE_INTERVAL', 3600))} seconds"
    )
    await update.message.reply_text(stats, parse_mode='Markdown')

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test the bot by posting a test message."""
    chat = update.effective_chat
    await update.message.reply_text(
        f"🧪 *Test Message*\n\n"
        f"This is a test to confirm the bot is working properly in this chat.",
        parse_mode='Markdown'
    )

# --- Main Function ---
async def post_on_startup(app: Application):
    """Post immediately on startup if there are new articles."""
    await asyncio.sleep(10)  # Wait for bot to fully start
    await post_news_to_channels(app)

def main():
    """Start the bot and the background scheduler."""
    # Initialize database
    init_database()
    
    # Create the Application
    application = Application.builder().token(TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("test", test))
    
    # Start the background job that checks for news
    job_queue = application.job_queue
    if job_queue:
        # Run every 60 minutes (or custom interval)
        interval = int(os.environ.get('UPDATE_INTERVAL', 3600))  # Default: 1 hour
        job_queue.run_repeating(post_job, interval=interval, first=30)
        logger.info(f"Background job scheduled every {interval} seconds.")
        
        # Also run on startup
        # job_queue.run_once(post_job, when=20)
    else:
        logger.warning("Job queue not available. News will not be posted automatically.")
    
    # Start the Bot
    print("🚀 CryptoSiftBot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
