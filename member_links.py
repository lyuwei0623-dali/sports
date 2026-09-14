"""Member-entry links are navigation, never authentication credentials."""
import hashlib
import hmac
import os
import re
from urllib.parse import urlencode, urlsplit, urlunsplit


def _member_account(value: str) -> str:
    """Accept a short member label without placing a password in the URL."""

    account = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,48}", account):
        raise ValueError("會員帳號請使用 2～48 個英數字、底線或連字號")
    return account


def _member_signature(account: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), account.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_member_link(account: str, signature: str, secret: str) -> bool:
    """Verify an account-link signature without storing member records locally."""

    if not account or not signature or not secret:
        return False
    try:
        expected = _member_signature(_member_account(account), secret)
    except ValueError:
        return False
    return hmac.compare_digest(expected, str(signature))


def build_member_link(raw, member_account: str = "", link_secret: str = ""):
    parts = urlsplit(str(raw).strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("請填入完整的 HTTPS APP 網址，不是 GitHub 專案網址")
    if parts.hostname.lower() in {"github.com", "www.github.com"}:
        raise ValueError("請填入 APP 網址，不是 GitHub 網址")
    # Strip any prefilled password, GitHub path, or old query string.  The
    # resulting link always opens this app's member-only password form.
    query = {"view": "member"}
    if member_account:
        if not link_secret:
            raise ValueError("尚未設定 APP_MEMBER_LINK_SECRET，無法安全產生專屬會員連結")
        account = _member_account(member_account)
        query.update({"member": account, "signature": _member_signature(account, link_secret)})
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), ""))


def resolve_login_role(password, member_password, admin_password, member_entry=False):
    if not password:
        return None
    if member_entry:
        return "member" if password == member_password else None
    if password == admin_password:
        return "admin"
    if password == member_password:
        return "member"
    return None


def render_member_link(st):
    with st.expander("會員專用連結", expanded=False):
        raw = os.environ.get("APP_PUBLIC_URL", "").strip()
        link_secret = os.environ.get("APP_MEMBER_LINK_SECRET", "")
        st.caption("APP 網址已由管理員設定，不需每次重打。請輸入會員帳號即可產生專屬入口。")
        account = st.text_input("會員帳號", placeholder="例如 dali_001", key="member_account")
        st.caption("會員開啟後仍須輸入會員密碼；連結不含密碼，管理員密碼也不能從此入口進後台。")
        if st.button("生成會員連結"):
            try:
                link = build_member_link(raw, account, link_secret)
                st.success("連結已生成。會員點開後會直接到會員密碼登入畫面。")
                st.code(link, language=None)
                st.download_button("下載會員邀請文字", "維大力體育APP\n會員帳號：" + _member_account(account) + "\n" + link + "\n請輸入另行提供的會員密碼。",
                                   file_name=f"member_invitation_{_member_account(account)}.txt", mime="text/plain")
            except ValueError as exc:
                st.error(str(exc))
