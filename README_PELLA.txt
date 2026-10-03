ANSH TELEGRAM CONTROLLER
========================

Files:
- gen.py                 Original engine
- bot_controller.py      Telegram live controller
- requirements.txt       Dependencies
- .env.example           Bot token/admin configuration template

SETUP
-----
1. Upload all files to the same Pella server directory.
2. Copy .env.example to .env.
3. Put a NEW BotFather token in .env (do not reuse a token exposed in chat).
4. Keep ADMIN_IDS=6625618149 unless you intentionally change the authorized admin.
5. Install dependencies:
   pip install -r requirements.txt
6. Start:
   python bot_controller.py

TELEGRAM FLOW
-------------
/start -> control panel
START -> launches gen.py
The controller waits for gen.py input prompts and sends the same terminal prompt/options to Telegram.
Reply with the same number/value you would enter in the terminal.

LIVE STATS
----------
/stats or LIVE STATS button shows generated, failed (when target is known), pending activation,
activated and event result counts.

LIVE LOGS
---------
Only operational process logs are sent to LIVE_LOGS. The configured output groups receive their
respective result files after each completed batch of 50 records.

IMPORTANT
---------
- This wrapper does not rewrite the underlying gen.py generation/spin logic.
- It uses a pseudo-terminal so input() prompts remain interactive.
- Pella must allow a persistent/always-on process for true non-stop operation.
- Keep the bot token in .env, not in source code.
