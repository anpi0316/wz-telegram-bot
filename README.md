🤖 WZ Daily Poll Bot

A Telegram bot that sends a daily non-anonymous poll to your gaming chat asking: "Сегодня играем в WZ?" (Shall we play WZ today?) with "Yes" and "No" options.

✨ Features

📊 Daily automatic polls at configurable time (default 12:00 MSK)
👁️ Non-anonymous voting – everyone sees who voted for what
✅ Single choice only – users can select only one option
🎮 Perfect for gaming communities to quickly check who's ready to play
⏰ Configurable poll time via environment variable
🛠️ Manual poll command – trigger a poll anytime with /poll
🛠️ Technologies Used

Python 3.14 – Core programming language
python-telegram-bot v22.6 – Telegram Bot API library with JobQueue support
pytz – Timezone handling for accurate scheduling
Render.com – Free cloud hosting (Web Service)
UptimeRobot – Free monitoring to prevent service sleeping
GitHub – Version control and deployment automation
🚀 Getting Started

Prerequisites

Python 3.8+ installed
Telegram account
GitHub account (for deployment)
Render.com account (free tier)
Local Development Setup

Clone the repository
bash
git clone https://github.com/anpi0316/wz-telegram-bot.git
cd wz-telegram-bot
Create virtual environment
bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
Install dependencies
bash
pip install -r requirements.txt
Create your bot on Telegram
Message @BotFather on Telegram
Send /newbot and follow instructions
Save the provided token
Get your chat/group ID
Add your bot to the group
Send any message in the group
Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
Find chat.id (negative number for groups)
Configure environment variables
Create a .env file or export directly:
bash
export BOT_TOKEN="your_bot_token_here"
export CHAT_ID="-1001234567890"
export POLL_TIME="12:00"  # Optional, defaults to 12:00
Run the bot locally
bash
python bot.py
