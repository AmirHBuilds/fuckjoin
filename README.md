# Authorized Telegram delivery flow

This tool runs as **your Telegram user account**, using your `api_id` and `api_hash`. It is for bots whose operator has explicitly authorized this automation.

## What it does

Send this message to your own **Saved Messages**:

```text
/get https://t.me/ultraajhlaghibot?start=9ym1SMzU
```

The tool then:

1. sends `/start 9ym1SMzU` to the authorized bot;
2. reads join-channel URLs from that bot's inline keyboard and message text;
3. joins those channels with **your account**;
4. sends the same `/start` command again; and
5. forwards the resulting response to Saved Messages.

It only accepts bots listed in `ALLOWED_BOTS`. This prevents an accidental `/get` command from driving an unapproved bot. Do not add a bot unless its operator has given permission.

## Setup

1. Create an application at [my.telegram.org](https://my.telegram.org), then copy its `API_ID` and `API_HASH`.
2. Copy the example configuration and fill it in:

   ```bash
   cp .env.example .env
   ```

   For example:

   ```env
   API_ID=123456
   API_HASH=abcdef0123456789abcdef0123456789
   ALLOWED_BOTS=ultraajhlaghibot
   ```

3. Install and run:

   ```bash
   python -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   python -m app
   ```

4. On the first run, enter your phone number and Telegram login code at the terminal. A local `.session` file is created so later runs do not require another code. Keep that file private.
5. Open **Saved Messages** in Telegram and send `/get <bot start link>`.

## Supported links

The `/get` command accepts `https://t.me/<bot>?start=<argument>` links. The bot username must appear in `ALLOWED_BOTS`.

The join step supports public `https://t.me/channel_name` links and private `https://t.me/+invite_hash` / `https://t.me/joinchat/invite_hash` links found in text or inline keyboards. Telegram may still reject a join if the channel requires an approval request or your account is restricted.

## Limits

This is a small workflow helper, not a universal bot scraper. It processes one `/get` request at a time and waits up to 30 seconds for each bot response. It does not solve CAPTCHAs, approval requests, payment gates, or custom interactive challenges.
