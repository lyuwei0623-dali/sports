"""Member-entry links are navigation, never authentication credentials."""
import os
from urllib.parse import urlsplit, urlunsplit


def build_member_link(raw):
    parts = urlsplit(str(raw).strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("請填入完整的 HTTPS APP 網址，不是 GitHub 專案網址")
    if parts.hostname.lower() in {"github.com", "www.github.com"}:
        raise ValueError("請填入 APP 網址，不是 GitHub 網址")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "view=member", ""))


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
        raw = st.text_input("APP 公開網址", value=os.environ.get("APP_PUBLIC_URL", ""),
                            placeholder="https://你的APP名稱.streamlit.app", key="member_base_url")
        st.caption("此為所有會員共用入口，非個別帳號或免密碼連結。會員仍須輸入原本設定的會員密碼。")
        if st.button("生成會員連結"):
            try:
                link = build_member_link(raw)
                st.text_input("複製此連結給會員", value=link)
                st.download_button("下載會員邀請文字", "維大力體育APP\n" + link + "\n請輸入另行提供的會員密碼。",
                                   file_name="member_invitation.txt", mime="text/plain")
            except ValueError as exc:
                st.error(str(exc))
