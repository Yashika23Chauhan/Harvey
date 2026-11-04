"""Generic SMTP + IMAP mailbox provider.

Works with any real mailbox: AgentMail (their IMAP/SMTP relay), Fastmail,
a Google Workspace account with an app password, or self-hosted mail.

.env:
    SMTP_HOST=            IMAP_HOST=
    SMTP_PORT=587         IMAP_PORT=993
    SMTP_USERNAME=        IMAP_USERNAME=   (defaults to SMTP_USERNAME)
    SMTP_PASSWORD=        IMAP_PASSWORD=   (defaults to SMTP_PASSWORD)

Sends as the configured persona (name + email). IMAP polling fetches
UNSEEN inbox messages without marking them read, and reads the Sent folder
(read-only) for replies you wrote from your own mail app.
"""

import asyncio
import email
import email.policy
import imaplib
import logging
import re
import socket
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

import aiosmtplib

from harvey.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)
from harvey.integrations.signature import SignatureError, load_signature, set_body

logger = logging.getLogger("harvey.smtp")


class _IMAP4_SSL(imaplib.IMAP4_SSL):
    """IMAP over TLS that tries IPv4 addresses first.

    Some networks hand out IPv6 routes that never connect; Python then waits
    ~21s per connection before falling back, which made every heartbeat's
    inbox read crawl. TLS still verifies the real hostname."""

    def _create_socket(self, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        infos.sort(key=lambda i: i[0] != socket.AF_INET)
        last = None
        for family, kind, proto, _, addr in infos:
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(timeout if timeout is not None else 15)
                sock.connect(addr)
                sock.settimeout(timeout)
                return self.ssl_context.wrap_socket(sock, server_hostname=self.host)
            except OSError as e:
                sock.close()
                last = e
        raise last or OSError(f"can't connect to {self.host}")


class SmtpImapProvider(MailProvider):
    name = "smtp"

    def __init__(self, config, env):
        self.config = config
        self.smtp_host = getattr(env, "smtp_host", "")
        self.smtp_port = int(getattr(env, "smtp_port", 587) or 587)
        self.smtp_user = getattr(env, "smtp_username", "")
        self.smtp_pass = getattr(env, "smtp_password", "")
        self.imap_host = getattr(env, "imap_host", "") or self.smtp_host
        self.imap_port = int(getattr(env, "imap_port", 993) or 993)
        self.imap_user = getattr(env, "imap_username", "") or self.smtp_user
        self.imap_pass = getattr(env, "imap_password", "") or self.smtp_pass
        # Messages are read without marking them read, so the inbox's UIDs
        # remember where the last fully handled read stopped.
        self._next_uid = 0          # 0: not known yet, read the newest unread
        self._read_up_to = 0        # highest UID the last read returned
        self._uidvalidity = None

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
    ) -> SendResult:
        try:
            sig = load_signature(self.config)
        except SignatureError as e:
            return SendResult(ok=False, error=f"signature: {e}"[:300])
        persona = self.config.persona
        msg = EmailMessage()
        msg["To"] = to_email
        msg["From"] = f"{persona.name} <{persona.email or self.smtp_user}>"
        msg["Subject"] = subject
        message_id = make_msgid(domain=(persona.email or self.smtp_user).split("@")[-1])
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        set_body(msg, body, sig)

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_pass,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465),
                timeout=30,
            )
        except Exception as e:
            logger.error(f"SMTP send failed to {to_email}: {e}")
            return SendResult(ok=False, error=str(e)[:300])
        return SendResult(ok=True, message_id=message_id, thread_ref=message_id)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_unseen, limit)
        except Exception as e:
            logger.error(f"IMAP fetch failed: {e}")
            return []

    def confirm_read(self) -> None:
        """Everything the last read returned was handled: next time, read
        only mail that arrived after it."""
        if self._read_up_to:
            self._next_uid = max(self._next_uid, self._read_up_to + 1)

    async def get_sent(self, limit: int = 30) -> list[InboundMessage]:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_sent, limit)
        except Exception as e:
            logger.error(f"IMAP sent-folder fetch failed: {e}")
            return []

    @staticmethod
    def _sent_mailbox(conn) -> str | None:
        """The Sent folder's name, from its \\Sent flag (Gmail's "[Gmail]/Sent
        Mail", Outlook's "Sent Items"), else a folder named like one."""
        status, rows = conn.list()
        if status != "OK":
            return None
        named = None
        for raw in rows or []:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
            if not m:
                continue
            name = m.group("name").strip()
            if "\\sent" in m.group("flags").lower():
                return name
            if named is None and name.strip('"').lower() in ("sent", "sent items", "sent mail", "inbox.sent"):
                named = name
        return named

    def _fetch_sent(self, limit: int) -> list[InboundMessage]:
        out: list[InboundMessage] = []
        conn = _IMAP4_SSL(self.imap_host, self.imap_port)
        try:
            conn.login(self.imap_user, self.imap_pass)
            box = self._sent_mailbox(conn)
            if not box:
                return []
            status, _ = conn.select(box, readonly=True)
            if status != "OK":
                return []
            since = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
            status, data = conn.search(None, "SINCE", since)
            if status != "OK":
                return []
            for msg_id in data[0].split()[-limit:]:
                status, parts = conn.fetch(msg_id, "(BODY.PEEK[])")
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                parsed = self._parse_rfc822(parts[0][1])
                if parsed and parsed.to_email:
                    out.append(parsed)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    def _fetch_unseen(self, limit: int) -> list[InboundMessage]:
        out: list[InboundMessage] = []
        conn = _IMAP4_SSL(self.imap_host, self.imap_port)
        try:
            conn.login(self.imap_user, self.imap_pass)
            conn.select("INBOX")
            validity = (conn.response("UIDVALIDITY")[1] or [None])[0]
            if validity != self._uidvalidity:
                self._uidvalidity, self._next_uid = validity, 0  # mailbox rebuilt: start over
            uidnext = (conn.response("UIDNEXT")[1] or [None])[0]
            if uidnext:
                # Everything already in the mailbox counts as read up to here.
                self._read_up_to = max(self._read_up_to, int(uidnext) - 1)
            # Once the position is known, read every new message, opened or not:
            # you may read a customer's email in Gmail or Outlook before Harvey does.
            criteria = (f"UID {self._next_uid}:*",) if self._next_uid else ("UNSEEN",)
            status, data = conn.uid("SEARCH", None, *criteria)
            if status != "OK":
                return []
            uids = [int(u) for u in data[0].split() if int(u) >= self._next_uid][-limit:]
            for uid in uids:
                # PEEK leaves the message unread: the mailbox may be someone's
                # personal inbox, and processed_replies already dedups.
                status, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                self._read_up_to = max(self._read_up_to, uid)
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                parsed = self._parse_rfc822(parts[0][1])
                if parsed:
                    out.append(parsed)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    @staticmethod
    def _parse_rfc822(raw: bytes) -> InboundMessage | None:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None
        from_email = parseaddr(str(msg.get("From", "")))[1].lower()
        if not from_email:
            return None
        subject = str(msg.get("Subject", ""))

        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    import re
                    body = re.sub(r"<[^>]+>", " ", body)
        except Exception:
            pass

        message_id = str(msg.get("Message-ID", "")).strip()
        return InboundMessage(
            provider_id=message_id or f"{from_email}:{subject}",
            from_email=from_email,
            subject=subject,
            body=(body or "")[:5000],
            message_id=message_id,
            in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
            thread_ref=str(msg.get("In-Reply-To", "")).strip(),
            date=str(msg.get("Date", "")),
            is_bounce=looks_like_bounce(from_email, subject),
            to_email=parseaddr(str(msg.get("To", "")))[1].lower(),
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD missing from .env"
        # SMTP handshake
        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.smtp_host, port=self.smtp_port,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465), timeout=15,
            )
            await smtp.connect()
            await smtp.login(self.smtp_user, self.smtp_pass)
            await smtp.quit()
        except Exception as e:
            return False, f"SMTP login failed: {str(e)[:200]}"
        # IMAP handshake
        loop = asyncio.get_event_loop()

        def _imap_check():
            conn = _IMAP4_SSL(self.imap_host, self.imap_port)
            try:
                conn.login(self.imap_user, self.imap_pass)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        try:
            await loop.run_in_executor(None, _imap_check)
        except Exception as e:
            return False, f"SMTP OK but IMAP login failed: {str(e)[:200]}"
        return True, f"SMTP + IMAP connected as {self.smtp_user}"
