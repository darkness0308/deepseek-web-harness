Drop a browser export of your signed-in chat.deepseek.com session in this folder.
It is imported automatically when the harness starts, on /refresh, and when DeepSeek
rejects the saved token (or run: python import_session.py).

Files that contain a LIVE LOGIN (never commit, share or export them) - this folder is git-ignored
and left out of exports. Delete a file once it has been imported.

What a file must contain
  A cookie export (Cookie-Editor, EditThisCookie ...) is NOT enough: DeepSeek keeps the login
  token in localStorage, not in a cookie. Use one of:
    1. /connect in the harness + the extension in extension/deepseek-session-exporter (one click; no file needed)
    2. a DevTools "Copy as cURL (bash)" of any chat.deepseek.com/api/v0/ request, saved as a .txt file
    3. a cookie export here + the token:  python import_session.py --token-prompt
       (token: F12 > Console > copy(JSON.parse(localStorage.userToken).value))
  Also understood: Playwright storage_state, Netscape cookies.txt, a Cookie header, session.json, HAR.
