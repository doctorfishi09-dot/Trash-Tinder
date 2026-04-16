# Patching & updates

Quick reference for shipping code changes from this machine to the live
PythonAnywhere deployment.

## The update cycle

### 1. Make the change locally

Edit files in `C:\Projects_Claude\Erstes_Projekt\`. Test with:

```
start.bat
```

or:

```
python server.py
```

Verify the change at `http://127.0.0.1:3000` in your browser before shipping.

### 2. Commit and push to GitHub

```bash
git add -A
git commit -m "short description of the change"
git push
```

### 3. Pull on PythonAnywhere

In PA's **Bash** console (open from the Consoles tab):

```bash
cd ~/trash-tinder
git pull
```

### 4. Reload the web app (only sometimes)

In PA's **Web** tab, click the green **Reload** button.

- **Reload required** if the change touched `server.py`, `wsgi.py`, or `db.py`
  (anything Python that runs on the server).
- **No reload needed** for changes inside `static/` (HTML, CSS, JS, icons,
  manifest, service worker) — they're served straight from disk and pick up
  on the next browser refresh.

Open `https://<username>.eu.pythonanywhere.com/` to confirm the change is live.

## Rollback

If a deploy breaks production, revert to the previous known-good commit:

```bash
cd ~/trash-tinder
git log --oneline
git reset --hard <previous-commit-sha>
```

Then click **Reload** in the PA Web tab. Fix the bug locally, commit, push,
pull again when ready.

## Things that stay between deploys

Because `data/` is in `.gitignore`, the following are preserved on PA across
every `git pull` — they never get overwritten:

- `data/app.db` (the SQLite database: households, users, items, votes)
- `data/photos/` (all uploaded item photos)

Only source code moves through Git.

## One-time pip installs on PA

Some patches add new Python dependencies. Install them in PA's Bash console
once, then click **Reload** in the Web tab.

| Patch | Command |
| --- | --- |
| Push notifications (this patch) | `pip3.10 install --user pywebpush` |

If you skip the install, the app keeps working — just the related feature is
silently disabled (e.g. push sends become no-ops). The error log will show
`ImportError` only if the code tries to import the dep without a guard.

To check what's installed:

```bash
pip3.10 list --user | grep -i webpush
```

## Schema changes

When a change requires new columns or tables, add migration logic to
`_migrate()` in `db.py`. It runs on every server startup. Old columns are
added with `ALTER TABLE ... ADD COLUMN`; new tables with
`CREATE TABLE IF NOT EXISTS`. Existing data survives.

## The APK is set-and-forget

The APK is a wrapper around `https://<username>.eu.pythonanywhere.com/`.
All feature and UI changes reach every installed phone automatically on the
next app launch — no APK rebuild needed. Only regenerate the APK when:

- The PA URL changes (moving hosts)
- The manifest changes fundamentally (icons, app name, display mode)
- You want native Android features the PWA can't provide

## Troubleshooting

**`git pull` says "Your branch is behind" but nothing changes**
You need to commit or stash local changes first. Usually shouldn't happen on
PA because you never edit files there directly — if it does, something was
edited in PA's file editor. Check with `git status` in the PA bash console.

**Service worker keeps serving old static content on your device**
Hard-refresh the page (Ctrl+Shift+R) once. The network-first service worker
will pick up the new files and cache them for offline use.

**PA Web app errors after a reload**
Open PA's **Web** tab, scroll to **Log files**, click **Error log**. The
last block of Python traceback tells you what went wrong. Usually a syntax
error in `server.py` — fix locally, commit, push, pull, reload.
