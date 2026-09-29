<div align="center">

# ⚡ fuckjoin

### Tired of joining 10 garbage Telegram channels just to watch one video? We are too.

Send any bot link. Get your video. Never join a spam channel again.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Docker](https://img.shields.io/badge/Docker-Enabled-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![Telegram](https://img.shields.io/badge/Telethon-MTProto-blueviolet?style=for-the-badge&logo=telegram&logoColor=white)](https://github.com/LonamiWebs/Telethon)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

---

<!-- HERO ASSET: Recommended Size: 1280x640px or 1280x720px (Optimized GIF / WebM under 5MB) -->
<img src="assets/hero-demo.gif" alt="fuckjoin in action" width="100%" />

</div>

---

## 💡 What is fuckjoin?

Telegram is flooded with file lockers demanding:
> *"You must join Channel A, Channel B, and Channel C to view this content."*

**fuckjoin automates the pain away:**
1. **You send a bot link** (or forward a message containing one).
2. **fuckjoin joins the requirements in the background** via an isolated session.
3. **Pulls the target video/files**, delivers them straight to your private chat.
4. **Auto-cleans the junk:** Silently leaves channels afterward so your account stays spotless.

---

## ⚡ Visual Walkthrough

<div align="center">

| 1. Send Link or Tap Option | 2. Real-Time Relay & Cleanup |
|:---:|:---:|
| <!-- Screenshot Size: 600x400px --> <img src="assets/step1-selection.png" width="400" alt="Link Selection Prompt"/> | <!-- Screenshot Size: 600x400px --> <img src="assets/step2-delivery.png" width="400" alt="Delivery Progress and Result"/> |
| Multi-link selector menus with instant tap | Live progress cards with instant `/cancel` |

</div>

---

## ✨ Features

- 🎯 **Deep Link Unlocking:** Resolves nested bot handoffs, start parameters, and multi-step verification bots.
- ⚡ **Auto Channel Cleanup:** Silently tracks joined channels in persistent storage and leaves them in the background.
- 🛡️ **FloodWait Resilience:** Automatically absorbs and paces API rate limits without dropping in-flight jobs.
- ⊘ **Instant Cancellation:** Interrupt any task on the fly by clicking or typing `/cancel`.
- 👥 **Access Control:** Manage dynamic permissions on the fly using `/add_user` and `/state` without restarting.
- 🐳 **Docker-Ready:** Fully containerized setup with volume mounts preserving sessions and storage.

---

## 🐳 Running with Docker (Recommended)

### 1. Clone & Configure

```bash
git clone https://github.com/yourusername/fuckjoin.git
cd fuckjoin
cp .env.example .env
```

Edit `.env` with your API details:

```env
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
ADMINS=your_numeric_telegram_id
SESSION_NAME=sessions/fuckjoin
```

### 2. First-Time Interactive Login

On your first run, Telegram requires your phone number and login code. Run the container interactively:

```bash
docker compose run --rm fuckjoin
```

Follow the prompts to enter your phone number and Telegram verification code. Once authorized, exit with `Ctrl+C`.

### 3. Launch in Background

Now run the bot as a persistent background daemon:

```bash
docker compose up -d
```

To view real-time logs:

```bash
docker compose logs -f
```

---

## 💻 Manual Setup (Without Docker)

<details>
<summary>Click to view manual installation steps</summary>

### 1. Create Virtual Environment

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Run

```bash
python -m src
```

</details>

---

## 🛠️ Usage & Commands

| Command | Who Can Use | Description |
|---|---|---|
| `https://t.me/bot?start=...` | Admins & Allowed Users | Paste or forward any bot link to fetch content |
| `/cancel` or `cancel` | Admins & Allowed Users | Aborts the active running download immediately |
| `/admin` | Admins Only | Shows the admin dashboard and quick buttons |
| `/state` | Admins Only | Displays active jobs, queue count, and authorized user IDs |
| `/add_user <user_id>` | Admins Only | Grants bot access to a user |
| `/remove_user <user_id>` | Admins Only | Revokes a user's bot access |

---

## 📖 Deep Dive

Curious about channel rotation cycles, transient bot response filtration, or concurrency architecture?  
Check out [**TECHNICAL.md**](TECHNICAL.md).

---

## ⚖️ Disclaimer

*fuckjoin is designed as a personal workflow automation utility under Telegram's Terms of Service. Please respect content rights and adhere to community guidelines.*