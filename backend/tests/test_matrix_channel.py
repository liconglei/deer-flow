"""Tests for the Matrix channel implementation."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.channels.message_bus import InboundMessageType, MessageBus, OutboundMessage, ResolvedAttachment


class TestMatrixSendFileTypeDetection:
    """Test that send_file uses correct Matrix msgtype based on MIME type."""

    @pytest.fixture
    def mock_matrix_client(self):
        """Create a mock nio AsyncClient."""
        client = MagicMock()
        client.user_id = "@bot:matrix.org"
        client.access_token = "test-token"
        client.homeserver = "https://matrix.org"
        return client

    @pytest.fixture
    def temp_file(self, tmp_path):
        """Create a temporary file for testing."""
        file_path = tmp_path / "test_file"
        file_path.write_bytes(b"test content")
        return file_path

    def _create_attachment(self, path: Path, mime_type: str, size: int = 100) -> ResolvedAttachment:
        """Helper to create a ResolvedAttachment."""
        return ResolvedAttachment(
            virtual_path=f"/mnt/user-data/outputs/{path.name}",
            actual_path=path,
            filename=path.name,
            mime_type=mime_type,
            size=size,
            is_image=mime_type.startswith("image/"),
        )

    @pytest.mark.asyncio
    async def test_send_image_uses_m_image(self, mock_matrix_client, temp_file):
        """Images should use msgtype 'm.image'."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        channel._client = mock_matrix_client

        # Mock upload response
        with patch("nio.UploadResponse") as MockUploadResponse:
            mock_upload = MagicMock()
            mock_upload.content_uri = "mxc://matrix.org/abc123"
            MockUploadResponse.return_value = mock_upload
            mock_matrix_client.upload = AsyncMock(return_value=mock_upload)

            # Mock room_send response
            with patch("nio.RoomSendResponse") as MockRoomSendResponse:
                MockRoomSendResponse.return_value = MagicMock(event_id="$event123")
                mock_matrix_client.room_send = AsyncMock(return_value=MagicMock(event_id="$event123"))

                attachment = self._create_attachment(temp_file, "image/png", size=1024)
                msg = OutboundMessage(
                    channel_name="matrix",
                    chat_id="!room:matrix.org",
                    thread_id="thread-1",
                    text="test",
                )

                success = await channel.send_file(msg, attachment)

                assert success is True
                # Verify room_send was called
                mock_matrix_client.room_send.assert_called_once()
                call_kwargs = mock_matrix_client.room_send.call_args[1]
                content = call_kwargs["content"]
                assert content["msgtype"] == "m.image"

    @pytest.mark.asyncio
    async def test_send_audio_uses_m_audio(self, mock_matrix_client, temp_file):
        """Audio files should use msgtype 'm.audio'."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        channel._client = mock_matrix_client

        mock_upload = MagicMock()
        mock_upload.content_uri = "mxc://matrix.org/audio123"
        mock_matrix_client.upload = AsyncMock(return_value=mock_upload)

        with patch("nio.RoomSendResponse"):
            mock_matrix_client.room_send = AsyncMock(return_value=MagicMock(event_id="$event123"))

            attachment = self._create_attachment(temp_file, "audio/mp3", size=5000)
            msg = OutboundMessage(
                channel_name="matrix",
                chat_id="!room:matrix.org",
                thread_id="thread-1",
                text="test",
            )

            success = await channel.send_file(msg, attachment)

            assert success is True
            call_kwargs = mock_matrix_client.room_send.call_args[1]
            content = call_kwargs["content"]
            assert content["msgtype"] == "m.audio"

    @pytest.mark.asyncio
    async def test_send_video_uses_m_video(self, mock_matrix_client, temp_file):
        """Video files should use msgtype 'm.video'."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        channel._client = mock_matrix_client

        mock_upload = MagicMock()
        mock_upload.content_uri = "mxc://matrix.org/video123"
        mock_matrix_client.upload = AsyncMock(return_value=mock_upload)

        with patch("nio.RoomSendResponse"):
            mock_matrix_client.room_send = AsyncMock(return_value=MagicMock(event_id="$event123"))

            attachment = self._create_attachment(temp_file, "video/mp4", size=10000)
            msg = OutboundMessage(
                channel_name="matrix",
                chat_id="!room:matrix.org",
                thread_id="thread-1",
                text="test",
            )

            success = await channel.send_file(msg, attachment)

            assert success is True
            call_kwargs = mock_matrix_client.room_send.call_args[1]
            content = call_kwargs["content"]
            assert content["msgtype"] == "m.video"

    @pytest.mark.asyncio
    async def test_send_generic_file_uses_m_file(self, mock_matrix_client, temp_file):
        """Generic files (PDF, etc.) should use msgtype 'm.file'."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        channel._client = mock_matrix_client

        mock_upload = MagicMock()
        mock_upload.content_uri = "mxc://matrix.org/file123"
        mock_matrix_client.upload = AsyncMock(return_value=mock_upload)

        with patch("nio.RoomSendResponse"):
            mock_matrix_client.room_send = AsyncMock(return_value=MagicMock(event_id="$event123"))

            attachment = self._create_attachment(temp_file, "application/pdf", size=2000)
            msg = OutboundMessage(
                channel_name="matrix",
                chat_id="!room:matrix.org",
                thread_id="thread-1",
                text="test",
            )

            success = await channel.send_file(msg, attachment)

            assert success is True
            call_kwargs = mock_matrix_client.room_send.call_args[1]
            content = call_kwargs["content"]
            assert content["msgtype"] == "m.file"

    @pytest.mark.asyncio
    async def test_send_file_skips_large_files(self, mock_matrix_client, temp_file):
        """Files larger than 20MB should be skipped."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        channel._client = mock_matrix_client

        # 25MB file
        attachment = self._create_attachment(temp_file, "video/mp4", size=25 * 1024 * 1024)
        msg = OutboundMessage(
            channel_name="matrix",
            chat_id="!room:matrix.org",
            thread_id="thread-1",
            text="test",
        )

        success = await channel.send_file(msg, attachment)

        assert success is False
        mock_matrix_client.upload.assert_not_called()


class TestMatrixOnRoomMessage:
    """Test handling of different Matrix message types."""

    @pytest.fixture
    def channel(self):
        """Create a MatrixChannel instance for testing."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        channel._client = MagicMock()
        channel._client.user_id = "@bot:matrix.org"
        return channel

    def _create_room_event(self, event_class, body: str, url: str = None, sender: str = "@user:matrix.org"):
        """Helper to create a mock room event."""
        event = MagicMock()
        event.__class__ = event_class
        event.sender = sender
        event.body = body
        event.event_id = "$event123"
        if url:
            event.url = url
        return event

    @pytest.mark.asyncio
    async def test_handles_text_message(self, channel):
        """RoomMessageText should be parsed correctly."""
        # Create mock text event
        mock_text_event = MagicMock()
        mock_text_event.sender = "@user:matrix.org"
        mock_text_event.body = "Hello bot!"
        mock_text_event.event_id = "$event123"

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"
        mock_room.display_name = "Test Room"

        # Mock the download method to avoid actual downloads
        channel._download_matrix_media = AsyncMock(return_value=None)

        # Publish to bus and capture
        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        # Import the event class and call the handler
        from nio import RoomMessageText

        mock_text_event.__class__ = RoomMessageText
        await channel._on_room_message(mock_room, mock_text_event)

        assert len(published) == 1
        msg = published[0]
        assert msg.text == "Hello bot!"
        assert msg.msg_type == InboundMessageType.CHAT
        assert msg.files == []

    @pytest.mark.asyncio
    async def test_handles_command_message(self, channel):
        """Messages starting with / should be treated as commands."""
        from nio import RoomMessageText

        mock_event = MagicMock()
        mock_event.__class__ = RoomMessageText
        mock_event.sender = "@user:matrix.org"
        mock_event.body = "/help"
        mock_event.event_id = "$event123"

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_message(mock_room, mock_event)

        assert len(published) == 1
        assert published[0].msg_type == InboundMessageType.COMMAND

    @pytest.mark.asyncio
    async def test_handles_image_message(self, channel):
        """RoomMessageImage should download and attach the image."""
        from nio import RoomMessageImage

        mock_event = MagicMock()
        mock_event.__class__ = RoomMessageImage
        mock_event.sender = "@user:matrix.org"
        mock_event.body = "photo.png"
        mock_event.url = "mxc://matrix.org/image123"
        mock_event.event_id = "$event123"

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"

        # Mock successful download
        channel._download_matrix_media = AsyncMock(return_value=Path("/tmp/image.png"))

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_message(mock_room, mock_event)

        assert len(published) == 1
        msg = published[0]
        assert "[用户发送了一张图片" in msg.text
        assert len(msg.files) == 1
        assert msg.files[0]["type"] == "image"

    @pytest.mark.asyncio
    async def test_handles_audio_message(self, channel):
        """RoomMessageAudio should download and attach the audio."""
        from nio import RoomMessageAudio

        mock_event = MagicMock()
        mock_event.__class__ = RoomMessageAudio
        mock_event.sender = "@user:matrix.org"
        mock_event.body = "voice.mp3"
        mock_event.url = "mxc://matrix.org/audio123"
        mock_event.event_id = "$event123"

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"

        channel._download_matrix_media = AsyncMock(return_value=Path("/tmp/audio.mp3"))

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_message(mock_room, mock_event)

        assert len(published) == 1
        msg = published[0]
        assert "[用户发送了一个音频" in msg.text
        assert len(msg.files) == 1
        assert msg.files[0]["type"] == "audio"

    @pytest.mark.asyncio
    async def test_handles_video_message(self, channel):
        """RoomMessageVideo should download and attach the video."""
        from nio import RoomMessageVideo

        mock_event = MagicMock()
        mock_event.__class__ = RoomMessageVideo
        mock_event.sender = "@user:matrix.org"
        mock_event.body = "video.mp4"
        mock_event.url = "mxc://matrix.org/video123"
        mock_event.event_id = "$event123"

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"

        channel._download_matrix_media = AsyncMock(return_value=Path("/tmp/video.mp4"))

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_message(mock_room, mock_event)

        assert len(published) == 1
        msg = published[0]
        assert "[用户发送了一个视频" in msg.text
        assert len(msg.files) == 1
        assert msg.files[0]["type"] == "video"

    @pytest.mark.asyncio
    async def test_handles_file_message(self, channel):
        """RoomMessageFile should download and attach the file."""
        from nio import RoomMessageFile

        mock_event = MagicMock()
        mock_event.__class__ = RoomMessageFile
        mock_event.sender = "@user:matrix.org"
        mock_event.body = "document.pdf"
        mock_event.url = "mxc://matrix.org/file123"
        mock_event.event_id = "$event123"

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"

        channel._download_matrix_media = AsyncMock(return_value=Path("/tmp/document.pdf"))

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_message(mock_room, mock_event)

        assert len(published) == 1
        msg = published[0]
        assert "[用户发送了一个文件" in msg.text
        assert len(msg.files) == 1
        assert msg.files[0]["type"] == "file"

    @pytest.mark.asyncio
    async def test_ignores_own_messages(self, channel):
        """Messages from the bot itself should be ignored."""
        from nio import RoomMessageText

        mock_event = MagicMock()
        mock_event.__class__ = RoomMessageText
        mock_event.sender = "@bot:matrix.org"  # Same as bot
        mock_event.body = "My own message"

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_message(mock_room, mock_event)

        assert len(published) == 0

    @pytest.mark.asyncio
    async def test_respects_allowed_rooms(self, channel):
        """Messages from non-allowed rooms should be ignored."""
        from nio import RoomMessageText

        channel._allowed_rooms = {"!allowed:matrix.org"}

        mock_event = MagicMock()
        mock_event.__class__ = RoomMessageText
        mock_event.sender = "@user:matrix.org"
        mock_event.body = "Hello"

        mock_room = MagicMock()
        mock_room.room_id = "!notallowed:matrix.org"

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_message(mock_room, mock_event)

        assert len(published) == 0

    @pytest.mark.asyncio
    async def test_respects_allowed_users(self, channel):
        """Messages from non-allowed users should be ignored."""
        from nio import RoomMessageText

        channel._allowed_users = {"@vip:matrix.org"}

        mock_event = MagicMock()
        mock_event.__class__ = RoomMessageText
        mock_event.sender = "@normal:matrix.org"  # Not in allowed list
        mock_event.body = "Hello"

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_message(mock_room, mock_event)

        assert len(published) == 0


class TestMatrixMarkdownToHtml:
    """Test the markdown to HTML conversion for Matrix."""

    def test_escapes_html_entities(self):
        """HTML entities should be escaped."""
        from src.channels.matrix import MatrixChannel

        result = MatrixChannel._markdown_to_html("<script>alert('xss')</script>")
        assert "&lt;script&gt;" in result
        assert "<script>" not in result

    def test_converts_bold(self):
        """Bold markdown should be converted to HTML."""
        from src.channels.matrix import MatrixChannel

        result = MatrixChannel._markdown_to_html("**bold text**")
        assert "<strong>bold text</strong>" in result

    def test_converts_italic(self):
        """Italic markdown should be converted to HTML."""
        from src.channels.matrix import MatrixChannel

        result = MatrixChannel._markdown_to_html("*italic text*")
        assert "<em>italic text</em>" in result

    def test_converts_code(self):
        """Inline code should be converted to HTML."""
        from src.channels.matrix import MatrixChannel

        result = MatrixChannel._markdown_to_html("`code`")
        assert "<code>code</code>" in result

    def test_converts_links(self):
        """Links should be converted to HTML."""
        from src.channels.matrix import MatrixChannel

        result = MatrixChannel._markdown_to_html("[link text](https://example.com)")
        assert '<a href="https://example.com">link text</a>' in result


class TestMatrixEncryptedEvents:
    """Test handling of encrypted events that fail to decrypt."""

    @pytest.fixture
    def channel(self):
        """Create a MatrixChannel instance for testing."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        channel._client = MagicMock()
        channel._client.user_id = "@bot:matrix.org"
        channel._client.request_room_key = AsyncMock()
        return channel

    @pytest.mark.asyncio
    async def test_handles_encrypted_event(self, channel):
        """MegolmEvent (undecryptable) should be handled gracefully."""
        from nio import MegolmEvent

        mock_event = MagicMock(spec=MegolmEvent)
        mock_event.sender = "@user:matrix.org"
        mock_event.event_id = "$encrypted123"
        mock_event.session_id = "session_abc"
        mock_event.sender_key = "key_xyz"

        mock_room = MagicMock()
        mock_room.room_id = "!encrypted_room:matrix.org"
        mock_room.display_name = "Secret Room"

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_encrypted_event(mock_room, mock_event)

        # Should request room key
        channel._client.request_room_key.assert_called_once()

        # Should publish a notice about decryption failure
        assert len(published) == 1
        msg = published[0]
        assert "无法解密" in msg.text
        assert msg.metadata.get("decryption_failed") is True

    @pytest.mark.asyncio
    async def test_encrypted_event_ignores_own_messages(self, channel):
        """Encrypted events from the bot itself should be ignored."""
        from nio import MegolmEvent

        mock_event = MagicMock(spec=MegolmEvent)
        mock_event.sender = "@bot:matrix.org"  # Same as bot
        mock_event.session_id = "session_abc"
        mock_event.sender_key = "key_xyz"

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_encrypted_event(mock_room, mock_event)

        assert len(published) == 0

    @pytest.mark.asyncio
    async def test_encrypted_event_respects_allowed_rooms(self, channel):
        """Encrypted events from non-allowed rooms should be ignored."""
        from nio import MegolmEvent

        channel._allowed_rooms = {"!allowed:matrix.org"}

        mock_event = MagicMock(spec=MegolmEvent)
        mock_event.sender = "@user:matrix.org"
        mock_event.session_id = "session_abc"
        mock_event.sender_key = "key_xyz"

        mock_room = MagicMock()
        mock_room.room_id = "!notallowed:matrix.org"

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_encrypted_event(mock_room, mock_event)

        assert len(published) == 0


class TestMatrixDownloadMxc:
    """Test the _download_matrix_media method with download_mxc."""

    @pytest.fixture
    def channel(self):
        """Create a MatrixChannel instance for testing."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        return channel

    @pytest.mark.asyncio
    async def test_download_uses_nio_download_mxc(self, channel):
        """Download should use nio's download_mxc for automatic decryption."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.body = b"decrypted image data"
        mock_client.download_mxc = AsyncMock(return_value=mock_response)

        channel._client = mock_client

        result = await channel._download_matrix_media("mxc://matrix.org/abc123", "image.png")

        # Should call download_mxc
        mock_client.download_mxc.assert_called_once_with("mxc://matrix.org/abc123")

        # Should return a path to the downloaded file
        assert result is not None
        assert result.exists()
        assert result.read_bytes() == b"decrypted image data"

        # Cleanup
        result.unlink()

    @pytest.mark.asyncio
    async def test_download_returns_none_on_failure(self, channel):
        """Download should return None if download_mxc fails."""
        mock_client = MagicMock()
        mock_client.download_mxc = AsyncMock(return_value=MagicMock())  # Not a DownloadResponse

        channel._client = mock_client

        result = await channel._download_matrix_media("mxc://matrix.org/abc123", "file.txt")

        assert result is None

    @pytest.mark.asyncio
    async def test_download_returns_none_on_exception(self, channel):
        """Download should return None if an exception occurs."""
        mock_client = MagicMock()
        mock_client.download_mxc = AsyncMock(side_effect=Exception("Network error"))

        channel._client = mock_client

        result = await channel._download_matrix_media("mxc://matrix.org/abc123", "file.txt")

        assert result is None


class TestMatrixTypingNotification:
    """Test typing notification functionality."""

    @pytest.fixture
    def channel(self):
        """Create a MatrixChannel instance for testing."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        channel._client = MagicMock()
        channel._client.user_id = "@bot:matrix.org"
        channel._client.access_token = "test-token"
        channel._client.homeserver = "https://matrix.org"
        return channel

    @pytest.mark.asyncio
    async def test_send_typing_notification_enabled(self, channel):
        """Typing notification should be sent when enabled."""
        channel._typing_enabled = True

        with patch("httpx.AsyncClient") as MockClient:
            mock_response = MagicMock()
            mock_response.status_code = 200

            mock_http = AsyncMock()
            mock_http.put = AsyncMock(return_value=mock_response)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock()

            MockClient.return_value = mock_http

            await channel._send_typing_notification("!room:matrix.org", typing=True)

            # Should have called PUT
            mock_http.put.assert_called_once()
            call_args = mock_http.put.call_args
            assert "typing" in call_args[1]["json"]
            assert call_args[1]["json"]["typing"] is True

    @pytest.mark.asyncio
    async def test_send_typing_notification_disabled(self, channel):
        """Typing notification should not be sent when disabled."""
        channel._typing_enabled = False

        with patch("httpx.AsyncClient") as MockClient:
            await channel._send_typing_notification("!room:matrix.org", typing=True)

            # Should not have created an HTTP client
            MockClient.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_typing_creates_task(self, channel):
        """Starting typing should create a background task."""
        channel._running = True
        channel._send_typing_notification = AsyncMock()

        await channel._start_typing("!room:matrix.org")

        assert "!room:matrix.org" in channel._typing_tasks

        # Cleanup
        channel._typing_tasks["!room:matrix.org"].cancel()
        try:
            await channel._typing_tasks["!room:matrix.org"]
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_stop_typing_cancels_task(self, channel):
        """Stopping typing should cancel the background task."""
        channel._running = True
        channel._send_typing_notification = AsyncMock()

        await channel._start_typing("!room:matrix.org")
        assert "!room:matrix.org" in channel._typing_tasks

        await channel._stop_typing("!room:matrix.org")

        assert "!room:matrix.org" not in channel._typing_tasks


class TestMatrixReactions:
    """Test emoji reaction functionality."""

    @pytest.fixture
    def channel(self):
        """Create a MatrixChannel instance for testing."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        channel._client = MagicMock()
        channel._client.user_id = "@bot:matrix.org"
        channel._client.room_send = AsyncMock()
        return channel

    @pytest.mark.asyncio
    async def test_send_reaction_enabled(self, channel):
        """Reaction should be sent when enabled."""
        channel._reactions_enabled = True

        with patch("nio.RoomSendResponse") as MockResponse:
            MockResponse.return_value = MagicMock(event_id="$reaction123")
            channel._client.room_send = AsyncMock(return_value=MagicMock(event_id="$reaction123"))

            await channel._send_reaction("!room:matrix.org", "$msg123", "👀")

            channel._client.room_send.assert_called_once()
            call_kwargs = channel._client.room_send.call_args[1]
            content = call_kwargs["content"]
            assert content["m.relates_to"]["rel_type"] == "m.annotation"
            assert content["m.relates_to"]["event_id"] == "$msg123"
            assert content["m.relates_to"]["key"] == "👀"

    @pytest.mark.asyncio
    async def test_send_reaction_disabled(self, channel):
        """Reaction should not be sent when disabled."""
        channel._reactions_enabled = False

        await channel._send_reaction("!room:matrix.org", "$msg123", "👀")

        channel._client.room_send.assert_not_called()


class TestMatrixThreading:
    """Test Matrix threading (MSC3440) support."""

    @pytest.fixture
    def channel(self):
        """Create a MatrixChannel instance for testing."""
        from src.channels.matrix import MatrixChannel

        bus = MessageBus()
        channel = MatrixChannel(bus, {"homeserver": "https://matrix.org", "user_id": "@bot:matrix.org"})
        channel._client = MagicMock()
        channel._client.user_id = "@bot:matrix.org"
        channel._start_time = time.time()
        channel._start_typing = AsyncMock()
        channel._send_reaction = AsyncMock()
        channel._download_matrix_media = AsyncMock(return_value=None)
        return channel

    @pytest.mark.asyncio
    async def test_thread_root_extracted_from_event(self, channel):
        """Thread root should be extracted from event relations."""
        from nio import RoomMessageText

        mock_event = MagicMock()
        mock_event.__class__ = RoomMessageText
        mock_event.sender = "@user:matrix.org"
        mock_event.body = "Thread reply"
        mock_event.event_id = "$reply123"
        mock_event.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread_root_456",
                }
            }
        }

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_message(mock_room, mock_event)

        assert len(published) == 1
        msg = published[0]
        # topic_id should be the thread root, not the room ID
        assert msg.topic_id == "$thread_root_456"
        assert msg.metadata.get("thread_root_id") == "$thread_root_456"

    @pytest.mark.asyncio
    async def test_non_thread_message_uses_room_as_topic(self, channel):
        """Messages not in a thread should use room_id as topic_id."""
        from nio import RoomMessageText

        mock_event = MagicMock()
        mock_event.__class__ = RoomMessageText
        mock_event.sender = "@user:matrix.org"
        mock_event.body = "Regular message"
        mock_event.event_id = "$msg123"
        mock_event.source = {}  # No thread relation

        mock_room = MagicMock()
        mock_room.room_id = "!room:matrix.org"

        published = []

        async def capture(msg):
            published.append(msg)

        channel.bus.publish_inbound = capture

        await channel._on_room_message(mock_room, mock_event)

        assert len(published) == 1
        msg = published[0]
        # topic_id should be the room ID
        assert msg.topic_id == "!room:matrix.org"
        assert msg.metadata.get("thread_root_id") is None
