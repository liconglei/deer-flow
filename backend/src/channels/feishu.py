"""Feishu/Lark channel — connects to Feishu via WebSocket (no public IP needed)."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import Any

from src.channels.base import Channel
from src.channels.message_bus import InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment

logger = logging.getLogger(__name__)


class FeishuChannel(Channel):
    """Feishu/Lark IM channel using the ``lark-oapi`` WebSocket client.

    Configuration keys (in ``config.yaml`` under ``channels.feishu``):
        - ``app_id``: Feishu app ID.
        - ``app_secret``: Feishu app secret.
        - ``verification_token``: (optional) Event verification token.

    The channel uses WebSocket long-connection mode so no public IP is required.

    Message flow:
        1. User sends a message → bot adds "OK" emoji reaction
        2. Bot replies in thread: "Working on it......"
        3. Agent processes the message and returns a result
        4. Bot replies in thread with the result
        5. Bot adds "DONE" emoji reaction to the original message
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="feishu", bus=bus, config=config)
        self._thread: threading.Thread | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._api_client = None
        self._CreateMessageReactionRequest = None
        self._CreateMessageReactionRequestBody = None
        self._Emoji = None
        self._CreateFileRequest = None
        self._CreateFileRequestBody = None
        self._CreateImageRequest = None
        self._CreateImageRequestBody = None

    async def start(self) -> None:
        if self._running:
            return

        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import (
                CreateFileRequest,
                CreateFileRequestBody,
                CreateImageRequest,
                CreateImageRequestBody,
                CreateMessageReactionRequest,
                CreateMessageReactionRequestBody,
                CreateMessageRequest,
                CreateMessageRequestBody,
                Emoji,
                ReplyMessageRequest,
                ReplyMessageRequestBody,
            )
        except ImportError:
            logger.error("lark-oapi is not installed. Install it with: uv add lark-oapi")
            return

        self._lark = lark
        self._CreateMessageRequest = CreateMessageRequest
        self._CreateMessageRequestBody = CreateMessageRequestBody
        self._ReplyMessageRequest = ReplyMessageRequest
        self._ReplyMessageRequestBody = ReplyMessageRequestBody
        self._CreateMessageReactionRequest = CreateMessageReactionRequest
        self._CreateMessageReactionRequestBody = CreateMessageReactionRequestBody
        self._Emoji = Emoji
        self._CreateFileRequest = CreateFileRequest
        self._CreateFileRequestBody = CreateFileRequestBody
        self._CreateImageRequest = CreateImageRequest
        self._CreateImageRequestBody = CreateImageRequestBody

        app_id = self.config.get("app_id", "")
        app_secret = self.config.get("app_secret", "")

        if not app_id or not app_secret:
            logger.error("Feishu channel requires app_id and app_secret")
            return

        self._api_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        self._main_loop = asyncio.get_event_loop()

        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

        # Both ws.Client construction and start() must happen in a dedicated
        # thread with its own event loop.  lark-oapi caches the running loop
        # at construction time and later calls loop.run_until_complete(),
        # which conflicts with an already-running uvloop.
        self._thread = threading.Thread(
            target=self._run_ws,
            args=(app_id, app_secret),
            daemon=True,
        )
        self._thread.start()
        logger.info("Feishu channel started")

    def _run_ws(self, app_id: str, app_secret: str) -> None:
        """Construct and run the lark WS client in a thread with a fresh event loop.

        The lark-oapi SDK captures a module-level event loop at import time
        (``lark_oapi.ws.client.loop``).  When uvicorn uses uvloop, that
        captured loop is the *main* thread's uvloop — which is already
        running, so ``loop.run_until_complete()`` inside ``Client.start()``
        raises ``RuntimeError``.

        We work around this by creating a plain asyncio event loop for this
        thread and patching the SDK's module-level reference before calling
        ``start()``.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            import lark_oapi as lark
            import lark_oapi.ws.client as _ws_client_mod

            # Replace the SDK's module-level loop so Client.start() uses
            # this thread's (non-running) event loop instead of the main
            # thread's uvloop.
            _ws_client_mod.loop = loop

            event_handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self._on_message).build()
            ws_client = lark.ws.Client(
                app_id=app_id,
                app_secret=app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,
            )
            ws_client.start()
        except Exception:
            if self._running:
                logger.exception("Feishu WebSocket error")

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Feishu channel stopped")

    async def send(self, msg: OutboundMessage, *, _max_retries: int = 3) -> None:
        if not self._api_client:
            logger.warning("[Feishu] send called but no api_client available")
            return

        logger.info(
            "[Feishu] sending reply: chat_id=%s, thread_ts=%s, text_len=%d",
            msg.chat_id,
            msg.thread_ts,
            len(msg.text),
        )
        content = self._build_card_content(msg.text)

        last_exc: Exception | None = None
        for attempt in range(_max_retries):
            try:
                if msg.thread_ts:
                    # Reply in thread (话题)
                    request = self._ReplyMessageRequest.builder().message_id(msg.thread_ts).request_body(self._ReplyMessageRequestBody.builder().msg_type("interactive").content(content).reply_in_thread(True).build()).build()
                    await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
                else:
                    # Send new message
                    request = self._CreateMessageRequest.builder().receive_id_type("chat_id").request_body(self._CreateMessageRequestBody.builder().receive_id(msg.chat_id).msg_type("interactive").content(content).build()).build()
                    await asyncio.to_thread(self._api_client.im.v1.message.create, request)

                # Add "DONE" reaction to the original message on final reply
                if msg.is_final and msg.thread_ts:
                    await self._add_reaction(msg.thread_ts, "DONE")

                return  # success
            except Exception as exc:
                last_exc = exc
                if attempt < _max_retries - 1:
                    delay = 2**attempt  # 1s, 2s
                    logger.warning(
                        "[Feishu] send failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1,
                        _max_retries,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        logger.error("[Feishu] send failed after %d attempts: %s", _max_retries, last_exc)
        raise last_exc  # type: ignore[misc]

    async def send_file(self, msg: OutboundMessage, attachment: ResolvedAttachment) -> bool:
        if not self._api_client:
            return False

        # Check size limits (image: 10MB, file: 30MB)
        if attachment.is_image and attachment.size > 10 * 1024 * 1024:
            logger.warning("[Feishu] image too large (%d bytes), skipping: %s", attachment.size, attachment.filename)
            return False
        if not attachment.is_image and attachment.size > 30 * 1024 * 1024:
            logger.warning("[Feishu] file too large (%d bytes), skipping: %s", attachment.size, attachment.filename)
            return False

        try:
            if attachment.is_image:
                file_key = await self._upload_image(attachment.actual_path)
                msg_type = "image"
                content = json.dumps({"image_key": file_key})
            else:
                file_key = await self._upload_file(attachment.actual_path, attachment.filename)
                msg_type = "file"
                content = json.dumps({"file_key": file_key})

            if msg.thread_ts:
                request = self._ReplyMessageRequest.builder().message_id(msg.thread_ts).request_body(self._ReplyMessageRequestBody.builder().msg_type(msg_type).content(content).reply_in_thread(True).build()).build()
                await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
            else:
                request = self._CreateMessageRequest.builder().receive_id_type("chat_id").request_body(self._CreateMessageRequestBody.builder().receive_id(msg.chat_id).msg_type(msg_type).content(content).build()).build()
                await asyncio.to_thread(self._api_client.im.v1.message.create, request)

            logger.info("[Feishu] file sent: %s (type=%s)", attachment.filename, msg_type)
            return True
        except Exception:
            logger.exception("[Feishu] failed to upload/send file: %s", attachment.filename)
            return False

    async def _upload_image(self, path) -> str:
        """Upload an image to Feishu and return the image_key."""
        with open(str(path), "rb") as f:
            request = self._CreateImageRequest.builder().request_body(self._CreateImageRequestBody.builder().image_type("message").image(f).build()).build()
            response = await asyncio.to_thread(self._api_client.im.v1.image.create, request)
        if not response.success():
            raise RuntimeError(f"Feishu image upload failed: code={response.code}, msg={response.msg}")
        return response.data.image_key

    async def _upload_file(self, path, filename: str) -> str:
        """Upload a file to Feishu and return the file_key."""
        suffix = path.suffix.lower() if hasattr(path, "suffix") else ""
        if suffix in (".xls", ".xlsx", ".csv"):
            file_type = "xls"
        elif suffix in (".ppt", ".pptx"):
            file_type = "ppt"
        elif suffix == ".pdf":
            file_type = "pdf"
        elif suffix in (".doc", ".docx"):
            file_type = "doc"
        else:
            file_type = "stream"

        with open(str(path), "rb") as f:
            request = self._CreateFileRequest.builder().request_body(self._CreateFileRequestBody.builder().file_type(file_type).file_name(filename).file(f).build()).build()
            response = await asyncio.to_thread(self._api_client.im.v1.file.create, request)
        if not response.success():
            raise RuntimeError(f"Feishu file upload failed: code={response.code}, msg={response.msg}")
        return response.data.file_key

    async def _download_user_image(self, message_id: str, image_key: str) -> Path | None:
        """Download a user-sent image from Feishu using the message resources API.

        User-sent images must use /im/v1/messages/{message_id}/resources/{file_key} API,
        not the /im/v1/images/{image_key} API (which is only for bot-uploaded images).

        Args:
            message_id: The message ID containing the image.
            image_key: The Feishu image key (file_key in resources API).

        Returns:
            Path to the downloaded image file, or None if download failed.
        """
        if not self._api_client:
            return None
        try:
            import httpx
            from lark_oapi.core.token import TokenManager

            # Get tenant access token using TokenManager
            config = self._api_client._config
            token = TokenManager.get_self_tenant_token(config)

            # User-sent images must use the message resources API
            # GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=image
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}?type=image"
            headers = {"Authorization": f"Bearer {token}"}

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url, headers=headers)

            if response.status_code != 200:
                # Try to parse error message
                try:
                    error_data = response.json()
                    error_code = error_data.get("code", "unknown")
                    error_msg = error_data.get("msg", response.text[:200])
                    logger.error(
                        "[Feishu] failed to download user image %s from message %s: status=%s, code=%s, msg=%s",
                        image_key,
                        message_id,
                        response.status_code,
                        error_code,
                        error_msg,
                    )
                except Exception:
                    logger.error(
                        "[Feishu] failed to download user image %s from message %s: status=%s, response=%s",
                        image_key,
                        message_id,
                        response.status_code,
                        response.text[:200] if response.text else "empty",
                    )
                return None

            # Determine file extension from content-type or default to .png
            content_type = response.headers.get("Content-Type", "")
            ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
            ext = ext_map.get(content_type, ".png")

            # Save to temp directory
            temp_dir = Path(tempfile.gettempdir()) / "deer-flow-feishu"
            temp_dir.mkdir(parents=True, exist_ok=True)
            file_path = temp_dir / f"{image_key}{ext}"

            with open(file_path, "wb") as f:
                f.write(response.content)

            logger.info("[Feishu] downloaded user image: %s -> %s (%d bytes)", image_key, file_path, len(response.content))
            return file_path
        except Exception:
            logger.exception("[Feishu] failed to download user image %s", image_key)
            return None

    async def _download_user_file(self, message_id: str, file_key: str, filename: str, resource_type: str = "file") -> Path | None:
        """Download a user-sent file from Feishu using the message resources API.

        User-sent files must use /im/v1/messages/{message_id}/resources/{file_key} API,
        not the /im/v1/files/{file_key} API (which is only for bot-uploaded files).

        Args:
            message_id: The message ID containing the file.
            file_key: The Feishu file key.
            filename: The original filename.
            resource_type: The resource type (file, media, audio).

        Returns:
            Path to the downloaded file, or None if download failed.
        """
        if not self._api_client:
            return None
        try:
            import httpx
            from lark_oapi.core.token import TokenManager

            # Get tenant access token using TokenManager
            config = self._api_client._config
            token = TokenManager.get_self_tenant_token(config)

            # User-sent files must use the message resources API
            # GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={type}
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={resource_type}"
            headers = {"Authorization": f"Bearer {token}"}

            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(url, headers=headers)

            if response.status_code != 200:
                # Try to parse error message
                try:
                    error_data = response.json()
                    error_code = error_data.get("code", "unknown")
                    error_msg = error_data.get("msg", response.text[:200])
                    logger.error(
                        "[Feishu] failed to download user file %s from message %s: status=%s, code=%s, msg=%s",
                        file_key,
                        message_id,
                        response.status_code,
                        error_code,
                        error_msg,
                    )
                except Exception:
                    logger.error(
                        "[Feishu] failed to download user file %s from message %s: status=%s, response=%s",
                        file_key,
                        message_id,
                        response.status_code,
                        response.text[:200] if response.text else "empty",
                    )
                return None

            # Save to temp directory
            temp_dir = Path(tempfile.gettempdir()) / "deer-flow-feishu"
            temp_dir.mkdir(parents=True, exist_ok=True)
            # Use original filename or fallback
            safe_filename = Path(filename).name if filename else f"{file_key}.bin"
            file_path = temp_dir / safe_filename

            with open(file_path, "wb") as f:
                f.write(response.content)

            logger.info("[Feishu] downloaded user file: %s -> %s (%d bytes)", file_key, file_path, len(response.content))
            return file_path
        except Exception:
            logger.exception("[Feishu] failed to download user file %s", file_key)
            return None

    # -- message formatting ------------------------------------------------

    @staticmethod
    def _build_card_content(text: str) -> str:
        """Build a Feishu interactive card with markdown content.

        Feishu's interactive card format natively renders markdown, including
        headers, bold/italic, code blocks, lists, and links.
        """
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": text}],
        }
        return json.dumps(card)

    # -- reaction helpers --------------------------------------------------

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """Add an emoji reaction to a message."""
        if not self._api_client or not self._CreateMessageReactionRequest:
            return
        try:
            request = self._CreateMessageReactionRequest.builder().message_id(message_id).request_body(self._CreateMessageReactionRequestBody.builder().reaction_type(self._Emoji.builder().emoji_type(emoji_type).build()).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message_reaction.create, request)
            logger.info("[Feishu] reaction '%s' added to message %s", emoji_type, message_id)
        except Exception:
            logger.exception("[Feishu] failed to add reaction '%s' to message %s", emoji_type, message_id)

    async def _send_running_reply(self, message_id: str) -> None:
        """Reply to a message in-thread with a 'Working on it...' hint."""
        if not self._api_client:
            return
        try:
            content = self._build_card_content("Working on it...")
            request = self._ReplyMessageRequest.builder().message_id(message_id).request_body(self._ReplyMessageRequestBody.builder().msg_type("interactive").content(content).reply_in_thread(True).build()).build()
            await asyncio.to_thread(self._api_client.im.v1.message.reply, request)
            logger.info("[Feishu] 'Working on it......' reply sent for message %s", message_id)
        except Exception:
            logger.exception("[Feishu] failed to send running reply for message %s", message_id)

    # -- internal ----------------------------------------------------------

    @staticmethod
    def _log_future_error(fut, name: str, msg_id: str) -> None:
        """Callback for run_coroutine_threadsafe futures to surface errors."""
        try:
            exc = fut.exception()
            if exc:
                logger.error("[Feishu] %s failed for msg_id=%s: %s", name, msg_id, exc)
        except Exception:
            pass

    def _on_message(self, event) -> None:
        """Called by lark-oapi when a message is received (runs in lark thread)."""
        try:
            logger.info("[Feishu] raw event received: type=%s", type(event).__name__)
            message = event.event.message
            chat_id = message.chat_id
            msg_id = message.message_id
            msg_type_feishu = message.message_type  # e.g., "text", "image", "file", "media"
            sender_id = event.event.sender.sender_id.open_id

            # root_id is set when the message is a reply within a Feishu thread.
            # Use it as topic_id so all replies share the same DeerFlow thread.
            root_id = getattr(message, "root_id", None) or None

            # Parse message content
            content = json.loads(message.content)
            text = content.get("text", "").strip()

            # Handle different message types
            files: list[dict[str, Any]] = []
            download_error_detail = ""
            if msg_type_feishu == "image":
                # Image message: {"image_key": "img_xxx"}
                # User-sent images must use /im/v1/messages/{message_id}/resources/{file_key} API
                image_key = content.get("image_key")
                if image_key:
                    logger.info("[Feishu] image message received: msg_id=%s, image_key=%s", msg_id, image_key)
                    # Download image using main loop via run_coroutine_threadsafe
                    try:
                        if self._main_loop and self._main_loop.is_running():
                            future = asyncio.run_coroutine_threadsafe(
                                self._download_user_image(msg_id, image_key), self._main_loop
                            )
                            file_path = future.result(timeout=30)  # Wait up to 30 seconds
                            if file_path:
                                text = "[用户发送了一张图片]"
                                files.append({
                                    "type": "image",
                                    "path": str(file_path),
                                    "filename": file_path.name,
                                })
                            else:
                                text = "[用户发送了一张图片，但下载失败]"
                                download_error_detail = f" (msg_id={msg_id}, image_key={image_key})"
                        else:
                            logger.warning("[Feishu] main loop not running, cannot download image")
                            text = "[用户发送了一张图片，但系统无法处理]"
                            download_error_detail = " (系统事件循环未运行)"
                    except Exception as e:
                        logger.exception("[Feishu] failed to download image")
                        text = f"[用户发送了一张图片，但下载失败: {type(e).__name__}]"
            elif msg_type_feishu == "file":
                # File message: {"file_key": "file_xxx", "file_name": "xxx.pdf"}
                # User-sent files must use /im/v1/messages/{message_id}/resources/{file_key} API
                file_key = content.get("file_key")
                file_name = content.get("file_name", "unknown_file")
                if file_key:
                    logger.info("[Feishu] file message received: msg_id=%s, file_key=%s, file_name=%s", msg_id, file_key, file_name)
                    
                    # Check if file is a video - videos are not supported
                    video_extensions = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".mpeg", ".mpg"}
                    file_ext = Path(file_name).suffix.lower() if file_name else ""
                    if file_ext in video_extensions:
                        logger.info("[Feishu] video file ignored (not supported): %s", file_name)
                        text = f"[用户发送了一个视频文件: {file_name}]\n\n抱歉，暂不支持视频文件处理。如需分析视频内容，请提取视频帧后以图片形式发送。"
                    else:
                        # Download file using main loop via run_coroutine_threadsafe
                        try:
                            if self._main_loop and self._main_loop.is_running():
                                future = asyncio.run_coroutine_threadsafe(
                                    self._download_user_file(msg_id, file_key, file_name), self._main_loop
                                )
                                file_path = future.result(timeout=60)  # Wait up to 60 seconds for files
                                if file_path:
                                    text = f"[用户发送了一个文件: {file_name}]"
                                    files.append({
                                        "type": "file",
                                        "path": str(file_path),
                                        "filename": file_name,
                                    })
                                else:
                                    text = f"[用户发送了一个文件: {file_name}，但下载失败]"
                            else:
                                logger.warning("[Feishu] main loop not running, cannot download file")
                                text = f"[用户发送了一个文件: {file_name}，但系统无法处理]"
                        except Exception:
                            logger.exception("[Feishu] failed to download file")
                            text = f"[用户发送了一个文件: {file_name}，但下载失败]"
            elif msg_type_feishu == "audio":
                # Audio message (voice): {"file_key": "file_xxx", "duration": 60}
                # User-sent audio must use /im/v1/messages/{message_id}/resources/{file_key} API
                file_key = content.get("file_key")
                duration_ms = content.get("duration", 0)  # Duration in milliseconds
                duration_sec = duration_ms / 1000 if duration_ms else 0  # Convert to seconds
                file_name = f"voice_{msg_id}.mp3"  # Feishu voice messages are typically mp3
                if file_key:
                    logger.info("[Feishu] audio message received: msg_id=%s, file_key=%s, duration=%sms (%ss)", msg_id, file_key, duration_ms, duration_sec)
                    # Download audio file using main loop via run_coroutine_threadsafe
                    try:
                        if self._main_loop and self._main_loop.is_running():
                            future = asyncio.run_coroutine_threadsafe(
                                self._download_user_file(msg_id, file_key, file_name, resource_type="file"), self._main_loop
                            )
                            file_path = future.result(timeout=60)  # Wait up to 60 seconds for audio
                            if file_path:
                                duration_text = f"{duration_sec}秒" if duration_sec else "未知时长"
                                text = f"[用户发送了一条语音消息，时长: {duration_text}]"
                                files.append({
                                    "type": "audio",
                                    "path": str(file_path),
                                    "filename": file_name,
                                    "duration": duration,
                                })
                            else:
                                text = "[用户发送了一条语音消息，但下载失败]"
                        else:
                            logger.warning("[Feishu] main loop not running, cannot download audio")
                            text = "[用户发送了一条语音消息，但系统无法处理]"
                    except Exception:
                        logger.exception("[Feishu] failed to download audio file")
                        text = "[用户发送了一条语音消息，但下载失败]"
            elif msg_type_feishu == "media":
                # Media message (audio/video): {"file_key": "file_xxx", "file_name": "xxx.mp3"}
                # User-sent media must use /im/v1/messages/{message_id}/resources/{file_key} API
                file_key = content.get("file_key")
                file_name = content.get("file_name", "unknown_media")
                if file_key:
                    logger.info("[Feishu] media message received: msg_id=%s, file_key=%s, file_name=%s", msg_id, file_key, file_name)
                    
                    # Check if file is a video - videos are not supported
                    video_extensions = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".mpeg", ".mpg"}
                    file_ext = Path(file_name).suffix.lower() if file_name else ""
                    if file_ext in video_extensions:
                        logger.info("[Feishu] video file ignored (not supported): %s", file_name)
                        text = f"[用户发送了一个视频文件: {file_name}]\n\n抱歉，暂不支持视频文件处理。如需分析视频内容，请提取视频帧后以图片形式发送。"
                    else:
                        # Download media file using main loop via run_coroutine_threadsafe
                        try:
                            if self._main_loop and self._main_loop.is_running():
                                future = asyncio.run_coroutine_threadsafe(
                                    self._download_user_file(msg_id, file_key, file_name, resource_type="media"), self._main_loop
                                )
                                file_path = future.result(timeout=60)  # Wait up to 60 seconds for media
                                if file_path:
                                    text = f"[用户发送了一个媒体文件: {file_name}]"
                                    files.append({
                                        "type": "media",
                                        "path": str(file_path),
                                        "filename": file_name,
                                    })
                                else:
                                    text = f"[用户发送了一个媒体文件: {file_name}，但下载失败]"
                            else:
                                logger.warning("[Feishu] main loop not running, cannot download media")
                                text = f"[用户发送了一个媒体文件: {file_name}，但系统无法处理]"
                        except Exception:
                            logger.exception("[Feishu] failed to download media file")
                            text = f"[用户发送了一个媒体文件: {file_name}，但下载失败]"
            elif msg_type_feishu == "post":
                # Rich text post: extract plain text content
                post_content = content.get("content", [])
                text = self._extract_post_text(post_content) or text

            logger.info(
                "[Feishu] parsed message: chat_id=%s, msg_id=%s, root_id=%s, sender=%s, msg_type=%s, text=%r, files=%d",
                chat_id,
                msg_id,
                root_id,
                sender_id,
                msg_type_feishu,
                text[:100] if text else "",
                len(files),
            )

            if not text and not files:
                logger.info("[Feishu] empty text and no files, ignoring message")
                return

            # Check if it's a command
            if text.startswith("/"):
                msg_type = InboundMessageType.COMMAND
            else:
                msg_type = InboundMessageType.CHAT

            # topic_id: use root_id for replies (same topic), msg_id for new messages (new topic)
            topic_id = root_id or msg_id

            inbound = self._make_inbound(
                chat_id=chat_id,
                user_id=sender_id,
                text=text,
                msg_type=msg_type,
                thread_ts=msg_id,
                files=files,
                metadata={"message_id": msg_id, "root_id": root_id, "feishu_msg_type": msg_type_feishu},
            )
            inbound.topic_id = topic_id

            # Schedule on the async event loop
            if self._main_loop and self._main_loop.is_running():
                logger.info("[Feishu] publishing inbound message to bus (type=%s, msg_id=%s)", msg_type.value, msg_id)
                # Schedule all coroutines and attach error logging to futures
                for name, coro in [
                    ("add_reaction", self._add_reaction(msg_id, "OK")),
                    ("send_running_reply", self._send_running_reply(msg_id)),
                    ("publish_inbound", self.bus.publish_inbound(inbound)),
                ]:
                    fut = asyncio.run_coroutine_threadsafe(coro, self._main_loop)
                    fut.add_done_callback(lambda f, n=name, mid=msg_id: self._log_future_error(f, n, mid))
            else:
                logger.warning("[Feishu] main loop not running, cannot publish inbound message")
        except Exception:
            logger.exception("[Feishu] error processing message")

    @staticmethod
    def _extract_post_text(post_content: list) -> str:
        """Extract plain text from a Feishu post (rich text) message.

        Args:
            post_content: List of post sections, each containing elements.

        Returns:
            Combined plain text from all text elements.
        """
        if not isinstance(post_content, list):
            return ""

        text_parts = []
        for section in post_content:
            if not isinstance(section, dict):
                continue
            elements = section.get("elements", [])
            for elem in elements:
                if isinstance(elem, dict) and elem.get("tag") == "text":
                    text_parts.append(elem.get("text", ""))

        return " ".join(text_parts).strip()