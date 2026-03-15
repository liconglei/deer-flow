"""Local HTTP-based web fetch tool.

This tool fetches web content using simple HTTP requests and extracts
readable content using Readability. It does not support JavaScript rendering.

For JavaScript-heavy sites, consider using Playwright MCP or remote services
like Jina AI or InfoQuest.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from langchain.tools import tool

from deerflow.config import get_app_config
from deerflow.utils.readability import ReadabilityExtractor

logger = logging.getLogger(__name__)

readability_extractor = ReadabilityExtractor()

# Default User-Agent to avoid basic bot detection
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Private IP ranges (RFC 1918) - blocked for security
_PRIVATE_IP_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),      # 10.0.0.0 - 10.255.255.255
    ipaddress.ip_network("172.16.0.0/12"),   # 172.16.0.0 - 172.31.255.255
    ipaddress.ip_network("192.168.0.0/16"),  # 192.168.0.0 - 192.168.255.255
    ipaddress.ip_network("127.0.0.0/8"),     # Loopback
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local
    ipaddress.ip_network("::1/128"),         # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),        # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),       # IPv6 link-local
]


def _is_private_ip(ip_str: str) -> bool:
    """Check if an IP address is in private/local range."""
    try:
        ip = ipaddress.ip_address(ip_str)
        for network in _PRIVATE_IP_NETWORKS:
            if ip in network:
                return True
        return False
    except ValueError:
        return False


def _resolve_host(url: str) -> str | None:
    """Resolve URL hostname to IP address for security check."""
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return None
        addr_info = socket.getaddrinfo(hostname, None)
        if addr_info:
            return addr_info[0][4][0]
        return None
    except Exception:
        return None


@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    This tool does NOT support JavaScript rendering. For dynamic content, use other tools.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    start_time = time.time()

    # Get timeout from config (existing config option)
    config = get_app_config().get_tool_config("web_fetch")
    timeout = 30
    if config is not None and "timeout" in config.model_extra:
        timeout = config.model_extra.get("timeout")

    logger.info("[http_fetch] [LOCAL] starting fetch: url=%s, timeout=%ss", url, timeout)

    # Security check: block private/local IP addresses (always enabled)
    resolved_ip = _resolve_host(url)
    if resolved_ip and _is_private_ip(resolved_ip):
        error_msg = f"Error: Access to private/local IP addresses is blocked for security (resolved: {resolved_ip})"
        logger.warning("[http_fetch] blocked private IP access: url=%s, resolved_ip=%s", url, resolved_ip)
        return error_msg

    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # Detect encoding from Content-Type header, fallback to UTF-8
            content_type = response.headers.get("Content-Type", "")
            encoding = "utf-8"
            if "charset=" in content_type.lower():
                encoding = content_type.lower().split("charset=")[-1].split(";")[0].strip()
            html_content = response.read().decode(encoding, errors="replace")

        fetch_time = time.time() - start_time

        # Extract readable content
        article = readability_extractor.extract_article(html_content)
        markdown_content = article.to_markdown()

        total_time = time.time() - start_time
        content_len = len(markdown_content)

        logger.info(
            "[http_fetch] [LOCAL] success: url=%s, fetch_time=%.2fs, total_time=%.2fs, content_len=%d",
            url, fetch_time, total_time, content_len
        )

        # Truncate to avoid token limits
        return markdown_content[:4096]

    except urllib.error.URLError as e:
        elapsed = time.time() - start_time
        error_msg = f"Error: Failed to fetch URL: {str(e)}"
        logger.warning("[http_fetch] [LOCAL] URL error: url=%s, elapsed=%.2fs, error=%s", url, elapsed, e)
        return error_msg
    except TimeoutError:
        elapsed = time.time() - start_time
        error_msg = f"Error: Request timed out after {timeout} seconds"
        logger.warning("[http_fetch] [LOCAL] timeout: url=%s, elapsed=%.2fs, timeout=%ss", url, elapsed, timeout)
        return error_msg
    except Exception as e:
        elapsed = time.time() - start_time
        error_msg = f"Error: {str(e)}"
        logger.exception("[http_fetch] [LOCAL] unexpected error: url=%s, elapsed=%.2fs", url, elapsed)
        return error_msg