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
4. sends the same `/start` command again;
5. follows a new, allow-listed bot `?start=` link if the bot hands off delivery; and
6. forwards the resulting response to Saved Messages.

It creates one formatted status message in Saved Messages and edits it as the job progresses: start sent, detected channels, clickable channel links, each join attempt, retries, handoffs, every final message, forwarding, or a final failure. Requests are processed through a single task queue, so multiple `/get` commands do not overlap. A bot may send several final messages; the tool collects messages that arrive within two seconds of its final response and forwards every forwardable one.

## Let approved friends submit requests

By default, only your own Saved Messages `/get` commands are accepted. To allow a friend, add their **numeric Telegram user ID** to `ALLOWED_REQUESTER_IDS` in `.env` and restart the tool:

```env
ALLOWED_REQUESTER_IDS=123456789,987654321
```

They must send `/get <link>` in a private chat with your account. The final forwarded messages and the same editable progress report are sent to them; an identical progress report is mirrored to your Saved Messages. Never use usernames for this allow-list and do not leave it open to everyone.

## Telegram rate limits

`FloodWaitError` means Telegram has temporarily rate-limited the account after too many actions (for example, channel joins). The progress message shows the required wait time; the tool waits that many seconds and retries the affected join once. It does not bypass Telegram's limit.

It ignores a bare `⏳` or `⌛` wait message and waits for the bot's next response. It also repeats the check after each successful join (up to five passes), so it can handle bots that reveal channel requirements in stages. Before each retry it waits `RETRY_DELAY_SECONDS` (2 seconds by default); set it to `0`–`60` in `.env` if a particular approved bot needs a different delay. It can follow up to five handoffs to another bot, but **every** handoff bot must be in `ALLOWED_BOTS`. This prevents an accidental `/get` command from driving an unapproved bot. Do not add a bot unless its operator has given permission.

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

If the final bot response has Telegram's protected-content/forwarding restriction enabled, Telegram will not let any client forward or copy it. The tool reports this in Saved Messages instead of crashing; it cannot bypass that Telegram restriction.
