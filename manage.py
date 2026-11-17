"""Admin commands.

  python manage.py adduser <username> <admin|accountant|viewer>   (asks for the password)
  python manage.py passwd <username>                               (reset a password)
  python manage.py users                                           (list users)
"""
import getpass
import sys

from werkzeug.security import generate_password_hash

import db


def ask_password():
    while True:
        p1 = getpass.getpass("Password (8+ characters): ")
        if len(p1) < 8:
            print("Too short.")
            continue
        if p1 != getpass.getpass("Repeat password: "):
            print("Passwords don't match.")
            continue
        return p1


def main():
    con = db.connect()
    db.init(con)
    args = sys.argv[1:]
    if len(args) == 3 and args[0] == "adduser" and args[2] in ("admin", "accountant", "viewer"):
        con.execute("INSERT INTO users(username, password_hash, role) VALUES (?,?,?)",
                    (args[1], generate_password_hash(ask_password()), args[2]))
        con.commit()
        print(f"Created {args[1]} ({args[2]}).")
    elif len(args) == 2 and args[0] == "passwd":
        n = con.execute("UPDATE users SET password_hash=?, active=1 WHERE username=?",
                        (generate_password_hash(ask_password()), args[1])).rowcount
        con.commit()
        print("Password updated." if n else "No such user.")
    elif args == ["users"]:
        for r in con.execute("SELECT username, role, active, last_login FROM users ORDER BY username"):
            print(f"{r[0]:20} {r[1]:11} {'active' if r[2] else 'disabled':9} last login {r[3] or 'never'}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
