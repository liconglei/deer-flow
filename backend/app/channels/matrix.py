"""Matrix channel — connects to Matrix homeserver via matrix-nio SDK."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from src.channels.base import Channel
from src.channels.message_bus import InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment

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

    The channel connects to the Matrix homeserver and listens for messages
    in rooms where the bot is joined.

    Message flow:
        1. User sends a message in a Matrix room
        2. Bot processes the message through the DeerFlow agent
        3. Bot replies in the same room/thread
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="matrix", bus=bus, config=config)
        self._client = None
        self._running = False
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._allowed_rooms: set[str] = set(config.get("allowed_rooms", []))
        self._allowed_users: set[str] = set(config.get("allowed_users", []))
        self._store_path = config.get("store_path")

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
                logger.info("[Matrix] using provided access token")
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

        # Set up event callback
        self._client.add_event_callback(self._on_room_message, self._get_room_message_event_class())

        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

        # Start sync in background
        self._sync_task = asyncio.create_task(self._run_sync())
        logger.info("Matrix channel started (homeserver=%s, user=%s)", homeserver, user_id)

    def _get_room_message_event_class(self):
        """Get the RoomMessage event class from nio."""
        from nio import RoomMessage

        return RoomMessage

    async def _run_sync(self) -> None:
        """Run the Matrix sync loop."""
        try:
            # First sync to get initial state
            await self._client.sync_forever(timeout=30000, full_state=True)
        except asyncio.CancelledError:
            logger.info("[Matrix] sync loop cancelled")
        except Exception:
            if self._running:
                logger.exception("[Matrix] sync error")

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)

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
            "[Matrix] sending reply: room_id=%s, text_len=%d",
            msg.chat_id,
            len(msg.text),
        )

        try:
            from nio import RoomSendResponse

            # Send message to room
            content = {
                "msgtype": "m.text",
                "body": msg.text,
                "format": "org.matrix.custom.html",
                "formatted_body": self._markdown_to_html(msg.text),
            }

            # If replying to a specific message (thread_ts is the event_id)
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
            else:
                logger.warning("[Matrix] failed to send message: %s", response)

        except Exception:
            logger.exception("[Matrix] error sending message")

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

            # Send file message
            if attachment.is_image:
                content = {
                    "msgtype": "m.image",
                    "body": attachment.filename,
                    "url": mxc_uri,
                    "info": {
                        "mimetype": attachment.mime_type,
                        "size": attachment.size,
                    },
                }
            else:
                content = {
                    "msgtype": "m.file",
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

    async def _on_room_message(self, room, event) -> None:
        """Called when a room message is received."""
        try:
            from nio import RoomMessageText, RoomMessageImage, RoomMessageFile, RoomMessageAudio, RoomMessageMedia

            # Skip messages from ourselves
            if event.sender == self._client.user_id:
                return

            # Check room allowlist
            if self._allowed_rooms and room.room_id not in self._allowed_rooms:
                logger.debug("[Matrix] ignoring message from unallowed room: %s", room.room_id)
                return

            # Check user allowlist
            if self._allowed_users and event.sender not in self._allowed_users:
                logger.debug("[Matrix] ignoring message from unallowed user: %s", event.sender)
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
            elif isinstance(event, RoomMessageMedia):
                text = f"[用户发送了一个媒体文件: {event.body}]"
                msg_type = InboundMessageType.CHAT
                # Download media
                file_path = await self._download_matrix_media(event.url, event.body or "media")
                if file_path:
                    files.append({
                        "type": "media",
                        "path": str(file_path),
                        "filename": event.body or "media",
                    })
                else:
                    text = "[用户发送了一个媒体文件，但下载失败]"
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
                },
            )

            # Get topic_id from room ID (each room is a separate conversation)
            inbound.topic_id = room.room_id

            await self.bus.publish_inbound(inbound)

        except Exception:
            logger.exception("[Matrix] error processing message")

    async def _download_matrix_media(self, mxc_uri: str, filename: str) -> Path | None:
        """Download media from Matrix server.

        Args:
            mxc_uri: Matrix content URI (e.g., "mxc://matrix.org/abc123")
            filename: Original filename.

        Returns:
            Path to downloaded file, or None if failed.
        """
        if not self._client or not mxc_uri:
            return None

        try:
            import httpx

            # Parse mxc:// URI
            parsed = urllib.parse.urlparse(mxc_uri)
            if parsed.scheme != "mxc":
                logger.warning("[Matrix] invalid mxc URI: %s", mxc_uri)
                return None

            # Build download URL
            server_name = parsed.netloc
            media_id = parsed.path.lstrip("/")
            download_url = f"{self._client.homeserver}/_matrix/media/v3/download/{server_name}/{media_id}"

            # Download with access token
            headers = {"Authorization": f"Bearer {self._client.access_token}"}
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(download_url, headers=headers)

            if response.status_code != 200:
                logger.error("[Matrix] failed to download media %s: status=%s", mxc_uri, response.status_code)
                return None

            # Save to temp directory
            temp_dir = Path(tempfile.gettempdir()) / "deer-flow-matrix"
            temp_dir.mkdir(parents=True, exist_ok=True)

            # Sanitize filename
            safe_filename = Path(filename).name if filename else media_id
            file_path = temp_dir / safe_filename

            with open(file_path, "wb") as f:
                f.write(response.content)

            logger.info("[Matrix] downloaded media: %s -> %s (%d bytes)", mxc_uri, file_path, len(response.content))
            return file_path

        except Exception:
            logger.exception("[Matrix] error downloading media: %s", mxc_uri)
            return None

    @staticmethod
    def _markdown_to_html(text: str) -> str:
        """Convert basic markdown to HTML for Matrix formatted messages.

        Matrix supports a subset of HTML. This converts common markdown
        formatting to HTML tags.
        """
        import re

        # Escape HTML entities first
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        # Code blocks
        text = re.sub(r"```(\w*)\n(.*?)```", r"<pre><code class='\1'>\2</code></pre>", text, flags=re.DOTALL)

        # Inline code
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

        # Bold
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)

        # Italic
        text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)

        # Links
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

        # Line breaks (double newline -> paragraph)
        text = re.sub(r"\n\n", "</p><p>", text)
        text = f"<p>{text}</p>"

        return text
