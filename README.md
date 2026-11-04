# 🚀 fuckjoin — SeZaR Enhanced Edition

## Overview

This contribution introduces a collection of improvements to the original **fuckjoin** Python project, extending its Telegram automation capabilities, reliability, multi-user support, group integration, media delivery, and server management.

The goal is to transform fuckjoin from a primarily personal Telegram automation tool into a more flexible, multi-user Telegram downloader that can operate in private chats, Saved Messages, and authorized Telegram groups.

These enhancements were developed as part of the **SeZaR Telegram Downloader** project, building upon the original architecture while aiming to preserve its existing functionality.

---

## ✨ Main Features & Improvements

### 1. 🔗 Advanced Telegram Link Processing

Improved support for Telegram bot links, invitation links, and alternative Telegram URL formats.

**Enhancements:**
- Automatically normalize `telegram.me` links to `t.me`.
- Preserve bot start parameters, including `?start=`.
- Support standard Telegram bot links and private invitation links.
- Handle links forwarded from other conversations.
- Extract supported links from bot messages and handoff responses.
- Support links submitted through private chats, groups, and Saved Messages.
- Validate recognized Telegram domains before processing.

**Example:**

`https://telegram.me/ExampleBot?start=123456`

Automatically becomes:

`https://t.me/ExampleBot?start=123456`

No additional user interaction is required.

### 2. 🤖 Automated Telegram Bot Interaction

Extended the existing automation workflow for handling Telegram bots with multiple verification and subscription requirements.

**Features:**
- Automatically send `/start` commands with supported parameters.
- Detect required Telegram channels and groups.
- Process multiple channel requirements.
- Follow supported bot-to-bot handoff links.
- Interact with supported verification buttons.
- Retry supported verification flows when necessary.
- Identify temporary processing and waiting messages.
- Track bot interactions and channel-joining progress.
- Avoid treating ordinary bot usernames as channel invitations.

This enables more complex, multi-step Telegram bot workflows.

### 3. 📥 Improved Media Retrieval & Delivery

Enhanced the media delivery process with additional validation, error handling, and retry logic.

**Improvements:**
- Retrieve supported videos, photos, documents, and other Telegram media.
- Forward retrieved content to the original requester or authorized group.
- Detect whether media was actually delivered.
- Avoid counting ordinary text messages as successful media delivery.
- Report forwarding failures rather than silently treating them as successful.
- Distinguish expired source messages from other delivery errors.
- Prevent unnecessary duplicate delivery when content has already been sent.

These changes are particularly useful for Telegram bots that automatically remove their messages shortly after sending them.

### 4. 🔄 Smart Recovery & Automatic Retries

Introduced a recovery mechanism designed to restart interrupted Telegram download workflows when appropriate.

**Features:**
- Preserve the original requested Telegram link.
- Recover from temporary bot-response timeouts.
- Restart eligible failed workflows from the original link.
- Recheck bot requirements during recovery.
- Support configurable retry attempts.
- Allow an additional recovery attempt when source media is confirmed deleted.
- Avoid unnecessary retries for protected-content or permission errors.
- Preserve the original requester and destination during retries.
- Respect Telegram FloodWait restrictions throughout recovery.

**Example workflow:**

1. User submits a Telegram bot link.
2. Automation starts and processes the bot requirements.
3. A temporary interruption delays media retrieval.
4. The source bot deletes its previous messages.
5. The recovery system retries the original link when the failure is eligible.
6. Newly available media is retrieved and delivered when permitted.

This reduces the need for users to manually resend links after temporary failures.

### 5. 👥 Telegram Group Integration

Extended the application to support requests submitted directly inside authorized Telegram groups.

**Features:**
- Allow approved Telegram groups to use the downloader.
- Automatically process supported links submitted by group members.
- Return results to the originating group.
- Support group-level access restrictions.
- Optionally allow all group members to submit requests.
- Introduce per-user cooldowns.
- Identify group chat IDs using diagnostic commands.
- Preserve separate handling for private chats and Saved Messages.

**Self-Message Protection:**

Messages sent by the automation account into its own group are ignored to prevent accidental self-triggered automation.

Saved Messages remain available for direct requests from the authenticated Telegram account.

### 6. 🔐 Multiple Authorized Users & Administration

Enhanced authorization to support multiple Telegram users independently of the primary automation account.

**Features:**
- Configure multiple authorized requester IDs.
- Separate administrator permissions from ordinary requester access.
- Maintain private-chat access controls.
- Support authorized group access.
- Provide user and automation status information.
- Add diagnostic commands for checking identity and connection behavior.

**Configuration example:**

```ini
ADMINS=YOUR_ADMIN_ID
ALLOWED_REQUESTER_IDS=USER_ID_1,USER_ID_2

ALLOWED_GROUP_IDS=YOUR_GROUP_ID
GROUP_ALLOW_ALL_MEMBERS=1
GROUP_USER_COOLDOWN_SECONDS=30
```

Administrators can control access without modifying the main Python workflow.

### 7. ❤️ Optional Channel Reaction Automation

Added configurable reaction support for workflows involving Telegram channels.

**Features:**
- Support reacting to recent posts in eligible channels.
- Configure the number of recent posts.
- Support configurable reaction emojis, including ❤️.
- Optionally target specifically configured channels.
- Apply reaction limits per automation flow.
- Handle Telegram reaction errors and FloodWait restrictions.
- Integrate reaction handling with supported channel-verification workflows.

**Example settings:**

```ini
AUTO_REACT_LAST_POSTS=5
AUTO_REACT_EMOJI=❤️
AUTO_REACT_MODE=required
AUTO_REACT_MAX_PER_FLOW=25
```

Reaction automation is optional and subject to Telegram's available reactions, permissions, and rate limits.

### 8. ⏳ FloodWait & Rate-Limit Handling

Improved reliability when Telegram applies temporary account restrictions.

**Enhancements:**
- Detect Telegram FloodWait exceptions.
- Respect required waiting periods.
- Apply configurable pacing between supported operations.
- Prevent unhandled FloodWait exceptions from unnecessarily terminating tasks.
- Improve handling of Telegram bot-response timeouts.
- Add limits to progress-message operations.
- Provide clearer status messages when processing is delayed.

These changes improve stability while respecting Telegram's restrictions; they do not bypass Telegram rate limits.

### 9. 📊 Progress Tracking & Detailed Reports

Enhanced visibility into automated download operations.

**Features:**
- Display progress for individual Telegram requests.
- Report channel requirements and join results.
- Show bot handoffs and verification steps.
- Display retry and recovery information.
- Report successful media delivery counts.
- Provide error information for unsuccessful requests.
- Maintain administrator-accessible automation state diagnostics.

**Example progress stages:**

```text
[★] Started
[⌲] Sending /start
[✓] Received bot response
[✓] Processing channel requirements
[↺] Retrying verification
[✶] Fetching media
[➴] Delivering to requester
[✓] Completed
```

This makes troubleshooting significantly easier for administrators.

### 10. 🛡️ Worker & Queue Reliability

Improved the internal task-processing system to reduce interruptions caused by individual Telegram requests.

**Enhancements:**
- Persistent task queue processing.
- Better handling of unexpected worker exceptions.
- Improved cleanup after completed or failed tasks.
- Worker supervision and restart support.
- Time limits on selected progress and reporting operations.
- Improved cancellation handling.
- Reduced risk of one failed request blocking subsequent requests.
- Additional event diagnostics for authorized group messages.

The objective is to keep the automation available even when individual Telegram bots are unavailable or unresponsive.

### 11. 🖥️ CentOS & systemd Integration

Added operational support for deploying the application as a persistent Linux service.

**Features:**
- Automatic application startup after server reboot.
- Automatic service restart after process failures.
- Python syntax checks before startup.
- Direct execution through the project's virtual environment.
- Centralized service status and logging.
- Convenient restart commands.

**Example:**

```bash
fuckjoin-reset
```

This command restarts the systemd-managed application without requiring administrators to manually enter the project directory and activate its Python environment.

### 12. 🌐 Multi-Instance Bot Management

Developed an additional Python management utility for running multiple independent fuckjoin installations on one Linux server.

**Features:**
- Create new instances from an existing project.
- Register previously installed instances.
- Assign an independent folder to each installation.
- Maintain separate `.env` configurations.
- Maintain independent Telegram sessions.
- Create isolated Python virtual environments.
- Manage each instance through a separate systemd service.
- Generate instance-specific restart commands.
- Support per-instance status and log inspection.

**Example:**

```bash
sezar-fuckjoin create 123 --from fuckjoin
sezar-fuckjoin create 456 --from fuckjoin
```

Each instance receives its own restart command:

```bash
fuckjoin-reset-123
fuckjoin-reset-456
```

The multi-instance manager is a separate deployment utility and should be reviewed independently if included in this contribution.

---

## ⚙️ Configuration & Compatibility

The enhanced implementation introduces optional environment settings for:

- Authorized Telegram requesters
- Allowed Telegram groups
- Group member access
- Per-user request cooldowns
- Channel reactions
- Reaction limits and emojis
- Automatic recovery attempts
- Expired-message recovery
- Telegram debugging and event diagnostics

The new options are designed to integrate with the existing environment-based configuration.

Existing installations should retain their API credentials, Telegram sessions, and application configuration when upgraded correctly.

---

## 🔒 Security & Telegram Compliance

The contribution is designed around the following principles:

- Keep Telegram API credentials and session files private.
- Do not include personal `.env` files in the repository.
- Require explicit authorization for private and group requests.
- Keep each automation instance's session isolated.
- Respect Telegram FloodWait responses.
- Do not attempt to bypass Telegram content-protection restrictions.
- Avoid uncontrolled retries and unnecessary duplicate messages.
- Provide configurable limits for potentially high-volume operations.

Administrators remain responsible for operating their accounts in accordance with Telegram's rules.

---

## 🧪 Testing & Verification

Development included offline checks for several individual enhancements, including:

- Python syntax validation
- Telegram URL normalization
- Recovery decision logic
- Missing-message detection
- Group-message filtering
- Authorization configuration
- Patch compatibility checks
- Multi-instance registration
- systemd service generation
- Backup creation and preservation

**Validation note:** Individual components have been tested offline, and selected features have been exercised on a CentOS server. Complete end-to-end testing of the combined changes is still required before production use or merging.

Suggested final validation:

- [ ] Private-chat request
- [ ] Saved Messages request
- [ ] Authorized group request
- [ ] Multi-user authorization
- [ ] Bot handoff processing
- [ ] Media delivery
- [ ] Expired-message recovery
- [ ] FloodWait recovery
- [ ] Worker restart
- [ ] Multiple independent instances

---

## 🎯 Project Goals

The primary objectives of these improvements are:

1. Make Telegram bot-link processing more reliable.
2. Reduce repeated manual requests caused by temporary failures.
3. Support multiple authorized users and groups.
4. Improve media delivery reporting and recovery.
5. Simplify Linux server deployment and maintenance.
6. Provide infrastructure for running independent automation instances.
7. Preserve and extend the original project's functionality.

---

## ❤️ Credits & Acknowledgments

**Original Project:** [AmirHBuilds/fuckjoin](https://github.com/AmirHBuilds/fuckjoin)

**Enhancements & Integration:** SeZaR

Special thanks to the original developer for creating and publishing the fuckjoin project.

These contributions are intended to build upon that foundation and make the project more flexible, maintainable, and useful for the community.

**Developed with SeZaR — Smart Telegram Automation.**
