#!/usr/bin/env python3
# Recovery tool (DESIGN §1): decrypt a pre-Chrome-158 (v10, mock keychain) Cookies DB from a backup into
# CDP Storage.setCookies params on stdout.
# Usage: (umask 077; python3 -I tools/v10-cookies-to-cdp.py <Cookies> > cookies.json)
# The output holds live session secrets: keep it private, delete after use.
"""Read a pre-Chrome-158 Cookies DB (v10, mock keychain) and print CDP Storage.setCookies params."""
import os, stat, sys
# The shell created the output file under its umask before this ran: make it private before a
# single cookie is written, and never print cookies to a terminal (scrollback, screen sharing).
_out = os.fstat(sys.stdout.fileno())
if stat.S_ISREG(_out.st_mode):
    os.fchmod(sys.stdout.fileno(), 0o600)
elif sys.stdout.isatty():
    sys.exit("v10-cookies-to-cdp: redirect the output to a file: (umask 077; python3 -I ... > cookies.json)")
os.umask(0o077)
import sqlite3, hashlib, json, time
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
KEY = hashlib.pbkdf2_hmac("sha1", b"mock_password", b"saltysalt", 1003, 16)
EPOCH = 11644473600
def dec(host, blob):
    if blob[:3] != b"v10": return None
    c = Cipher(algorithms.AES(KEY), modes.CBC(b" " * 16)).decryptor()
    d = c.update(blob[3:]) + c.finalize(); d = d[:-d[-1]]
    if d[:32] != hashlib.sha256(host.encode()).digest(): return None
    return d[32:].decode("utf-8")
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row
now = time.time(); out = []; skipped = {"expired": 0, "undecryptable": 0, "session": 0}
for r in db.execute("select * from cookies"):
    v = dec(r["host_key"], r["encrypted_value"]) if r["encrypted_value"] else r["value"]
    if v is None: skipped["undecryptable"] += 1; continue
    p = {"name": r["name"], "value": v, "domain": r["host_key"], "path": r["path"],
         "secure": bool(r["is_secure"]), "httpOnly": bool(r["is_httponly"]),
         "priority": ["Low", "Medium", "High"][r["priority"]] if r["priority"] in (0, 1, 2) else "Medium",
         "sourceScheme": ["Unset", "NonSecure", "Secure"][r["source_scheme"]] if r["source_scheme"] in (0, 1, 2) else "Unset",
         "sourcePort": r["source_port"]}
    if r["samesite"] in (0, 1, 2): p["sameSite"] = ["None", "Lax", "Strict"][r["samesite"]]
    if r["is_persistent"]:
        exp = r["expires_utc"] / 1e6 - EPOCH
        if exp <= now: skipped["expired"] += 1; continue
        p["expires"] = exp
    else:
        skipped["session"] += 1   # session cookies are kept too: they were live when the backup ran
    if r["top_frame_site_key"]:
        p["partitionKey"] = {"topLevelSite": r["top_frame_site_key"], "hasCrossSiteAncestor": bool(r["has_cross_site_ancestor"])}
    out.append(p)
json.dump(out, sys.stdout)
print(json.dumps({"restorable": len(out), **skipped}), file=sys.stderr)
