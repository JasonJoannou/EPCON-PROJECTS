import os
import sqlite3
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from apscheduler.schedulers.background import BackgroundScheduler
from google import genai
from typing import List
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file


app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    token_verification_enabled=False 
)
TEST_MODE = True  # Set to False for production
CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID") if not TEST_MODE else os.environ.get("SLACK_CHANNEL_ID_TEST")
MY_SLACK_ID = os.environ.get("MY_SLACK_ID") if TEST_MODE else None


class CatchupBot:

    def __init__(self):
        self._init_db()
        self.client = genai.Client(api_key=os.environ.get("GENAI_API_KEY"))
        self.workspace_members = self._get_team_members(CHANNEL_ID)

    def _init_db(self):
        with sqlite3.connect("standups.db") as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS updates (
                    date TEXT, user_id TEXT, morning_plan TEXT, afternoon_done TEXT,
                    PRIMARY KEY (date, user_id)
                )
            """)

    def _get_team_members(
        self, channel_id: str
    ) -> List[str]:  # Channel Id defaults to #epcon-daily-standup
        result = app.client.conversations_members(channel=channel_id)
        return result["members"]

    def _prepare_prompt(
        self,
        user_input: str,
        time_period: str = "morning",
        context: str = "",
        yesterday_context: str = "",
    ) -> str:
        role = "You are a professional Agile Team Coordinator. Your goal is to synthesize messy, long-form employee updates into clear, actionable Slack summaries."

        comparison = (
            f"\nYesterday's reported progress was: {yesterday_context}"
            if yesterday_context
            else ""
        )

        if time_period == "morning":
            return f"""
            {role}
            {comparison}
            
            USER INPUT: {user_input}

            OUTPUT FORMAT:
            *Morning Plan:*
            • *Primary Focus:* [1-sentence summary of the main goal]
            • *Key Tasks:* [List 2-3 specific high-level tasks]
            • *Yesterday Delta:* [Briefly note if they are continuing yesterday's work or starting fresh]
            """
        else:
            return f"""
            {role}
            The employee's plan for TODAY was: {context}.
            
            THEY NOW REPORT: {user_input}

            OUTPUT FORMAT:
            *Evening Recap:*
            • *Status:* [Complete / Partial / Blocked]
            • *Summary:* [2-sentence summary of what actually happened]
            • *Comparison:* [How this aligns with the morning plan]
            """

    def get_yesterday_context(self, user_id):
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        with sqlite3.connect("standups.db") as conn:
            res = conn.execute("SELECT afternoon_done FROM updates WHERE date=? AND user_id=?", 
                               (yesterday, user_id)).fetchone()
        return res[0] if res else "No previous data."

    def get_morning_plan(self, user_id):
        today = datetime.now().strftime('%Y-%m-%d')
        with sqlite3.connect("standups.db") as conn:
            res = conn.execute("SELECT morning_plan FROM updates WHERE date=? AND user_id=?", 
                               (today, user_id)).fetchone()
        return res[0] if res else "No plan recorded."
    
    def save_update(self, user_id, text, column):
        today = datetime.now().strftime('%Y-%m-%d')
        with sqlite3.connect("standups.db") as conn:
            query = f"INSERT INTO updates (date, user_id, {column}) VALUES (?, ?, ?) " \
                    f"ON CONFLICT(date, user_id) DO UPDATE SET {column}=excluded.{column}"
            conn.execute(query, (today, user_id, text))


    def summarise_update(self, user_input: str, time_period: str, context: str = "", yesterday_context: str = "") -> str:
        prompt = self._prepare_prompt(user_input, time_period, context, yesterday_context)
        try:
            response = self.client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt
            )
            return response.text
        except Exception as e:
            print(f"AI Error: {e}")
            return f"Summary unavailable. Raw input: {user_input}"
    

catchup_bot = CatchupBot()

@app.event("message")
def handle_message(event, client):
    print(f"DEBUG: Received a message from {event.get('user')}") # ADD THIS
    # Only process DMs from humans
    if event.get("channel_type") != "im" or event.get("bot_id"):
        print("DEBUG: Ignored (not a DM or is a bot)")
        return

    user_id = event["user"]
    raw_text = event["text"]
    
    # Mode logic: 2 PM cutoff
    mode = "morning" if datetime.now().hour < 14 else "afternoon"
    column = "morning_plan" if mode == "morning" else "afternoon_done"
    
    # Save the raw data
    catchup_bot.save_update(user_id, raw_text, column)
    
    # Summarize with AI
    yesterday_context = catchup_bot.get_yesterday_context(user_id)
    context = catchup_bot.get_morning_plan(user_id) if mode == "afternoon" else ""
    response = catchup_bot.summarise_update(raw_text, mode, context, yesterday_context)

    # Post to the public standup channel
    client.chat_postMessage(
        channel=CHANNEL_ID,
        text=f"👤 *Update from <@{user_id}>*\n{response}"
    )


def send_pings(text):
    if TEST_MODE:
        app.client.chat_postMessage(channel=MY_SLACK_ID, text=f"[TEST] {text}")
        return

    # In production, ping everyone in the channel
    result = app.client.conversations_members(channel=CHANNEL_ID)
    for uid in result["members"]:
        user_info = app.client.users_info(user=uid)
        if not user_info["user"]["is_bot"]:
            app.client.chat_postMessage(channel=uid, text=text)

if __name__ == "__main__":
    # Start the Scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: send_pings("Good morning! ☀️ What's the plan for today?"), 'cron', hour=10)
    scheduler.add_job(lambda: send_pings("EOD Recap time! 🏁 What did you get done?"), 'cron', hour=17)
    scheduler.start()

    # --- ADD THIS LINE FOR TESTING ---
    if TEST_MODE:
        print("🛠️ TEST_MODE is ON: Triggering instant test ping...")
        send_pings("Instant Test: What is the plan for today?")
    # ---------------------------------

    # Launch Socket Mode
    print("🚀 Bot is running...")
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    handler.start()
