import os
import json
import logging
from datetime import datetime, timedelta
import feedparser
import praw
from praw.exceptions import RedditAPIException
from dotenv import load_dotenv
import time
import re

#logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ReddotHelper")

class ConfigManager:
    #loading configurations
    def __init__(self, config_path="config.json"):
        with open(config_path, 'r') as f:
            self.settings = json.load(f)

        # .env if present; silently ignores if missing
        load_dotenv()

        self.client_id = os.environ.get("REDDIT_CLIENT_ID")
        self.client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
        self.username = os.environ.get("REDDIT_USERNAME")
        self.password = os.environ.get("REDDIT_PASSWORD")
        self.user_agent = os.environ.get("REDDIT_USER_AGENT", f"script:rss-bot:v1.0 (by u/{self.username})")

        if not all([self.client_id, self.client_secret, self.username, self.password]):
            raise ValueError("Missing Reddit credentials. Ensure .env is set up or environment variables are provided.")

class HistoryManager:
    #history to prevent duplicate posts
    def __init__(self, history_file, retention_days):
        self.history_file = history_file
        self.retention_days = retention_days
        self.history = self._load_history()

    def _load_history(self):
        if os.path.exists(self.history_file):
            with open(self.history_file, 'r') as f:
                return json.load(f)
        return {}

    def save_history(self):
        with open(self.history_file, 'w') as f:
            json.dump(self.history, f, indent=4)

    def is_posted(self, url):
        return url in self.history

    def mark_posted(self, url):
        self.history[url] = datetime.utcnow().isoformat()

    def prune_old_entries(self):
        #Removes older entries
        cutoff_date = datetime.utcnow() - timedelta(days=self.retention_days)
        pruned_history = {}

        for url, timestamp_str in self.history.items():
            entry_date = datetime.fromisoformat(timestamp_str)
            if entry_date > cutoff_date:
                pruned_history[url] = timestamp_str

        self.history = pruned_history
        self.save_history()

class RSSFetcher:
    #fetch, parss, and filter the RSS
    def __init__(self, rss_config):
        self.feed_url = rss_config["feed_url"]

        # keywords to lowercase for case-insensitive matching
        self.include_keywords = [k.lower() for k in rss_config.get("include_keywords", [])]
        self.exclude_keywords = [k.lower() for k in rss_config.get("exclude_keywords", [])]

    def get_latest_entries(self):
        logger.info(f"Fetching RSS feed: {self.feed_url}")
        feed = feedparser.parse(self.feed_url)

        if feed.bozo:
            logger.error("Failed to parse RSS feed.")
            return []

        filtered_entries = []
        for entry in feed.entries:
            title_lower = entry.title.lower()

            # 1. Skip if title contains ANY excluded keyword
            if any(keyword in title_lower for keyword in self.exclude_keywords):
                logger.info(f"Skipped (exclude match): {entry.title}")
                continue

            # 2. Skip if include list has items and title contains NONE of them
            if self.include_keywords and not any(keyword in title_lower for keyword in self.include_keywords):
                logger.info(f"Skipped (no include match): {entry.title}")
                continue

            filtered_entries.append({"title": entry.title, "url": entry.link})

        return filtered_entries

class RedditPoster:
    #Handles Reddit API authentication and posting with rate limit management.
    def __init__(self, config):
        self.reddit = praw.Reddit(
            client_id=config.client_id,
            client_secret=config.client_secret,
            user_agent=config.user_agent,
            username=config.username,
            password=config.password
        )
        self.subreddit_name = config.settings["reddit"]["subreddit"]

    def post_link(self, title, url, max_retries=2):
        subreddit = self.reddit.subreddit(self.subreddit_name)

        for attempt in range(max_retries + 1):
            try:
                submission = subreddit.submit(title=title, url=url)
                logger.info(f"Successfully posted: {title} ({submission.shortlink})")
                return True

            except RedditAPIException as e:
                rate_limit_hit = False

                for sub_exception in e.items:
                    if sub_exception.error_type == "RATELIMIT":
                        rate_limit_hit = True

                        # Extract the wait time from the Reddit error message
                        match = re.search(r'(\d+)\s+(minute|second)', sub_exception.message)

                        if match:
                            amount = int(match.group(1))
                            unit = match.group(2)
                            wait_seconds = amount * 60 if 'minute' in unit else amount

                            logger.warning(f"Rate limit hit. Sleeping for {wait_seconds} seconds (Attempt {attempt + 1}/{max_retries})")
                            time.sleep(wait_seconds + 5)  # Buffer added
                            break
                        else:
                            logger.warning("Rate limit hit, but couldn't parse wait time. Sleeping for 60 seconds.")
                            time.sleep(60)
                            break
                    else:
                        logger.error(f"Reddit API Error: {sub_exception.error_type} - {sub_exception.message}")

                if not rate_limit_hit:
                    return False

            except Exception as e:
                logger.error(f"Unexpected error posting to Reddit: {e}")
                return False

        logger.error(f"Failed to post '{title}' after {max_retries} rate limit retries.")
        return False

def main():
    logger.info("Starting RSS to Reddit Bot")

    try:
        config = ConfigManager()
        history = HistoryManager(
            config.settings["app"]["history_file"],
            config.settings["app"]["history_retention_days"]
        )
        rss = RSSFetcher(config.settings["rss"])
        reddit = RedditPoster(config)

        entries = rss.get_latest_entries()
        posts_made = 0
        max_posts = config.settings["app"]["max_posts_per_run"]

        # Iterate through feed entries
        for entry in entries:
            if posts_made >= max_posts:
                logger.info(f"Reached max posts per run ({max_posts}). Stopping.")
                break

            if not history.is_posted(entry["url"]):
                logger.info(f"Attempting to post: {entry['title']}")
                success = reddit.post_link(entry["title"], entry["url"])

                if success:
                    history.mark_posted(entry["url"])
                    posts_made += 1
                else:
                    logger.warning("Aborting further posts this run due to API errors.")
                    break

        history.prune_old_entries()
        logger.info("Run complete.")

    except Exception as e:
        logger.critical(f"Critical execution failure: {e}")

if __name__ == "__main__":
    main()
