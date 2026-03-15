"""Matrix channel — connects to Matrix homeserver via matrix-nio SDK."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from app.channels.base import Channel
from app.channels.message_bus import InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment

logger = logging.getLogger(__name__)


class MatrixChannel(Channel):
    """Matrix IM channel using the ``matrix-nio`` SDK.

    Configuration keys (in ``config.yaml`` under ``channels.matrix``):
        - ``homeserver``: Matrix homeserver URL (e.g., "https://matrix.org").
        - ``user_id``: Matrix user ID (e.g., "@bot:matrix.org").
        - ``password``: Matrix password for login.
        - ``access_token``: (alternative) Access token instead of password.
        - ``device_id``: (optional) Device ID for the session.
        - ``allowed_rooms``: (optional) List of room IDs to listen to. Empty = all rooms.
        - ``allowed_users``: (optional) List of user IDs allowed to interact. Empty = all users.
        - ``store_path``: (optional) Path to store encryption keys and session data.
        - ``typing_enabled``: (optional) Enable typing notifications (default: True).
        - ``reactions_enabled``: (optional) Enable emoji reactions (default: True).

    The channel connects to the Matrix homeserver and listens for messages
    in rooms where the bot is joined.

    Message flow:
        1. User sends a message in a Matrix room
        2. Bot sends typing notification and adds reaction
        3. Bot processes the message through the DeerFlow agent
        4. Bot replies in the same room/thread
        5. Bot updates reaction to indicate completion
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="matrix", bus=bus, config=config)
        self._client = None
        self._running = False
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._allowed_rooms: set[str] = set(config.get("allowed_rooms", []))
        self._allowed_users: set[str] = set(config.get("allowed_users", []))
        self._store_path = config.get("store_path")
        self._typing_enabled = config.get("typing_enabled", True)
        self._reactions_enabled = config.get("reactions_enabled", True)
        # Track active typing tasks for cancellation
        self._typing_tasks: dict[str, asyncio.Task] = {}
        # Track streaming message event_ids: chat_id -> event_id
        self._streaming_messages: dict[str, str] = {}

    async def start(self) -> None:
        if self._running:
            return

        try:
            from nio import AsyncClient, AsyncClientConfig, LoginResponse
        except ImportError:
            logger.error("matrix-nio is not installed. Install it with: uv add matrix-nio")
            return

        homeserver = self.config.get("homeserver", "")
        user_id = self.config.get("user_id", "")
        password = self.config.get("password", "")
        access_token = self.config.get("access_token", "")
        device_id = self.config.get("device_id", "deer-flow-bot")

        if not homeserver or not user_id:
            logger.error("Matrix channel requires homeserver and user_id")
            return

        if not password and not access_token:
            logger.error("Matrix channel requires either password or access_token")
            return

        self._main_loop = asyncio.get_event_loop()

        # Set up store path for encryption keys
        if self._store_path:
            store_path = Path(self._store_path)
        else:
            store_path = Path(tempfile.gettempdir()) / "deer-flow-matrix-store"
        store_path.mkdir(parents=True, exist_ok=True)

        # Configure client
        client_config = AsyncClientConfig(
            max_limit_exceeded=0,
            max_timeouts=0,
            store_sync_tokens=True,
            encryption_enabled=True,
        )

        self._client = AsyncClient(
            homeserver=homeserver,
            user=user_id,
            device_id=device_id,
            store_path=str(store_path),
            config=client_config,
        )

        # Login
        try:
            if access_token:
                self._client.access_token = access_token
                # IMPORTANT: When using access_token directly, user_id must be set manually
                # because nio doesn't set it from the constructor when access_token is provided later
                self._client.user_id = user_id
                logger.info("[Matrix] using provided access token for user %s", user_id)
            else:
                response = await self._client.login(password=password, device_name=device_id)
                if isinstance(response, LoginResponse):
                    logger.info("[Matrix] logged in successfully as %s", user_id)
                else:
                    logger.error("[Matrix] login failed: %s", response)
                    return
        except Exception:
            logger.exception("[Matrix] login error")
            return

        # Load encryption keys if available
        try:
            await self._client.load_store()
            logger.info("[Matrix] loaded encryption store")
        except Exception:
            logger.info("[Matrix] no existing encryption store, will create new one")

        # CRITICAL: Perform initial sync BEFORE registering event callbacks
        # This ensures we get the next_batch token without triggering callbacks
        # on any historical messages that might be in the sync response
        await self._initial_sync()

        # NOW register event callbacks - they will only receive new events
        # since we already have the next_batch token from initial sync
        self._client.add_event_callback(self._on_room_message, self._get_room_message_event_class())

        # Set up event callback for encrypted events that failed to decrypt
        # Note: nio uses MegolmEvent for undecryptable events, RoomEncryptionEvent for encryption state changes
        from nio import MegolmEvent
        self._client.add_event_callback(self._on_room_encrypted_event, MegolmEvent)

        self._running = True
        self._start_time = time.time()  # Record when the bot started (after initial sync)
        self.bus.subscribe_outbound(self._on_outbound)
        self.bus.subscribe_stream_update(self._on_stream_update)  # For streaming responses

        # Start sync in background - will use next_batch from initial sync
        self._sync_task = asyncio.create_task(self._run_sync())
        logger.info("Matrix channel started (homeserver=%s, user=%s)", homeserver, user_id)

    def _get_room_message_event_class(self):
        """Get the RoomMessage event class from nio."""
        from nio import RoomMessage

        return RoomMessage

    async def _initial_sync(self) -> None:
        """Perform initial sync to get current position without processing events.

        This is called once at startup to get the next_batch token.
        After this, only new messages will be received.
        """
        try:
            # Use a filter that excludes timeline events to speed up initial sync
            # and avoid receiving historical messages
            sync_filter = {
                "room": {
                    "timeline": {"limit": 0},  # Don't fetch any timeline events
                    "state": {"lazy_load_members": True},
                },
                "account_data": {"not_types": ["*"]},  # Exclude account data
                "presence": {"not_types": ["*"]},  # Exclude presence
            }
            response = await self._client.sync(
                timeout=30000,
                full_state=True,
                sync_filter=sync_filter,
            )
            # The client stores the next_batch token internally
            logger.info("[Matrix] initial sync completed, next_batch=%s", self._client.next_batch)
        except Exception:
            logger.exception("[Matrix] initial sync error")
            raise

    async def _run_sync(self) -> None:
        """Run the Matrix sync loop.

        Uses the next_batch token from initial sync to only receive new events.
        """
        try:
            # sync_forever will use the stored next_batch token,
            # so it will only receive events after the initial sync
            await self._client.sync_forever(timeout=30000)
        except asyncio.CancelledError:
            logger.info("[Matrix] sync loop cancelled")
        except Exception:
            if self._running:
                logger.exception("[Matrix] sync error")

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)
        self.bus.unsubscribe_stream_update(self._on_stream_update)  # Remove streaming callback

        if hasattr(self, "_sync_task"):
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        if self._client:
            await self._client.close()
            self._client = None

        logger.info("Matrix channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        if not self._client:
            logger.warning("[Matrix] send called but no client available")
            return

        logger.info(
            "[Matrix] sending reply: room_id=%s, text_len=%d, thread_ts=%s, is_final=%s",
            msg.chat_id,
            len(msg.text),
            msg.thread_ts,
            msg.is_final,
        )

        try:
            from nio import RoomSendResponse

            # Check if we have a tracked event for this chat (streaming update)
            tracked_event_id = self._streaming_messages.get(msg.chat_id)

            if tracked_event_id and not msg.is_final:
                # Streaming update: edit existing message
                await self.edit_message(msg.chat_id, tracked_event_id, msg.text)
                logger.debug("[Matrix] edited streaming message: chat=%s, event=%s", msg.chat_id, tracked_event_id)
                return

            if tracked_event_id and msg.is_final:
                # Final streaming update: edit and cleanup
                await self.edit_message(msg.chat_id, tracked_event_id, msg.text)
                await self._stop_typing(msg.chat_id)
                if msg.thread_ts:
                    await self._send_reaction(msg.chat_id, msg.thread_ts, "✅")
                self._streaming_messages.pop(msg.chat_id, None)
                logger.info("[Matrix] streaming completed: chat=%s, event=%s", msg.chat_id, tracked_event_id)
                return

            # No tracked event: send new message
            await self._stop_typing(msg.chat_id)

            # Send message to room
            content = {
                "msgtype": "m.text",
                "body": msg.text,
                "format": "org.matrix.custom.html",
                "formatted_body": self._markdown_to_html(msg.text),
            }

            # If replying to a specific message (thread_ts is the event_id)
            # Use m.in_reply_to for simple replies, m.thread for thread support
            if msg.thread_ts:
                content["m.relates_to"] = {
                    "m.in_reply_to": {"event_id": msg.thread_ts}
                }

            response = await self._client.room_send(
                room_id=msg.chat_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=True,
            )

            if isinstance(response, RoomSendResponse):
                logger.info("[Matrix] message sent successfully: event_id=%s", response.event_id)

                # Track event_id for streaming messages
                if not msg.is_final:
                    self._streaming_messages[msg.chat_id] = response.event_id
                    logger.debug("[Matrix] tracking streaming message: chat=%s, event=%s", msg.chat_id, response.event_id)

                # Send "check mark" reaction to original message to indicate completion
                if msg.thread_ts and msg.is_final:
                    await self._send_reaction(msg.chat_id, msg.thread_ts, "✅")
            else:
                logger.warning("[Matrix] failed to send message: %s", response)

        except Exception:
            logger.exception("[Matrix] error sending message")
            # Ensure typing is stopped on error
            await self._stop_typing(msg.chat_id)

    async def send_file(self, msg: OutboundMessage, attachment: ResolvedAttachment) -> bool:
        if not self._client:
            return False

        # Check file size (Matrix typically has 50MB limit, but we'll use 20MB to be safe)
        if attachment.size > 20 * 1024 * 1024:
            logger.warning("[Matrix] file too large (%d bytes), skipping: %s", attachment.size, attachment.filename)
            return False

        try:
            from nio import UploadResponse

            # Read file content
            with open(attachment.actual_path, "rb") as f:
                file_data = f.read()

            # Upload to Matrix
            response = await self._client.upload(
                data=file_data,
                content_type=attachment.mime_type,
                filename=attachment.filename,
            )

            if not isinstance(response, UploadResponse):
                logger.warning("[Matrix] failed to upload file: %s", response)
                return False

            mxc_uri = response.content_uri
            logger.info("[Matrix] file uploaded: %s -> %s", attachment.filename, mxc_uri)

            # Send file message - determine msgtype based on MIME type
            msgtype = "m.file"
            if attachment.mime_type.startswith("image/"):
                msgtype = "m.image"
            elif attachment.mime_type.startswith("audio/"):
                msgtype = "m.audio"
            elif attachment.mime_type.startswith("video/"):
                msgtype = "m.video"

            content = {
                "msgtype": msgtype,
                "body": attachment.filename,
                "url": mxc_uri,
                "info": {
                    "mimetype": attachment.mime_type,
                    "size": attachment.size,
                },
            }

            # Add reply relation if needed
            if msg.thread_ts:
                content["m.relates_to"] = {
                    "m.in_reply_to": {"event_id": msg.thread_ts}
                }

            from nio import RoomSendResponse
            send_response = await self._client.room_send(
                room_id=msg.chat_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=True,
            )

            if isinstance(send_response, RoomSendResponse):
                logger.info("[Matrix] file sent: %s", attachment.filename)
                return True
            else:
                logger.warning("[Matrix] failed to send file message: %s", send_response)
                return False

        except Exception:
            logger.exception("[Matrix] error uploading/sending file: %s", attachment.filename)
            return False

    async def _on_room_encrypted_event(self, room, event) -> None:
        """Called when an encrypted event cannot be decrypted.

        This can happen when:
        - The bot hasn't received the room key yet
        - The sender's device is not verified
        - The message was sent before the bot joined the room

        We log a warning and optionally request the room key.
        """
        logger.warning(
            "[Matrix] failed to decrypt message in room %s from %s (event_id=%s)",
            room.room_id,
            event.sender,
            event.event_id,
        )

        # Skip messages from ourselves
        if event.sender == self._client.user_id:
            return

        # Check room allowlist
        if self._allowed_rooms and room.room_id not in self._allowed_rooms:
            return

        # Check user allowlist
        if self._allowed_users and event.sender not in self._allowed_users:
            return

        # Safety check: Skip historical messages (sent before the bot started)
        # This should not happen after initial sync, but we keep it as a safeguard
        if hasattr(event, 'server_timestamp') and event.server_timestamp:
            event_timestamp = event.server_timestamp / 1000  # Convert ms to seconds
            if event_timestamp < self._start_time:
                logger.info(
                    "[Matrix] skipping historical encrypted message: event_ts=%.2f, bot_start=%.2f, sender=%s",
                    event_timestamp,
                    self._start_time,
                    event.sender,
                )
                return

        # Request the room key to try to decrypt future messages
        try:
            await self._client.request_room_key(event.session_id, event.sender_key, room.room_id)
            logger.info("[Matrix] requested room key for session %s", event.session_id)
        except Exception:
            logger.exception("[Matrix] failed to request room key")

        # Send a notice to the user about decryption failure
        inbound = self._make_inbound(
            chat_id=room.room_id,
            user_id=event.sender,
            text="[无法解密此消息，请确保已验证设备或重新发送消息]",
            msg_type=InboundMessageType.CHAT,
            thread_ts=event.event_id,
            metadata={
                "event_id": event.event_id,
                "room_name": room.display_name,
                "decryption_failed": True,
            },
        )
        inbound.topic_id = room.room_id
        await self.bus.publish_inbound(inbound)

    async def _on_room_message(self, room, event) -> None:
        """Called when a room message is received."""
        try:
            from nio import RoomMessageText, RoomMessageImage, RoomMessageFile, RoomMessageAudio, RoomMessageVideo

            # Log all incoming events for debugging
            event_ts = event.server_timestamp / 1000 if hasattr(event, 'server_timestamp') and event.server_timestamp else 0
            logger.info(
                "[Matrix] received event: room=%s, sender=%s, type=%s, event_ts=%.2f, bot_start=%.2f, age=%.1fs, bot_user_id=%s",
                room.room_id,
                event.sender,
                type(event).__name__,
                event_ts,
                self._start_time,
                time.time() - event_ts if event_ts > 0 else 0,
                self._client.user_id,
            )

            # Skip messages from ourselves (compare case-insensitively)
            # Matrix user IDs are case-sensitive but some servers normalize them
            sender_lower = event.sender.lower() if event.sender else ""
            user_id_lower = self._client.user_id.lower() if self._client.user_id else ""
            logger.info("[Matrix] checking own message: sender_lower=%s, user_id_lower=%s, match=%s", sender_lower, user_id_lower, sender_lower == user_id_lower)
            if sender_lower and user_id_lower and sender_lower == user_id_lower:
                logger.info("[Matrix] skipping own message")
                return

            # Check room allowlist
            if self._allowed_rooms and room.room_id not in self._allowed_rooms:
                logger.debug("[Matrix] ignoring message from unallowed room: %s", room.room_id)
                return

            # Check user allowlist
            if self._allowed_users and event.sender not in self._allowed_users:
                logger.debug("[Matrix] ignoring message from unallowed user: %s", event.sender)
                return

            # Safety check: Skip historical messages (sent before the bot started)
            # This should not happen after initial sync, but we keep it as a safeguard
            if event_ts > 0 and event_ts < self._start_time:
                logger.info(
                    "[Matrix] skipping historical message: event_ts=%.2f < bot_start=%.2f",
                    event_ts,
                    self._start_time,
                )
                return

            # Extract message content
            text = ""
            files: list[dict[str, Any]] = []

            if isinstance(event, RoomMessageText):
                text = event.body or ""
                # Check for commands
                if text.startswith("/"):
                    msg_type = InboundMessageType.COMMAND
                else:
                    msg_type = InboundMessageType.CHAT
            elif isinstance(event, RoomMessageImage):
                text = f"[用户发送了一张图片: {event.body}]"
                msg_type = InboundMessageType.CHAT
                # Download image
                file_path = await self._download_matrix_media(event.url, event.body or "image")
                if file_path:
                    files.append({
                        "type": "image",
                        "path": str(file_path),
                        "filename": event.body or "image",
                    })
                else:
                    text = "[用户发送了一张图片，但下载失败]"
            elif isinstance(event, RoomMessageFile):
                text = f"[用户发送了一个文件: {event.body}]"
                msg_type = InboundMessageType.CHAT
                # Download file
                file_path = await self._download_matrix_media(event.url, event.body or "file")
                if file_path:
                    files.append({
                        "type": "file",
                        "path": str(file_path),
                        "filename": event.body or "file",
                    })
                else:
                    text = "[用户发送了一个文件，但下载失败]"
            elif isinstance(event, RoomMessageAudio):
                text = f"[用户发送了一个音频: {event.body}]"
                msg_type = InboundMessageType.CHAT
                # Download audio
                file_path = await self._download_matrix_media(event.url, event.body or "audio")
                if file_path:
                    files.append({
                        "type": "audio",
                        "path": str(file_path),
                        "filename": event.body or "audio",
                    })
                else:
                    text = "[用户发送了一个音频，但下载失败]"
            elif isinstance(event, RoomMessageVideo):
                text = f"[用户发送了一个视频: {event.body}]"
                msg_type = InboundMessageType.CHAT
                # Download video
                file_path = await self._download_matrix_media(event.url, event.body or "video")
                if file_path:
                    files.append({
                        "type": "video",
                        "path": str(file_path),
                        "filename": event.body or "video",
                    })
                else:
                    text = "[用户发送了一个视频，但下载失败]"
            else:
                # Unknown message type
                logger.debug("[Matrix] ignoring unknown message type: %s", type(event).__name__)
                return

            text = text.strip()
            if not text and not files:
                logger.debug("[Matrix] empty message, ignoring")
                return

            logger.info(
                "[Matrix] parsed message: room_id=%s, sender=%s, text=%r, files=%d",
                room.room_id,
                event.sender,
                text[:100] if text else "",
                len(files),
            )

            # Send typing notification and reaction to indicate processing
            # Start typing notification (will be stopped when response is sent)
            await self._start_typing(room.room_id)
            # Add "eyes" reaction to indicate message received
            await self._send_reaction(room.room_id, event.event_id, "👀")

            # Extract thread root from event relations (MSC3440 threading support)
            # If the message is part of a thread, use the thread root as topic_id
            # This enables token caching for conversations within the same thread
            thread_root_id: str | None = None
            if hasattr(event, 'source') and isinstance(event.source, dict):
                relates_to = event.source.get('content', {}).get('m.relates_to', {})
                if relates_to.get('rel_type') == 'm.thread':
                    thread_root_id = relates_to.get('event_id')
                    logger.info("[Matrix] message is in thread, thread_root=%s", thread_root_id)

            # Create inbound message
            inbound = self._make_inbound(
                chat_id=room.room_id,
                user_id=event.sender,
                text=text,
                msg_type=msg_type,
                thread_ts=event.event_id,  # Use event_id for threaded replies
                files=files,
                metadata={
                    "event_id": event.event_id,
                    "room_name": room.display_name,
                    "thread_root_id": thread_root_id,
                },
            )

            # topic_id determines DeerFlow thread mapping:
            # - If message is in a Matrix thread (MSC3440), use thread_root_id
            # - Otherwise, use room_id (each room = one conversation)
            # This enables token caching for conversations in the same Matrix thread
            inbound.topic_id = thread_root_id or room.room_id

            await self.bus.publish_inbound(inbound)

        except Exception:
            logger.exception("[Matrix] error processing message")

    async def _download_matrix_media(self, mxc_uri: str, filename: str) -> Path | None:
        """Download media from Matrix server, with automatic decryption for encrypted rooms.

        Args:
            mxc_uri: Matrix content URI (e.g., "mxc://matrix.org/abc123")
            filename: Original filename.

        Returns:
            Path to downloaded file, or None if failed.
        """
        if not self._client or not mxc_uri:
            return None

        try:
            from nio import DownloadResponse

            # Parse mxc:// URI: mxc://server_name/media_id
            if not mxc_uri.startswith("mxc://"):
                logger.warning("[Matrix] invalid mxc URI: %s", mxc_uri)
                return None

            parts = mxc_uri[6:].split("/", 1)
            if len(parts) != 2:
                logger.warning("[Matrix] invalid mxc URI format: %s", mxc_uri)
                return None

            server_name, media_id = parts

            # Use nio's download() for automatic decryption support
            response = await self._client.download(
                server_name=server_name,
                media_id=media_id,
                filename=filename,
            )

            if not isinstance(response, DownloadResponse):
                logger.warning("[Matrix] failed to download media %s: %s", mxc_uri, response)
                return None

            # Save to temp directory
            temp_dir = Path(tempfile.gettempdir()) / "deer-flow-matrix"
            temp_dir.mkdir(parents=True, exist_ok=True)

            # Sanitize filename
            safe_filename = Path(filename).name if filename else media_id
            file_path = temp_dir / safe_filename

            with open(file_path, "wb") as f:
                f.write(response.body)

            logger.info("[Matrix] downloaded media: %s -> %s (%d bytes)", mxc_uri, file_path, len(response.body))
            return file_path

        except Exception:
            logger.exception("[Matrix] error downloading media: %s", mxc_uri)
            return None

    @staticmethod
    def _markdown_to_html(text: str) -> str:
        """Convert markdown to HTML for Matrix formatted messages.

        Matrix supports a subset of HTML. This converts markdown formatting
        to Matrix-compatible HTML tags.

        Supported markdown:
        - Headers: # ## ### #### ##### ######
        - Bold: **text** or __text__
        - Italic: *text* or _text_
        - Strikethrough: ~~text~~
        - Code blocks: ```lang\ncode```
        - Inline code: `code`
        - Links: [text](url)
        - Unordered lists: - item or * item
        - Ordered lists: 1. item
        - Task lists: - [ ] item or - [x] item
        - Blockquotes: > text
        - Horizontal rules: --- or ***
        - Tables: | col1 | col2 |
        """
        import re

        lines = text.split("\n")
        result_lines = []
        in_code_block = False
        code_block_lang = ""
        code_block_content = []
        in_list = False
        list_type = None  # "ul" or "ol"
        in_blockquote = False
        in_table = False
        table_rows = []

        def escape_html(s: str) -> str:
            """Escape HTML entities."""
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        i = 0
        while i < len(lines):
            line = lines[i]

            # Code blocks (handle first, as they contain raw content)
            if line.strip().startswith("```"):
                if not in_code_block:
                    in_code_block = True
                    code_block_lang = line.strip()[3:].strip()
                    code_block_content = []
                else:
                    # End code block - escape and output
                    code_html = escape_html("\n".join(code_block_content))
                    if code_block_lang:
                        result_lines.append(f"<pre><code class='{code_block_lang}'>{code_html}</code></pre>")
                    else:
                        result_lines.append(f"<pre><code>{code_html}</code></pre>")
                    in_code_block = False
                    code_block_lang = ""
                    code_block_content = []
                i += 1
                continue

            if in_code_block:
                code_block_content.append(line)
                i += 1
                continue

            # Close list if needed
            if in_list and not (line.strip().startswith(("-", "*", "+")) or re.match(r"^(\s*)[-*+]\s+\[", line) or re.match(r"^\d+\.\s", line.strip())):
                result_lines.append("</ul>" if list_type == "ul" else "</ol>")
                in_list = False
                list_type = None

            # Close blockquote if needed
            if in_blockquote and not line.strip().startswith(">"):
                result_lines.append("</blockquote>")
                in_blockquote = False

            # Close table if needed
            if in_table and not line.strip().startswith("|"):
                if table_rows:
                    result_lines.append("<table>")
                    for idx, row in enumerate(table_rows):
                        cells = [MatrixChannel._format_inline_md(escape_html(c.strip())) for c in row.split("|")[1:-1]]
                        if idx == 0:
                            result_lines.append("<thead><tr>")
                            for cell in cells:
                                result_lines.append(f"<th>{cell}</th>")
                            result_lines.append("</tr></thead><tbody>")
                        elif not re.match(r"^[\s\-:|]+$", row):  # Skip separator rows
                            result_lines.append("<tr>")
                            for cell in cells:
                                result_lines.append(f"<td>{cell}</td>")
                            result_lines.append("</tr>")
                    result_lines.append("</tbody></table>")
                table_rows = []
                in_table = False

            # Empty line
            if not line.strip():
                i += 1
                continue

            # Horizontal rules
            if re.match(r"^(-{3,}|\*{3,}|_{3,})$", line.strip()):
                result_lines.append("<hr>")
                i += 1
                continue

            # Headers
            header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if header_match:
                level = len(header_match.group(1))
                content = MatrixChannel._format_inline_md(escape_html(header_match.group(2)))
                result_lines.append(f"<h{level}>{content}</h{level}>")
                i += 1
                continue

            # Blockquotes
            if line.strip().startswith(">"):
                if not in_blockquote:
                    result_lines.append("<blockquote>")
                    in_blockquote = True
                quote_content = line.strip()[1:].strip()
                result_lines.append(MatrixChannel._format_inline_md(escape_html(quote_content)))
                i += 1
                continue

            # Tables
            if line.strip().startswith("|"):
                if not in_table:
                    in_table = True
                    table_rows = []
                table_rows.append(line)
                i += 1
                continue

            # Unordered lists (including task lists)
            ul_match = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
            if ul_match:
                if not in_list or list_type != "ul":
                    if in_list:
                        result_lines.append("</ol>" if list_type == "ol" else "</ul>")
                    result_lines.append("<ul>")
                    in_list = True
                    list_type = "ul"
                item_content = ul_match.group(2)
                # Task list
                task_match = re.match(r"^\[([ xX])\]\s*(.*)$", item_content)
                if task_match:
                    checked = task_match.group(1).lower() == "x"
                    checkbox = "✅ " if checked else "⬜ "
                    item_content = checkbox + MatrixChannel._format_inline_md(escape_html(task_match.group(2)))
                else:
                    item_content = MatrixChannel._format_inline_md(escape_html(item_content))
                result_lines.append(f"<li>{item_content}</li>")
                i += 1
                continue

            # Ordered lists
            ol_match = re.match(r"^(\s*)\d+\.\s+(.*)$", line)
            if ol_match:
                if not in_list or list_type != "ol":
                    if in_list:
                        result_lines.append("</ul>" if list_type == "ul" else "</ol>")
                    result_lines.append("<ol>")
                    in_list = True
                    list_type = "ol"
                item_content = MatrixChannel._format_inline_md(escape_html(ol_match.group(2)))
                result_lines.append(f"<li>{item_content}</li>")
                i += 1
                continue

            # Regular paragraph
            result_lines.append(f"<p>{MatrixChannel._format_inline_md(escape_html(line))}</p>")
            i += 1

        # Close any open tags
        if in_code_block:
            code_html = escape_html("\n".join(code_block_content))
            result_lines.append(f"<pre><code>{code_html}</code></pre>")
        if in_list:
            result_lines.append("</ul>" if list_type == "ul" else "</ol>")
        if in_blockquote:
            result_lines.append("</blockquote>")
        if in_table and table_rows:
            result_lines.append("<table>")
            for idx, row in enumerate(table_rows):
                cells = [MatrixChannel._format_inline_md(escape_html(c.strip())) for c in row.split("|")[1:-1]]
                if idx == 0:
                    result_lines.append("<thead><tr>")
                    for cell in cells:
                        result_lines.append(f"<th>{cell}</th>")
                    result_lines.append("</tr></thead><tbody>")
                elif not re.match(r"^[\s\-:|]+$", row):
                    result_lines.append("<tr>")
                    for cell in cells:
                        result_lines.append(f"<td>{cell}</td>")
                    result_lines.append("</tr>")
            result_lines.append("</tbody></table>")

        return "\n".join(result_lines)

    @staticmethod
    def _format_inline_md(text: str) -> str:
        """Format inline markdown elements (bold, italic, code, links, strikethrough).

        Note: text should already be HTML-escaped before calling this.
        """
        import re

        # Inline code - use special markers to avoid double processing
        # Since text is already escaped, we look for backticks directly
        parts = []
        last_end = 0
        for match in re.finditer(r"`([^`]+)`", text):
            # Add text before code
            parts.append(text[last_end:match.start()])
            # Add code (content is already escaped)
            parts.append(f"<code>{match.group(1)}</code>")
            last_end = match.end()
        parts.append(text[last_end:])
        text = "".join(parts)

        # Bold: **text** or __text__
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)

        # Italic: *text* or _text_ (must come after bold)
        text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
        text = re.sub(r"_([^_]+)_", r"<em>\1</em>", text)

        # Strikethrough: ~~text~~
        text = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", text)

        # Links: [text](url) - URL needs to be unescaped for href
        def replace_link(m):
            link_text = m.group(1)
            url = m.group(2)
            # Unescape URL for href
            url = url.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            return f'<a href="{url}">{link_text}</a>'

        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace_link, text)

        return text

    # -- typing notification helpers ---------------------------------------

    async def _send_typing_notification(self, room_id: str, typing: bool = True, timeout: int = 30000) -> None:
        """Send typing notification to a Matrix room.

        This tells other users in the room that the bot is typing.
        The notification expires after `timeout` milliseconds.

        Args:
            room_id: The Matrix room ID.
            typing: True to show typing, False to clear typing state.
            timeout: Duration in milliseconds for the typing notification (default: 30s).
        """
        if not self._client or not self._typing_enabled:
            return

        try:
            import httpx

            # Build the typing API URL
            # PUT /_matrix/client/v3/rooms/{roomId}/typing/{userId}
            url = f"{self._client.homeserver}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id)}/typing/{urllib.parse.quote(self._client.user_id)}"

            # Build request body
            body = {"typing": typing}
            if typing:
                body["timeout"] = timeout

            async with httpx.AsyncClient() as http:
                response = await http.put(
                    url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._client.access_token}",
                        "Content-Type": "application/json",
                    },
                    timeout=10.0,
                )

                if response.status_code == 200:
                    logger.debug("[Matrix] typing notification sent: room=%s, typing=%s", room_id, typing)
                else:
                    logger.warning(
                        "[Matrix] typing notification failed: status=%d, body=%s",
                        response.status_code,
                        response.text[:200],
                    )

        except Exception:
            logger.exception("[Matrix] failed to send typing notification")

    async def _start_typing(self, room_id: str) -> None:
        """Start sending periodic typing notifications.

        Typing notifications expire after 30 seconds, so we need to
        periodically resend them until the response is ready.
        """
        # Cancel any existing typing task for this room
        if room_id in self._typing_tasks:
            self._typing_tasks[room_id].cancel()

        async def _typing_loop():
            try:
                while self._running:
                    await self._send_typing_notification(room_id, typing=True, timeout=30000)
                    await asyncio.sleep(25)  # Resend every 25 seconds (before 30s timeout)
            except asyncio.CancelledError:
                # Stop typing when cancelled
                await self._send_typing_notification(room_id, typing=False)
                raise
            except Exception:
                logger.exception("[Matrix] typing loop error")

        self._typing_tasks[room_id] = asyncio.create_task(_typing_loop())

    async def _stop_typing(self, room_id: str) -> None:
        """Stop typing notification for a room."""
        if room_id in self._typing_tasks:
            self._typing_tasks[room_id].cancel()
            try:
                await self._typing_tasks[room_id]
            except asyncio.CancelledError:
                pass
            del self._typing_tasks[room_id]

        # Send final typing=false
        await self._send_typing_notification(room_id, typing=False)

    # -- reaction helpers ---------------------------------------------------

    async def _send_reaction(self, room_id: str, event_id: str, emoji: str) -> None:
        """Send an emoji reaction to a message.

        Args:
            room_id: The Matrix room ID.
            event_id: The event ID to react to.
            emoji: The emoji to react with (e.g., "✅", "👀").
        """
        if not self._client or not self._reactions_enabled:
            return

        try:
            from nio import RoomSendResponse

            # Reactions are sent as m.reaction events with a relates_to
            content = {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event_id,
                    "key": emoji,
                }
            }

            response = await self._client.room_send(
                room_id=room_id,
                message_type="m.reaction",
                content=content,
                ignore_unverified_devices=True,
            )

            if isinstance(response, RoomSendResponse):
                logger.info("[Matrix] reaction '%s' sent to event %s", emoji, event_id)
            else:
                logger.warning("[Matrix] failed to send reaction: %s", response)

        except Exception:
            logger.exception("[Matrix] failed to send reaction")

    async def _redact_reaction(self, room_id: str, reaction_event_id: str, reason: str = "") -> None:
        """Redact (remove) a reaction.

        Args:
            room_id: The Matrix room ID.
            reaction_event_id: The event ID of the reaction to remove.
            reason: Optional reason for redaction.
        """
        if not self._client:
            return

        try:
            await self._client.room_redact(
                room_id=room_id,
                event_id=reaction_event_id,
                reason=reason,
            )
            logger.info("[Matrix] reaction redacted: %s", reaction_event_id)
        except Exception:
            logger.exception("[Matrix] failed to redact reaction")

    # -- message editing (streaming support) --------------------------------

    async def edit_message(self, room_id: str, event_id: str, new_text: str) -> str | None:
        """Edit a previously sent message.

        This is used for streaming responses where we update a single message
        as content is generated, rather than sending multiple messages.

        Args:
            room_id: The Matrix room ID.
            event_id: The event ID of the message to edit.
            new_text: The new text content.

        Returns:
            The new event ID if successful, None otherwise.
        """
        if not self._client:
            return None

        try:
            from nio import RoomSendResponse

            # Matrix edit format: m.replace relation with m.new_content
            content = {
                "msgtype": "m.text",
                "body": f"* {new_text}",  # Prefix with * to indicate edit
                "format": "org.matrix.custom.html",
                "formatted_body": self._markdown_to_html(new_text),
                "m.new_content": {
                    "msgtype": "m.text",
                    "body": new_text,
                    "format": "org.matrix.custom.html",
                    "formatted_body": self._markdown_to_html(new_text),
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": event_id,
                },
            }

            response = await self._client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
                ignore_unverified_devices=True,
            )

            if isinstance(response, RoomSendResponse):
                logger.debug("[Matrix] message edited: %s -> %s", event_id, response.event_id)
                return response.event_id
            else:
                logger.warning("[Matrix] failed to edit message: %s", response)
                return None

        except Exception:
            logger.exception("[Matrix] error editing message")
            return None

    # -- streaming support --------------------------------------------------

    async def _on_stream_update(self, msg: "StreamUpdateMessage") -> None:
        """Handle streaming message updates from the dispatcher.

        This is called during streaming response generation to update
        the message content in real-time.

        Args:
            msg: The stream update message containing the new text.
        """
        if not self._client:
            return

        room_id = msg.chat_id

        # Get the tracked event_id for this chat (set when initial message was sent)
        event_id = self._streaming_messages.get(room_id) or msg.message_id

        logger.debug(
            "[Matrix] stream update: room=%s, event=%s, text_len=%d, is_final=%s",
            room_id,
            event_id,
            len(msg.text),
            msg.is_final,
        )

        # Edit the message with new content
        if event_id:
            await self.edit_message(room_id, event_id, msg.text)

            # If this is the final update, stop typing, add completion reaction, and cleanup
            if msg.is_final:
                await self._stop_typing(room_id)
                # Note: The reaction should be added to the original message event_id
                # not the edit event, so users see the reaction on the message
                await self._send_reaction(room_id, event_id, "✅")
                # Cleanup tracked message
                self._streaming_messages.pop(room_id, None)

    def supports_streaming(self) -> bool:
        """Return True if this channel supports message editing for streaming."""
        return True
